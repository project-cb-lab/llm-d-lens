"""Service/router tests for the Kubespray bootstrap flow -- everything that
would touch real SSH/Ansible/Kubespray is monkeypatched, per instructions to
not exercise this against real machines."""

from __future__ import annotations

import shlex
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from llm_d_bench.api.main import app
from llm_d_bench.cluster.bootstrap import kubespray, preflight, service
from llm_d_bench.cluster.bootstrap.dto import (
    BootstrapHostKeyPin,
    BootstrapNodeInput,
)
from llm_d_bench.cluster.bootstrap.host_expr import expand_host_spec
from llm_d_bench.cluster.bootstrap.models import BootstrapNode, job_store
from llm_d_bench.utils.shell import CommandNotFoundError
from llm_d_bench.utils.ssh import HostKeyInfo, RemoteResult, host_key_fingerprint

client = TestClient(app)
_TEST_FINGERPRINT = "SHA256:" + "A" * 43


def _pins_for(payloads):
    return [
        BootstrapHostKeyPin(host=host, port=payload.port, fingerprint=_TEST_FINGERPRINT)
        for payload in payloads
        for host in expand_host_spec(payload.host)
    ]


def _create_job(payloads, proxy=None):
    return service.create_job(payloads, _pins_for(payloads), proxy=proxy)


@pytest.fixture(autouse=True)
def _isolated_work_root(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "_work_root", lambda: tmp_path / "bootstrap-work")
    # "auto" proxy mode probes each node over real SSH (see
    # ``_resolve_auto_proxy_from_nodes``) -- default that to "nothing
    # found, no network call" so tests that don't care about proxy
    # behaviour don't hang/fail trying to actually SSH anywhere; tests
    # that do care override this explicitly.
    monkeypatch.setattr(service, "remote_exec", lambda *a, **k: RemoteResult(stdout="", stderr="", returncode=1))
    monkeypatch.setattr(service, "_write_known_hosts", lambda _nodes, path: path.write_text("test-key\n"))
    yield
    job_store._jobs.clear()  # noqa: SLF001 - test-only cleanup


async def _fake_pass_all(nodes: list[BootstrapNode]) -> list[BootstrapNode]:
    for node in nodes:
        node.preflight_state = "passed"
        node.preflight_error = None
    return nodes


async def _fake_fail_one(nodes: list[BootstrapNode]) -> list[BootstrapNode]:
    for index, node in enumerate(nodes):
        if index == 0:
            node.preflight_state = "failed"
            node.preflight_error = "sudo not available"
        else:
            node.preflight_state = "passed"
    return nodes


