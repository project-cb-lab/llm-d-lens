"""Trace dataset catalog, validation, and download support."""

from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import math
import os
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import urljoin, urlparse

import aiohttp

from llm_d_bench.utils.artifact_store import artifact_uri, register_artifacts
from llm_d_bench.utils.paths import storage_path

from .errors import SimulationConfigurationError

if TYPE_CHECKING:
    from .models import SimulationTrace

DEFAULT_MAX_BYTES = 2 * 1024 * 1024 * 1024
TIMELINE_INDEX_VERSION = 2
TIMELINE_BASE_BIN_COUNT = 2048
TIMELINE_SPARSE_RECORD_INTERVAL = 4096

_timeline_locks: dict[str, asyncio.Lock] = {}


class BaseTrace:
    format: ClassVar[str]
    name: ClassVar[str]
    filename: ClassVar[str]
    scenario: ClassVar[str]
    license: ClassVar[str | None] = None
    description: ClassVar[str]
    sha256: ClassVar[str | None] = None
    source_repository: ClassVar[str | None] = None
    externally_managed: ClassVar[bool] = False
    file_backed: ClassVar[bool] = True
    timestamp_divisor: ClassVar[float]
    required_fields: ClassVar[tuple[str, ...]] = ("timestamp", "input_length", "output_length")
    _download_count: ClassVar[int] = 0
    _download_count_lock: ClassVar[asyncio.Lock | None] = None

    def __init__(self, path: Path) -> None:
        self.path = path.resolve(strict=True)

    @classmethod
    def trace_root(cls) -> Path:
        return storage_path("data", "datasets")

    @classmethod
    def catalog_entry(cls) -> dict[str, Any]:
        destination = cls.trace_root() / cls.filename
        try:
            size = destination.stat().st_size
        except OSError:
            size = None
        entry = {
            "name": cls.name,
            "filename": cls.filename,
            "scenario": cls.scenario,
            "trace_format": cls.format,
            "description": cls.description,
            "downloaded": size is not None,
            "path": str(destination),
            "artifact_uri": artifact_uri("dataset", cls.name, cls.filename),
            "size_bytes": size,
        }
        if cls.license is not None:
            entry["license"] = cls.license
        if cls.sha256 is not None:
            entry["sha256"] = cls.sha256
        if cls.source_repository is not None:
            entry["source_repository"] = cls.source_repository
        return entry

    @classmethod
    def generate(cls, destination: Path) -> bool:
        return False

    @classmethod
    def transform_download(
        cls,
        source: Path,
        destination: Path,
    ) -> Path:
        return source

    @classmethod
    async def download(
        cls,
        name: str,
        force: bool = False,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> dict:
        trace_type = trace_registry.get_variant(name)
        if trace_type.externally_managed:
            raise ValueError("Externally managed datasets cannot be downloaded by Prism")
        return await trace_type._download(force, max_bytes)

    @classmethod
    async def _reserve_download(cls) -> None:
        if BaseTrace._download_count_lock is None:
            BaseTrace._download_count_lock = asyncio.Lock()
        async with BaseTrace._download_count_lock:
            try:
                maximum = int(os.environ.get("SIMULATION_MAX_CONCURRENT_DOWNLOADS", "1"))
            except ValueError:
                maximum = 1
            if maximum <= 0:
                maximum = 1
            if BaseTrace._download_count >= maximum:
                raise SimulationConfigurationError("The maximum number of concurrent trace downloads is running")
            BaseTrace._download_count += 1

    @classmethod
    async def _release_download(cls) -> None:
        if BaseTrace._download_count_lock is None:
            raise RuntimeError("Trace download lock is not initialized")
        async with BaseTrace._download_count_lock:
            BaseTrace._download_count -= 1

    @classmethod
    async def _download(
        cls,
        force: bool,
        max_bytes: int,
    ) -> dict:
        catalog_entry = cls.catalog_entry()
        if "url" in catalog_entry and urlparse(catalog_entry["url"]).scheme != "https":
            raise ValueError("trace dataset URL must use HTTPS")
        await cls._reserve_download()
        destination = cls.trace_root() / cls.filename
        lock = destination.parent / f".{destination.name}.lock"
        temporary = destination.parent / f".{destination.name}.{uuid.uuid4()}.tmp"
        transformed_temporary = destination.parent / f".{destination.name}.{uuid.uuid4()}.transformed"
        timeline_temporary = destination.parent / f".{destination.name}.{uuid.uuid4()}.timeline.tmp"
        lock_fd = None
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            try:
                lock_fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError as error:
                raise ValueError(f"trace download is already in progress: {destination}") from error
            if not force and destination.exists():
                raise ValueError(f"{destination} already exists; pass force=true to replace it")
            digest = hashlib.sha256()
            generated = await asyncio.to_thread(cls.generate, temporary)
            if generated:
                os.chmod(temporary, 0o600)
                downloaded = temporary.stat().st_size
                if downloaded > max_bytes:
                    raise ValueError(f"generated trace exceeds max_bytes limit ({max_bytes})")
                digest.update(temporary.read_bytes())
            else:
                timeout = aiohttp.ClientTimeout(total=60)
                downloaded = 0
                url = catalog_entry["url"]
                async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
                    for _redirects in range(11):
                        response = await session.get(
                            url,
                            allow_redirects=False,
                            headers={"User-Agent": "Prism-trace-downloader/1"},
                        )
                        if response.status in {301, 302, 303, 307, 308}:
                            location = response.headers.get("Location")
                            response.release()
                            if not location:
                                raise ValueError("download redirect did not include a location")
                            url = urljoin(url, location)
                            if urlparse(url).scheme != "https":
                                raise ValueError("refusing redirect to a non-HTTPS URL")
                            continue
                        if response.status < 200 or response.status >= 300:
                            response.release()
                            raise ValueError(f"trace download failed with HTTP {response.status}")
                        if urlparse(str(response.url)).scheme != "https":
                            response.release()
                            raise ValueError("final download URL is not HTTPS")
                        if response.content_length is not None and response.content_length > max_bytes:
                            response.release()
                            raise ValueError(f"download exceeds max_bytes limit ({max_bytes})")
                        with temporary.open("xb") as output:
                            os.chmod(temporary, 0o600)
                            async for chunk in response.content.iter_chunked(64 * 1024):
                                downloaded += len(chunk)
                                if downloaded > max_bytes:
                                    raise ValueError(f"download exceeds max_bytes limit ({max_bytes})")
                                digest.update(chunk)
                                output.write(chunk)
                        break
                    else:
                        raise ValueError("too many download redirects")
            sha256 = digest.hexdigest()
            if cls.sha256 is not None and sha256 != cls.sha256:
                raise ValueError(f"SHA-256 mismatch: expected {cls.sha256}, got {sha256}")
            transformed = await asyncio.to_thread(
                cls.transform_download,
                temporary,
                transformed_temporary,
            )
            if transformed != temporary:
                os.chmod(transformed_temporary, 0o600)
                temporary.unlink()
                os.replace(transformed_temporary, temporary)
                downloaded = temporary.stat().st_size
            trace = cls(temporary)
            trace.validate()
            await asyncio.to_thread(
                trace.build_timeline_index,
                output_path=timeline_temporary,
                force=True,
            )
            os.replace(temporary, destination)
            os.replace(timeline_temporary, cls(destination).timeline_index_path)
            metadata_path = Path(f"{destination}.metadata.json")
            metadata_temporary = Path(f"{metadata_path}.{uuid.uuid4()}.tmp")
            try:
                metadata_temporary.write_text(
                    json.dumps(
                        {
                            "dataset": cls.name,
                            "trace_format": cls.format,
                            "source_url": catalog_entry.get("url"),
                            "sha256": sha256,
                            "downloaded_at": datetime.now(UTC).isoformat(),
                            "license": cls.license,
                            "source_repository": cls.source_repository,
                            "description": cls.description,
                        },
                        indent=2,
                    )
                    + "\n",
                    encoding="utf-8",
                )
                os.chmod(metadata_temporary, 0o600)
                os.replace(metadata_temporary, metadata_path)
            finally:
                metadata_temporary.unlink(missing_ok=True)
            await asyncio.to_thread(
                register_artifacts,
                destination.parent,
                owner_type="dataset",
                owner_id=cls.name,
                manifest_name=f"{destination.name}.manifest.json",
                source_version={
                    "source": "generated" if generated else catalog_entry.get("url", "unknown"),
                    "repository": cls.source_repository or "unknown",
                    "revision": getattr(cls, "REVISION", "unknown"),
                    "download_sha256": sha256,
                },
                files={
                    destination.name: {"kind": "dataset"},
                    metadata_path.name: {"kind": "metadata"},
                    cls(destination).timeline_index_path.name: {"kind": "timeline-index"},
                },
            )
            return {
                "artifact_uri": artifact_uri("dataset", cls.name, cls.filename),
                "path": str(destination),
                "trace_format": cls.format,
                "sha256": sha256,
                "metadata_path": str(metadata_path),
                "size_bytes": downloaded,
            }
        except (TimeoutError, aiohttp.ClientError) as error:
            raise ValueError(f"trace download request failed: {error}") from error
        finally:
            if lock_fd is not None:
                os.close(lock_fd)
                lock.unlink(missing_ok=True)
            temporary.unlink(missing_ok=True)
            transformed_temporary.unlink(missing_ok=True)
            timeline_temporary.unlink(missing_ok=True)
            await cls._release_download()

    @classmethod
    def resolve_source(cls, source: str) -> BaseTrace:
        if not source:
            raise SimulationConfigurationError("Trace replay requires trace_path")
        root = cls.trace_root()
        candidate = (root / source).resolve()
        try:
            resolved_root = root.resolve(strict=True)
            resolved = candidate.resolve(strict=True)
        except FileNotFoundError as error:
            raise SimulationConfigurationError(f"Trace path does not exist: {candidate}") from error
        try:
            resolved.relative_to(resolved_root)
        except ValueError as error:
            raise SimulationConfigurationError(f"Trace path must be inside {resolved_root}") from error
        if not resolved.is_file():
            raise SimulationConfigurationError(f"Trace path must be a file: {resolved}")
        return cls(resolved)

    @property
    def timeline_index_path(self) -> Path:
        return Path(f"{self.path}.timeline.v{TIMELINE_INDEX_VERSION}.json")

    def _records(self) -> Iterator[tuple[int | None, dict, float]]:
        with self.path.open("rb") as handle:
            for line in handle:
                offset = handle.tell() - len(line)
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                    timestamp = float(record["timestamp"]) / self.timestamp_divisor
                except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                    raise ValueError(f"Invalid trace timestamp near byte offset {offset}: {error}") from error
                if not isinstance(record, dict) or not math.isfinite(timestamp):
                    raise ValueError(f"Invalid trace record near byte offset {offset}")
                yield offset, record, timestamp

    def _timestamp_entries(self) -> Iterator[tuple[float, int | None]]:
        yield from ((timestamp, offset) for offset, _record, timestamp in self._records())

    def _timestamps(self) -> Iterator[float]:
        yield from (timestamp for timestamp, _offset in self._timestamp_entries())

    def build_timeline_index(
        self,
        *,
        output_path: Path | None = None,
        force: bool = False,
    ) -> dict[str, Any]:
        destination = output_path or self.timeline_index_path
        stat = self.path.stat()
        if not force and destination.exists():
            try:
                cached = json.loads(destination.read_text(encoding="utf-8"))
                if (
                    cached.get("version") == TIMELINE_INDEX_VERSION
                    and cached.get("trace_format") == self.format
                    and cached.get("source_size") == stat.st_size
                    and cached.get("source_mtime_ns") == stat.st_mtime_ns
                ):
                    return cached
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                pass

        request_count = 0
        minimum = maximum = None
        previous = None
        sparse_offsets: list[list[float | int]] = []
        for timestamp, offset in self._timestamp_entries():
            if previous is not None and timestamp < previous:
                raise ValueError("Trace timestamps must be sorted in non-decreasing order")
            if offset is not None and request_count % TIMELINE_SPARSE_RECORD_INTERVAL == 0:
                sparse_offsets.append([timestamp, offset])
            minimum = timestamp if minimum is None else minimum
            maximum = timestamp
            previous = timestamp
            request_count += 1
        if minimum is None or maximum is None:
            raise ValueError("Trace file contains no requests")

        duration = max(0.0, maximum - minimum)
        bin_count = min(TIMELINE_BASE_BIN_COUNT, max(1, request_count))
        counts = [0] * bin_count
        if duration == 0:
            counts[0] = request_count
        else:
            for timestamp in self._timestamps():
                position = min(bin_count - 1, int((timestamp - minimum) / duration * bin_count))
                counts[position] += 1

        index = {
            "version": TIMELINE_INDEX_VERSION,
            "trace_format": self.format,
            "source_size": stat.st_size,
            "source_mtime_ns": stat.st_mtime_ns,
            "request_count": request_count,
            "minimum_timestamp": minimum,
            "maximum_timestamp": maximum,
            "duration_seconds": duration,
            "bin_count": bin_count,
            "counts": counts,
            "sparse_offsets": sparse_offsets,
        }
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.parent / f".{destination.name}.{uuid.uuid4().hex}.tmp"
        try:
            temporary.write_text(json.dumps(index, separators=(",", ":")) + "\n", encoding="utf-8")
            os.chmod(temporary, 0o600)
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        return index

    def timeline(self, requested_bins: int) -> dict[str, Any]:
        index = self.build_timeline_index()
        source_counts = index["counts"]
        target_count = min(requested_bins, len(source_counts))
        counts = [0] * target_count
        for source_index, count in enumerate(source_counts):
            target_index = min(target_count - 1, source_index * target_count // len(source_counts))
            counts[target_index] += count
        duration = float(index["duration_seconds"])
        width = duration / target_count if duration else 0.0
        bins = [
            {
                "start_seconds": position * width,
                "end_seconds": duration if position == target_count - 1 else (position + 1) * width,
                "request_count": count,
            }
            for position, count in enumerate(counts)
        ]
        return {
            "request_count": index["request_count"],
            "duration_seconds": duration,
            "source_size_bytes": index["source_size"],
            "bin_width_seconds": width,
            "bins": bins,
        }

    def validate_range(self, start_seconds: float, end_seconds: float) -> dict[str, Any]:
        index = self.build_timeline_index()
        duration = float(index["duration_seconds"])
        if (
            not math.isfinite(start_seconds)
            or not math.isfinite(end_seconds)
            or start_seconds < 0
            or end_seconds <= start_seconds
            or end_seconds > duration + 1e-6
        ):
            raise SimulationConfigurationError(f"Trace range must satisfy 0 <= start < end <= {duration:g} seconds")
        return index

    def count_requests(self, start_seconds: float, end_seconds: float, *, include_end: bool) -> int:
        index = self.build_timeline_index()
        minimum = float(index["minimum_timestamp"])

        def selected(timestamp: float) -> bool:
            relative = timestamp - minimum
            return start_seconds <= relative and (relative <= end_seconds if include_end else relative < end_seconds)

        return sum(1 for timestamp in self._timestamps() if selected(timestamp))

    def expected_request_count(
        self,
        trace: SimulationTrace,
        duration_seconds: float,
        *,
        include_end: bool,
    ) -> int:
        end_seconds = (
            trace.end_seconds
            if trace.end_seconds is not None
            else trace.start_seconds + duration_seconds * trace.synthesis_speedup_ratio
        )
        return self.count_requests(trace.start_seconds, end_seconds, include_end=include_end)

    def arrival_schedule(
        self,
        *,
        start_seconds: float,
        end_seconds: float,
        speedup_ratio: float = 1.0,
        include_end: bool = False,
    ) -> list[float]:
        """Relative arrival times (seconds) for every request whose source timestamp
        falls within the selected window.

        Times are offset by ``start_seconds`` and scaled by ``speedup_ratio`` so they
        map onto simulation wall-clock time. This includes requests that were never
        completed, which per-request backend output omits.
        """
        index = self.build_timeline_index()
        minimum = float(index["minimum_timestamp"])
        ratio = speedup_ratio if speedup_ratio and speedup_ratio > 0 else 1.0
        arrivals: list[float] = []
        for timestamp in self._timestamps():
            relative = timestamp - minimum
            if start_seconds <= relative and (relative <= end_seconds if include_end else relative < end_seconds):
                arrivals.append((relative - start_seconds) / ratio)
        return arrivals

    def validate(self) -> None:
        first_line = None
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    first_line = line
                    break
        if first_line is None:
            raise ValueError("trace file is empty")
        if first_line.startswith("version https://git-lfs.github.com/spec"):
            raise ValueError("download returned a Git LFS pointer instead of trace data")
        record = json.loads(first_line)
        if not isinstance(record, dict):
            raise TypeError("trace first record must be a JSON object")
        missing = [key for key in self.required_fields if key not in record]
        if missing:
            raise ValueError(f"{self.format} first record is missing: {', '.join(missing)}")


class TraceRegistry:
    def __init__(self) -> None:
        self._formats: dict[str, type[BaseTrace]] = {}
        self._variants: dict[str, type[BaseTrace]] = {}

    def register_format(self, trace_type: type[BaseTrace]) -> type[BaseTrace]:
        if not issubclass(trace_type, BaseTrace):
            raise TypeError("Registered trace formats must inherit BaseTrace")
        if not trace_type.format:
            raise ValueError("Trace format must not be empty")
        if trace_type.format in self._formats:
            raise ValueError(f"Trace format '{trace_type.format}' is already registered")
        self._formats[trace_type.format] = trace_type
        return trace_type

    def register(self, trace_type: type[BaseTrace]) -> type[BaseTrace]:
        if not issubclass(trace_type, BaseTrace):
            raise TypeError("Registered trace variants must inherit BaseTrace")
        name = trace_type.__dict__.get("name")
        if not name:
            raise ValueError("Registered trace variants must define a name")
        if name in self._variants:
            raise ValueError(f"Trace dataset '{name}' is already registered")
        format_type = self._formats.get(trace_type.format)
        if format_type is None:
            raise ValueError(f"Trace format '{trace_type.format}' must be registered before its variants")
        if not issubclass(trace_type, format_type):
            raise TypeError(f"Trace variant '{name}' must inherit {format_type.__name__}")
        self._variants[name] = trace_type
        return trace_type

    def get_type(self, trace_format: str) -> type[BaseTrace]:
        trace_type = self._formats.get(trace_format)
        if trace_type is None:
            raise SimulationConfigurationError(f"Unsupported trace format '{trace_format}'")
        return trace_type

    def get_variant(self, name: str) -> type[BaseTrace]:
        trace_type = self._variants.get(name)
        if trace_type is None:
            raise SimulationConfigurationError(f"Unknown trace dataset '{name}'")
        return trace_type

    def get(self, name: str) -> dict:
        return self.get_variant(name).catalog_entry()

    def list(self) -> list[dict]:
        return [self.get(name) for name in sorted(self._variants)]

    def _variant_for_source(self, source: str, trace_format: str) -> type[BaseTrace] | None:
        source_name = Path(source).name
        for trace_type in self._variants.values():
            if trace_type.format != trace_format:
                continue
            if source == trace_type.filename or source_name == trace_type.filename:
                return trace_type
        return None

    def resolve(self, source: str, trace_format: str) -> BaseTrace:
        trace_type = self._variant_for_source(source, trace_format) or self.get_type(trace_format)
        return trace_type.resolve_source(source)

    def from_path(self, path: Path, trace_format: str) -> BaseTrace:
        trace_type = self._variant_for_source(str(path), trace_format) or self.get_type(trace_format)
        if not trace_type.file_backed:
            raise SimulationConfigurationError(f"Trace format '{trace_format}' is not file-backed")
        return trace_type(path)

    def resolve_file(self, source: str, trace_format: str) -> BaseTrace:
        trace = self.resolve(source, trace_format)
        if not trace.file_backed:
            raise SimulationConfigurationError(f"Trace format '{trace_format}' is not file-backed")
        return trace


trace_registry = TraceRegistry()


@trace_registry.register_format
class MooncakeTrace(BaseTrace):
    format = "mooncake_trace"
    REVISION = "9fbd622256e7a26aeea9f038e316eeb58203c102"
    REPOSITORY = "https://github.com/kvcache-ai/Mooncake"
    source_repository = REPOSITORY
    timestamp_divisor = 1000.0

    source_path: ClassVar[str]

    @classmethod
    def catalog_entry(cls) -> dict[str, Any]:
        return {
            **super().catalog_entry(),
            "url": f"https://raw.githubusercontent.com/kvcache-ai/Mooncake/{cls.REVISION}/{cls.source_path}",
            "source_repository": cls.REPOSITORY,
        }


@trace_registry.register
class MooncakeArxivTrace(MooncakeTrace):
    name = "mooncake-arxiv"
    filename = "mooncake_trace.jsonl"
    scenario = "chat"
    source_path = "FAST25-release/arxiv-trace/mooncake_trace.jsonl"
    sha256 = "b434f1816a707f4bac697235588184ebc374c9907cb981bb65fb0643471fe711"
    license = "Apache-2.0"
    description = "Original Mooncake arXiv trace used by AIPerf documentation."


@trace_registry.register
class MooncakeConversationTrace(MooncakeTrace):
    name = "mooncake-conversation"
    filename = "conversation_trace.jsonl"
    scenario = "chat"
    source_path = "FAST25-release/traces/conversation_trace.jsonl"
    sha256 = "b8cbb061a85206d729d91cdc2981f43c9e0d99209dce588d3af5f7934408b9df"
    license = "Apache-2.0"
    description = "Mooncake FAST'25 conversation workload."


@trace_registry.register
class MooncakeToolAgentTrace(MooncakeTrace):
    name = "mooncake-toolagent"
    filename = "toolagent_trace.jsonl"
    scenario = "api-calling"
    source_path = "FAST25-release/traces/toolagent_trace.jsonl"
    sha256 = "48a2db1a13d3bc05e6330140c64f604ba366df20d3c9e128b5c35a01c1fa5f71"
    license = "Apache-2.0"
    description = "Mooncake FAST'25 tool and agent workload."


@trace_registry.register
class MooncakeSyntheticTrace(MooncakeTrace):
    name = "mooncake-synthetic"
    filename = "synthetic_trace.jsonl"
    scenario = "chat"
    source_path = "FAST25-release/traces/synthetic_trace.jsonl"
    sha256 = "bd070915a98fc0ed264d7cfef2ce746002eb3076a695ec31ba2674c0111ec131"
    license = "Apache-2.0"
    description = "Mooncake FAST'25 synthetic Poisson workload."


@trace_registry.register_format
class BailianTrace(BaseTrace):
    format = "bailian_trace"
    REVISION = "5f7439c51ec248a0c585f7d90a41a6f57773b912"
    REPOSITORY = "https://github.com/alibaba-edu/qwen-bailian-usagetraces-anon"
    source_repository = REPOSITORY
    timestamp_divisor = 1.0
    required_fields = (*BaseTrace.required_fields, "chat_id", "parent_chat_id", "turn")

    @classmethod
    def catalog_entry(cls) -> dict[str, Any]:
        return {
            **super().catalog_entry(),
            "url": (
                "https://media.githubusercontent.com/media/"
                f"alibaba-edu/qwen-bailian-usagetraces-anon/{cls.REVISION}/{cls.filename}"
            ),
            "source_repository": cls.REPOSITORY,
        }


@trace_registry.register
class BailianTraceA(BailianTrace):
    name = "bailian-trace-a"
    filename = "qwen_traceA_blksz_16.jsonl"
    scenario = "chat"
    sha256 = "07cedc9ed8aff301994ac68ed4aede8123b7603673575eeba9dd677de663db17"
    license = "Apache-2.0"
    description = "Alibaba Bailian anonymized to-C trace (about 56 MB)."


@trace_registry.register
class BailianTraceB(BailianTrace):
    name = "bailian-trace-b"
    filename = "qwen_traceB_blksz_16.jsonl"
    scenario = "api-calling"
    sha256 = "68e3f98e2d601d60d0abf4b89bc8a3654372abab7b1cde6373a13d0054379d59"
    license = "Apache-2.0"
    description = "Alibaba Bailian anonymized B2B trace (about 96 MB)."


@trace_registry.register
class BailianCoderTrace(BailianTrace):
    name = "bailian-coder"
    filename = "qwen_coder_blksz_16.jsonl"
    scenario = "coding"
    sha256 = "3d74974cebb7bccb69b1ca6ea210a44d9a3033443c0271ed015327284dea1aff"
    license = "Apache-2.0"
    description = "Alibaba Bailian anonymized code-generation trace (about 132 MB)."


@trace_registry.register
class BailianThinkingTrace(BailianTrace):
    name = "bailian-thinking"
    filename = "qwen_thinking_blksz_16.jsonl"
    scenario = "chat"
    sha256 = "41ac36d9d1b54d084eeb6b05e9d142ba9d02f8bf79cfc9fff418d8ab7cdee906"
    license = "Apache-2.0"
    description = "Alibaba Bailian anonymized thinking trace (about 28 MB)."


@trace_registry.register_format
class BasetenTrace(BaseTrace):
    format = "baseten_trace"

    def _timestamp_entries(self) -> Iterator[tuple[float, int | None]]:
        try:
            import pyarrow.parquet as pq
        except ImportError as error:
            raise SimulationConfigurationError("Baseten trace timelines require pyarrow") from error
        yield from (
            (float(value) / 1000, None)
            for value in pq.read_table(self.path, columns=["timestamp_start_unix_ms"])
            .column("timestamp_start_unix_ms")
            .to_pylist()
        )

    def validate(self) -> None:
        if self.path.stat().st_size < 8:
            raise ValueError("baseten_trace must be a Parquet file")
        with self.path.open("rb") as handle:
            head = handle.read(4)
            handle.seek(-4, os.SEEK_END)
            tail = handle.read(4)
        if head != b"PAR1" or tail != b"PAR1":
            raise ValueError("baseten_trace must be a Parquet file")

    @classmethod
    def generate(cls, destination: Path) -> bool:
        try:
            import pyarrow as pa
            import pyarrow.parquet as pq
        except ImportError as error:
            raise ValueError("Baseten trace generation requires pyarrow") from error
        request_count = 600
        start_ms = 1_700_000_000_000
        table = pa.table(
            {
                "timestamp_start_unix_ms": [start_ms + index * 1000 for index in range(request_count)],
                "prompt": [f"Explain request {index} in one concise paragraph." for index in range(request_count)],
                "input_tokens": [64 + index % 448 for index in range(request_count)],
                "output_tokens": [32 + index % 224 for index in range(request_count)],
                "provided_session_id": [f"session-{index // 3}" for index in range(request_count)],
            }
        )
        pq.write_table(table, destination, compression="zstd")
        return True


@trace_registry.register
class BasetenSyntheticTrace(BasetenTrace):
    name = "baseten-synthetic"
    filename = "baseten_synthetic_trace.parquet"
    scenario = "chat"
    license = "Apache-2.0"
    description = "Deterministic Baseten-compatible completion trace (10 minutes, 600 requests)."


@trace_registry.register_format
class BurstGPTTrace(BaseTrace):
    format = "burst_gpt_trace"
    SOURCE_URL = "https://github.com/HPMLL/BurstGPT/releases/download/v2.0/BurstGPT_1.csv"
    sha256 = "4bb3783693d0a435686fbfc885615d2349bd067239079fa4b749f2e679e12122"
    source_repository = "https://github.com/HPMLL/BurstGPT"
    log_type: ClassVar[str]

    def _timestamp_entries(self) -> Iterator[tuple[float, int | None]]:
        with self.path.open(newline="", encoding="utf-8-sig") as handle:
            for row in csv.DictReader(handle):
                if row.get("Timestamp"):
                    yield float(row["Timestamp"]), None

    def validate(self) -> None:
        with self.path.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            required = {"Timestamp", "Request tokens", "Response tokens"}
            if reader.fieldnames is None or not required.issubset(reader.fieldnames):
                raise ValueError("burst_gpt_trace is missing required CSV columns")
            try:
                row = next(reader)
            except StopIteration as error:
                raise ValueError("trace file is empty") from error
            float(row["Timestamp"])
            int(row["Request tokens"])
            int(row["Response tokens"])

    @classmethod
    def catalog_entry(cls) -> dict[str, Any]:
        return {
            **super().catalog_entry(),
            "url": cls.SOURCE_URL,
            "log_type": cls.log_type,
        }

    @classmethod
    def transform_download(
        cls,
        source: Path,
        destination: Path,
    ) -> Path:
        with source.open(newline="", encoding="utf-8-sig") as input_file:
            reader = csv.DictReader(input_file)
            required = {"Timestamp", "Request tokens", "Response tokens", "Log Type"}
            if reader.fieldnames is None or not required.issubset(reader.fieldnames):
                raise ValueError("BurstGPT CSV is missing required columns")
            with destination.open("x", newline="", encoding="utf-8") as output_file:
                writer = csv.DictWriter(output_file, fieldnames=reader.fieldnames)
                writer.writeheader()
                written = 0
                for row in reader:
                    if row["Log Type"] == cls.log_type:
                        writer.writerow(row)
                        written += 1
        if written == 0:
            raise ValueError(f"BurstGPT trace contains no {cls.log_type} rows")
        return destination


@trace_registry.register
class BurstGPTConversationTrace(BurstGPTTrace):
    name = "burstgpt-conversation"
    filename = "burstgpt_conversation.csv"
    scenario = "chat"
    log_type = "Conversation log"
    license = "CC-BY-4.0"
    description = "BurstGPT v2 conversation traffic, filtered from BurstGPT_1."


@trace_registry.register
class BurstGPTAPITrace(BurstGPTTrace):
    name = "burstgpt-api"
    filename = "burstgpt_api.csv"
    scenario = "api-calling"
    log_type = "API log"
    license = "CC-BY-4.0"
    description = "BurstGPT v2 API traffic, filtered from BurstGPT_1."


@trace_registry.register_format
class WekaPublicDatasetTrace(BaseTrace):
    format = "weka_public_dataset"
    file_backed = False
    externally_managed = True

    def __init__(self, dataset: str) -> None:
        self.dataset = dataset

    @classmethod
    def resolve_source(cls, source: str) -> WekaPublicDatasetTrace:
        trace = cls(source)
        trace.validate()
        return trace

    def validate(self) -> None:
        if not self.dataset.startswith("semianalysis_cc_traces_weka_"):
            raise SimulationConfigurationError("Invalid Weka public dataset")

    @classmethod
    def catalog_entry(cls) -> dict[str, Any]:
        return {
            "name": cls.name,
            "filename": cls.filename,
            "scenario": cls.scenario,
            "trace_format": cls.format,
            "source_repository": cls.source_repository,
            "description": cls.description,
            "public_dataset": cls.filename,
            "source_type": "external_dataset",
            "downloaded": True,
            "path": cls.filename,
            "size_bytes": None,
        }


@trace_registry.register
class WekaClaudeCodeTrace(WekaPublicDatasetTrace):
    name = "weka-claude-code"
    filename = "semianalysis_cc_traces_weka_no_subagents"
    scenario = "coding"
    source_repository = "https://huggingface.co/datasets/semianalysisai/cc-traces-weka-no-subagents-051826"
    description = (
        "SemiAnalysis Weka Claude Code traces with top-level agent turns and subagent blocks removed (98 traces)."
    )


@trace_registry.register
class WekaClaudeCodeSubagentsTrace(WekaPublicDatasetTrace):
    name = "weka-claude-code-subagents-256k"
    filename = "semianalysis_cc_traces_weka_with_subagents_256k"
    scenario = "coding"
    source_repository = "https://huggingface.co/datasets/semianalysisai/cc-traces-weka-062126-256k"
    description = (
        "SemiAnalysis Weka Claude Code traces preserving parent/subagent "
        "fan-out, capped at a 256K context (393 traces)."
    )


async def get_trace_timeline(name: str, bins: int) -> dict:
    try:
        dataset = trace_registry.get(name)
    except SimulationConfigurationError as error:
        raise KeyError(f"unknown trace dataset: {name}") from error
    trace = trace_registry.resolve_file(dataset["filename"], dataset["trace_format"])
    lock = _timeline_locks.setdefault(str(trace.path), asyncio.Lock())
    async with lock:
        timeline = await asyncio.to_thread(trace.timeline, bins)
    return {
        "dataset": name,
        "trace_format": dataset["trace_format"],
        **timeline,
    }
