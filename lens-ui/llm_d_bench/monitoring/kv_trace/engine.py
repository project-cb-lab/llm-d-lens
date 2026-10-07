"""Standalone stdlib-only vLLM probe, mounted into model containers by Prism.

Run as a script for begin/end commands. No prompts or token IDs leave the engine.
The hook observes full prompt blocks held at successful request completion, not
placement changes, allocation attempts, or per-token decode memory accesses.
"""

import fcntl
import functools
import hashlib
import json
import os
import re
import sys
import time
from contextlib import contextmanager
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

ROOT = Path(os.environ.get("PRISM_KV_TRACE_DIR", "/tmp/prism-kv-trace"))  # noqa: S108 - pod-local scratch, configurable by env
MAX_BYTES = 128 * 1024 * 1024


@contextmanager
def locked():
    ROOT.mkdir(parents=True, exist_ok=True)
    with (ROOT / "lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def engines():
    result = []
    for path in sorted(ROOT.glob("engine-*.json")):
        item = json.loads(path.read_text())
        try:
            os.kill(item["pid"], 0)
        except ProcessLookupError:
            continue
        result.append(item)
    return result


def command(action, session):
    if not re.fullmatch(r"[a-zA-Z0-9-]{1,100}", session):
        raise ValueError("invalid trace session")
    with locked():
        active = ROOT / "active.json"
        if action == "begin":
            if active.exists():
                raise ValueError("another KV capture is active")
            current = engines()
            if not current or any(not item["supported"] for item in current):
                raise ValueError("compatible vLLM KV manager probe is not ready")
            control = {"session": session, "start": time.time(), "engines": current}
            (ROOT / f"{session}.jsonl").write_text("")
            active.write_text(json.dumps(control))
            return control
        if not active.exists():
            raise ValueError("KV capture is not active")
        control = json.loads(active.read_text())
        if control["session"] != session:
            raise ValueError("KV capture session mismatch")
        active.unlink()
        path = ROOT / f"{session}.jsonl"
        try:
            rows = [json.loads(line) for line in path.read_text().splitlines()]
            return {**control, "end": time.time(), "engines_end": engines(), "requests": rows}
        finally:
            path.unlink(missing_ok=True)


def inspect_request(manager, request):
    groups = manager.kv_cache_config.kv_cache_groups
    if len(groups) != 1 or type(groups[0].kv_cache_spec).__name__ != "FullAttentionSpec":
        raise ValueError("requires one FullAttention KV cache group")
    if not manager.enable_caching:
        raise ValueError("prefix caching disabled")
    spec = groups[0].kv_cache_spec
    size = spec.block_size
    # Reject context-parallel layouts whose logical block size differs.
    effective = getattr(manager, "block_size", None)
    if effective is not None and effective != size:
        raise ValueError("context-parallel block layout unsupported")
    if (
        getattr(request, "lora_request", None)
        or getattr(request, "mm_features", None)
        or getattr(request, "multi_modal_inputs", None)
    ):
        raise ValueError("LoRA and multimodal KV identities are not supported")
    tokens = request.prompt_token_ids
    if not isinstance(tokens, list) or any(type(t) is not int or t < 0 for t in tokens):
        raise ValueError("engine prompt token IDs unavailable")
    if request.num_computed_tokens < len(tokens):
        raise ValueError("prompt computation not complete")
    blocks = manager.get_blocks(request.request_id).blocks
    count = len(tokens) // size
    if len(blocks) != 1 or len(blocks[0]) < count:
        raise ValueError("prompt block coverage incomplete")
    namespace = os.environ.get("PRISM_KV_TRACE_NAMESPACE", "")
    if not namespace:
        raise ValueError("model/cache namespace missing")
    # Canonical chained hashes avoid process-local native hash seeds while
    # retaining engine tokenization and the full parent-prefix context.
    parent = hashlib.sha256(json.dumps([namespace, size, str(getattr(request, "cache_salt", None))]).encode()).digest()
    accesses = []
    for index, block in enumerate(blocks[0][:count]):
        if getattr(block, "is_null", False) or block.block_hash is None:
            raise ValueError("prompt block is not a reusable computed block")
        parent = hashlib.sha256(
            parent + json.dumps(tokens[index * size : (index + 1) * size], separators=(",", ":")).encode()
        ).digest()
        accesses.append({"namespace": namespace, "block_hash": parent.hex(), "valid_tokens": size})
    return accesses


def capture(manager, request):
    if getattr(getattr(request, "status", None), "name", "") not in {"FINISHED_STOPPED", "FINISHED_LENGTH_CAPPED"}:
        return
    if not (ROOT / "active.json").exists():
        return
    try:
        with locked():
            if not (ROOT / "active.json").exists():
                return
            control = json.loads((ROOT / "active.json").read_text())
            path = ROOT / f"{control['session']}.jsonl"
            item = {"request_id": request.request_id, "timestamp": time.time(), "pid": os.getpid()}
            try:
                item["accesses"] = inspect_request(manager, request)
            except Exception as error:
                item["error"] = str(error)
            if path.stat().st_size > MAX_BYTES:
                return  # Collector's client/engine count parity rejects truncation.
            with path.open("a") as output:
                output.write(json.dumps(item, separators=(",", ":")) + "\n")
                output.flush()
    except Exception:  # noqa: S110 - trace capture must never disrupt inference
        # Instrumentation must not break inference; missing records fail parity.
        pass


def install(module):
    cls = module.KVCacheManager
    if getattr(cls, "_prism_kv_trace", False):
        return
    original_init, original_free = cls.__init__, cls.free

    @functools.wraps(original_init)
    def init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        try:
            groups = self.kv_cache_config.kv_cache_groups
            supported = (
                len(groups) == 1
                and type(groups[0].kv_cache_spec).__name__ == "FullAttentionSpec"
                and self.enable_caching
                and callable(getattr(self, "get_blocks", None))
            )
            try:
                engine_version = version("vllm")
            except PackageNotFoundError:
                engine_version = "unknown"
            with locked():
                (ROOT / f"engine-{os.getpid()}.json").write_text(
                    json.dumps(
                        {
                            "pid": os.getpid(),
                            "instance": str(time.time_ns()),
                            "supported": bool(supported),
                            "probe_version": 1,
                            "vllm_version": engine_version,
                            "source": "vllm.KVCacheManager.free",
                        }
                    )
                )
        except Exception:  # noqa: S110 - tracing is optional and must not disrupt inference
            pass

    @functools.wraps(original_free)
    def free(self, request, *args, **kwargs):
        capture(self, request)
        return original_free(self, request, *args, **kwargs)

    cls.__init__, cls.free, cls._prism_kv_trace = init, free, True


if __name__ == "__main__":
    try:
        print(json.dumps(command(sys.argv[1], sys.argv[2])))
    except Exception as error:
        print(json.dumps({"error": str(error)}))
        sys.exit(1)