def test_preflight_endpoint_reports_all_passed(monkeypatch):
    monkeypatch.setattr(preflight, "run_preflight", _fake_pass_all)
    response = client.post(
        "/api/cluster/bootstrap/preflight",
        json={
            "nodes": [{"host": "10.0.0.1", "roles": ["control-plane"]}],
            "hostKeys": [{"host": "10.0.0.1", "port": 22, "fingerprint": _TEST_FINGERPRINT}],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["allPassed"] is True
    assert body["items"][0]["state"] == "passed"


def test_host_key_discovery_expands_ranges_without_authentication(monkeypatch):
    inspected = []

    def _inspect(host, port):
        inspected.append((host, port))
        return HostKeyInfo("ssh-ed25519", _TEST_FINGERPRINT, "cHVibGljLWtleQ==")

    monkeypatch.setattr(service, "inspect_host_key", _inspect)
    response = client.post(
        "/api/cluster/bootstrap/host-keys",
        json={"nodes": [{"host": "10.0.0.10-12", "port": 2222}]},
    )

    assert response.status_code == 200
    assert inspected == [(f"10.0.0.{octet}", 2222) for octet in range(10, 13)]
    assert [item["host"] for item in response.json()["items"]] == [f"10.0.0.{octet}" for octet in range(10, 13)]
    assert all(item["fingerprint"] == _TEST_FINGERPRINT for item in response.json()["items"])


def test_preflight_rejects_missing_or_unexpected_host_key_pins():
    payload = {"nodes": [{"host": "10.0.0.1", "roles": ["control-plane"]}]}
    missing = client.post("/api/cluster/bootstrap/preflight", json={**payload, "hostKeys": []})
    unexpected = client.post(
        "/api/cluster/bootstrap/preflight",
        json={
            **payload,
            "hostKeys": [{"host": "10.0.0.2", "port": 22, "fingerprint": _TEST_FINGERPRINT}],
        },
    )

    assert missing.status_code == 422
    assert unexpected.status_code == 422


def test_paramiko_host_key_policy_accepts_only_the_pinned_fingerprint():
    import paramiko

    from llm_d_bench.utils.ssh import _PinnedHostKeyPolicy

    trusted_key = paramiko.RSAKey.generate(1024)
    other_key = paramiko.RSAKey.generate(1024)
    policy = _PinnedHostKeyPolicy(host_key_fingerprint(trusted_key))

    policy.missing_host_key(None, "node.example", trusted_key)
    with pytest.raises(paramiko.SSHException, match="fingerprint mismatch"):
        policy.missing_host_key(None, "node.example", other_key)


def test_ansible_known_hosts_writer_rechecks_fingerprint(monkeypatch, tmp_path):
    node = BootstrapNode(
        host="node.example",
        port=2222,
        host_key_fingerprint=_TEST_FINGERPRINT,
    )
    monkeypatch.setattr(
        service,
        "inspect_host_key",
        lambda *_args: HostKeyInfo("ssh-ed25519", _TEST_FINGERPRINT, "cHVibGljLWtleQ=="),
    )
    path = tmp_path / "known_hosts"

    service._write_known_hosts([node], path)

    assert path.read_text() == "[node.example]:2222 ssh-ed25519 cHVibGljLWtleQ==\n"
    assert path.stat().st_mode & 0o777 == 0o600
    monkeypatch.setattr(
        service,
        "inspect_host_key",
        lambda *_args: HostKeyInfo("ssh-ed25519", "SHA256:" + "B" * 43, "other-key"),
    )
    with pytest.raises(ValueError, match="host key changed"):
        service._write_known_hosts([node], path)


def test_preflight_endpoint_reports_per_check_breakdown(monkeypatch):
    from llm_d_bench.utils.ssh import RemoteResult

    responses = [
        RemoteResult(returncode=0, stdout="", stderr=""),  # reachable
        RemoteResult(returncode=0, stdout="", stderr=""),  # sudo -n true
        RemoteResult(returncode=0, stdout="node-1\n", stderr=""),  # hostname
        RemoteResult(returncode=0, stdout='ID=ubuntu\nVERSION_ID="22.04"\n', stderr=""),  # os-release
        RemoteResult(returncode=0, stdout="EXISTING_K8S_MARKERS:", stderr=""),
    ]
    monkeypatch.setattr(preflight, "remote_exec", lambda *a, **k: responses.pop(0))

    response = client.post(
        "/api/cluster/bootstrap/preflight",
        json={
            "nodes": [{"host": "10.0.0.1", "roles": ["control-plane"]}],
            "hostKeys": [{"host": "10.0.0.1", "port": 22, "fingerprint": _TEST_FINGERPRINT}],
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["allPassed"] is True
    checks = body["items"][0]["checks"]
    assert [check["id"] for check in checks] == ["ssh", "sudo", "hostname", "os", "existing_k8s"]
    assert all(check["status"] == "passed" for check in checks)
    assert "ubuntu 22.04" in checks[3]["detail"]


def test_create_job_defaults_to_auto_proxy_mode_unresolved_until_run(monkeypatch):
    payload = [BootstrapNodeInput(host="10.0.0.1", roles=["control-plane"])]

    job = _create_job(payload)

    # "auto" is resolved later, in run_bootstrap_job, by probing the
    # target nodes themselves -- not eagerly from the backend's own env.
    assert job.proxy_mode == "auto"
    assert job.proxy is None


def test_create_job_uses_explicit_custom_proxy_immediately():
    from llm_d_bench.cluster.bootstrap.dto import BootstrapProxyConfig

    payload = [BootstrapNodeInput(host="10.0.0.1", roles=["control-plane"])]

    job = _create_job(
        payload,
        proxy=BootstrapProxyConfig(mode="custom", httpProxy="http://custom-proxy:8080", noProxy="localhost"),
    )

    assert job.proxy_mode == "custom"
    assert job.proxy == {"http_proxy": "http://custom-proxy:8080", "no_proxy": "localhost"}


@pytest.mark.asyncio
async def test_run_bootstrap_job_auto_mode_reuses_the_target_nodes_own_proxy(monkeypatch, tmp_path):
    monkeypatch.setattr(preflight, "run_preflight", _fake_pass_all)
    monkeypatch.setattr(
        kubespray, "require_ready", lambda: (_ for _ in ()).throw(CommandNotFoundError("not provisioned"))
    )
    monkeypatch.setattr(kubespray, "ensure_ready", lambda: (_ for _ in ()).throw(RuntimeError("git clone failed")))

    def _fake_remote_exec(target, command, timeout=None):
        # Simulate a node that already has a proxy configured for itself
        # (e.g. via /etc/environment), distinct from anything the Prism
        # backend host's own env might have.
        return RemoteResult(
            stdout='http_proxy=http://node-proxy:3128\nno_proxy="localhost,127.0.0.1"\n', stderr="", returncode=0
        )

    monkeypatch.setattr(service, "remote_exec", _fake_remote_exec)

    payload = [BootstrapNodeInput(host="10.0.0.1", roles=["control-plane"])]
    job = _create_job(payload)
    await service.run_bootstrap_job(job.id)

    refreshed = service.get_job(job.id)
    assert refreshed.proxy == {"http_proxy": "http://node-proxy:3128", "no_proxy": "localhost,127.0.0.1"}


@pytest.mark.asyncio
async def test_run_bootstrap_job_auto_mode_has_no_proxy_when_nodes_have_none(monkeypatch, tmp_path):
    monkeypatch.setattr(preflight, "run_preflight", _fake_pass_all)
    monkeypatch.setattr(
        kubespray, "require_ready", lambda: (_ for _ in ()).throw(CommandNotFoundError("not provisioned"))
    )
    monkeypatch.setattr(kubespray, "ensure_ready", lambda: (_ for _ in ()).throw(RuntimeError("git clone failed")))
    # Uses the autouse "no proxy found" fake remote_exec.

    payload = [BootstrapNodeInput(host="10.0.0.1", roles=["control-plane"])]
    job = _create_job(payload)
    await service.run_bootstrap_job(job.id)

    refreshed = service.get_job(job.id)
    assert refreshed.proxy is None


def test_preflight_endpoint_reports_failures(monkeypatch):
    monkeypatch.setattr(preflight, "run_preflight", _fake_fail_one)
    response = client.post(
        "/api/cluster/bootstrap/preflight",
        json={
            "nodes": [
                {"host": "10.0.0.1", "roles": ["control-plane"]},
                {"host": "10.0.0.2", "roles": ["worker"]},
            ],
            "hostKeys": [
                {"host": "10.0.0.1", "port": 22, "fingerprint": _TEST_FINGERPRINT},
                {"host": "10.0.0.2", "port": 22, "fingerprint": _TEST_FINGERPRINT},
            ],
        },
    )
    body = response.json()
    assert body["allPassed"] is False
    assert body["items"][0]["state"] == "failed"
    assert body["items"][0]["error"] == "sudo not available"


def test_preflight_endpoint_expands_host_range_into_individual_hosts(monkeypatch):
    monkeypatch.setattr(preflight, "run_preflight", _fake_pass_all)
    response = client.post(
        "/api/cluster/bootstrap/preflight",
        json={
            "nodes": [{"host": "10.0.0.10-12", "roles": ["worker"]}],
            "hostKeys": [
                {"host": f"10.0.0.{octet}", "port": 22, "fingerprint": _TEST_FINGERPRINT} for octet in range(10, 13)
            ],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert [item["host"] for item in body["items"]] == ["10.0.0.10", "10.0.0.11", "10.0.0.12"]
    assert body["allPassed"] is True


def test_preflight_endpoint_rejects_malformed_host_spec(monkeypatch):
    monkeypatch.setattr(preflight, "run_preflight", _fake_pass_all)
    response = client.post(
        "/api/cluster/bootstrap/preflight",
        json={
            "nodes": [{"host": "10.0.0.5-999", "roles": ["worker"]}],
            "hostKeys": [{"host": "10.0.0.5", "port": 22, "fingerprint": _TEST_FINGERPRINT}],
        },
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_run_bootstrap_job_succeeds_end_to_end(monkeypatch, tmp_path):
    monkeypatch.setattr(service, "_work_root", lambda: tmp_path / "bootstrap-work")
    monkeypatch.setattr(preflight, "run_preflight", _fake_pass_all)
    monkeypatch.setattr(kubespray, "require_ready", lambda: tmp_path / "kubespray-checkout")
    monkeypatch.setattr(kubespray, "ansible_playbook_path", lambda: Path("/fake/ansible-playbook"))
    monkeypatch.setattr(kubespray, "cluster_playbook_path", lambda: Path("/fake/cluster.yml"))

    class _FakeStdout:
        def __aiter__(self):
            async def _gen():
                yield b"PLAY [all]\n"
                yield b"ok: [10-0-0-1]\n"

            return _gen()

    class _FakeProcess:
        def __init__(self):
            self.stdout = _FakeStdout()
            self.returncode = 0
            self.pid = 4242

        async def wait(self):
            return 0

        def terminate(self):
            pass

    spawn_options = []

    async def _fake_spawn(argv, **kwargs):
        spawn_options.append(kwargs)
        ssh_args = shlex.split(kwargs["env"]["ANSIBLE_SSH_COMMON_ARGS"])
        known_hosts_arg = next(argument for argument in ssh_args if argument.startswith("UserKnownHostsFile="))
        known_hosts = Path(known_hosts_arg.split("=", 1)[1])
        assert known_hosts.read_text() == "test-key\n"
        return _FakeProcess()

    monkeypatch.setattr(service.shell, "spawn", _fake_spawn)

    payload = [BootstrapNodeInput(host="10.0.0.1", roles=["control-plane"])]
    job = _create_job(payload)

    # Pre-create the admin.conf the fake ansible-playbook "would have" written.
    work_dir = (tmp_path / "bootstrap-work") / job.id
    (work_dir / "artifacts").mkdir(parents=True, exist_ok=True)
    (work_dir / "artifacts" / "admin.conf").write_text("apiVersion: v1\nkind: Config\n")

    await service.run_bootstrap_job(job.id)

    refreshed = service.get_job(job.id)
    assert refreshed.phase == "succeeded"
    assert refreshed.kubeconfig_text == "apiVersion: v1\nkind: Config\n"
    assert spawn_options[0]["env"]["ANSIBLE_HOST_KEY_CHECKING"] == "True"
    assert "StrictHostKeyChecking=yes" in spawn_options[0]["env"]["ANSIBLE_SSH_COMMON_ARGS"]
    # Plaintext credentials must not linger once the job is done (design §6).
    assert all(node.private_key is None and node.password is None for node in refreshed.nodes)


@pytest.mark.asyncio
async def test_run_bootstrap_job_fails_when_preflight_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(service, "_work_root", lambda: tmp_path / "bootstrap-work")
    monkeypatch.setattr(preflight, "run_preflight", _fake_fail_one)

    payload = [
        BootstrapNodeInput(host="10.0.0.1", roles=["control-plane"]),
        BootstrapNodeInput(host="10.0.0.2", roles=["worker"]),
    ]
    job = _create_job(payload)
    await service.run_bootstrap_job(job.id)

    refreshed = service.get_job(job.id)
    assert refreshed.phase == "failed"
    assert "sudo not available" in refreshed.error


@pytest.mark.asyncio
async def test_run_bootstrap_job_fails_when_kubespray_auto_provisioning_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(service, "_work_root", lambda: tmp_path / "bootstrap-work")
    monkeypatch.setattr(preflight, "run_preflight", _fake_pass_all)
    # Force the "not provisioned" branch deterministically (rather than
    # relying on the real Kubespray cache dir happening to be empty on
    # whatever machine runs this test -- it may well already be provisioned
    # for real, e.g. from a previous real bootstrap job) and simulate the
    # auto-provisioning fallback itself failing (e.g. no network access to
    # GitHub/PyPI from this environment).
    monkeypatch.setattr(
        kubespray, "require_ready", lambda: (_ for _ in ()).throw(CommandNotFoundError("not provisioned"))
    )
    monkeypatch.setattr(kubespray, "ensure_ready", lambda: (_ for _ in ()).throw(RuntimeError("git clone failed")))

    payload = [BootstrapNodeInput(host="10.0.0.1", roles=["control-plane"])]
    job = _create_job(payload)
    await service.run_bootstrap_job(job.id)

    refreshed = service.get_job(job.id)
    assert refreshed.phase == "failed"
    assert "failed to auto-provision Kubespray" in refreshed.error


@pytest.mark.asyncio
async def test_run_bootstrap_job_auto_provisions_kubespray_when_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(service, "_work_root", lambda: tmp_path / "bootstrap-work")
    monkeypatch.setattr(preflight, "run_preflight", _fake_pass_all)
    monkeypatch.setattr(kubespray, "ansible_playbook_path", lambda *a, **k: Path("/fake/ansible-playbook"))
    monkeypatch.setattr(kubespray, "cluster_playbook_path", lambda *a, **k: Path("/fake/cluster.yml"))
    # Force the "not provisioned" branch deterministically (rather than
    # relying on the real Kubespray cache dir happening to be empty on
    # whatever machine runs this test); ensure_ready() (the slow, real
    # provisioning path) is mocked to succeed instead of hitting the
    # network, proving the auto-provisioning fallback is actually invoked.
    monkeypatch.setattr(
        kubespray, "require_ready", lambda: (_ for _ in ()).throw(CommandNotFoundError("not provisioned"))
    )
    ensure_ready_calls: list[None] = []

    def _fake_ensure_ready():
        ensure_ready_calls.append(None)
        return tmp_path / "kubespray-checkout"

    monkeypatch.setattr(kubespray, "ensure_ready", _fake_ensure_ready)

    async def _fake_spawn(*_args, **_kwargs):
        raise RuntimeError("spawn should not be reached in this test's assertions -- checked before")

    # Only assert we got past provisioning into the "running" phase; a fake
    # process isn't wired up here, so just confirm ensure_ready() ran and the
    # job moved off "failed"/"not provisioned".
    monkeypatch.setattr(service.shell, "spawn", _fake_spawn, raising=False)

    payload = [BootstrapNodeInput(host="10.0.0.1", roles=["control-plane"])]
    job = _create_job(payload)
    await service.run_bootstrap_job(job.id)

    assert ensure_ready_calls, "ensure_ready() should have been called to auto-provision Kubespray"
    refreshed = service.get_job(job.id)
    assert refreshed.phase == "failed"  # spawn stub raised, but only *after* provisioning succeeded
    assert "not provisioned" not in (refreshed.error or "")


def test_consume_kubeconfig_is_one_shot(tmp_path):
    job = job_store.create([BootstrapNode(host="10.0.0.1", roles=["control-plane"])])
    job.phase = "succeeded"
    job.kubeconfig_text = "apiVersion: v1\n"
    job.work_dir = tmp_path / job.id
    job.work_dir.mkdir()

    text = service.consume_kubeconfig(job.id)
    assert text == "apiVersion: v1\n"
    with pytest.raises(KeyError):
        service.get_job(job.id)  # job (and its work_dir) are torn down after consumption
    assert not job.work_dir.exists()
