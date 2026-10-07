"""Tests for the preflight OS-support matrix (design §3.2): it must mirror
Kubespray's actual "Supported Linux Distributions" list, not an arbitrary
narrower whitelist."""

from unittest.mock import patch

from llm_d_bench.cluster.bootstrap import preflight
from llm_d_bench.cluster.bootstrap.preflight import (
    _existing_k8s_markers,
    _is_supported_os,
    _is_valid_k8s_hostname,
    _run_check,
)
from llm_d_bench.utils.ssh import RemoteResult, SshTarget


def test_supported_ubuntu_versions_pass():
    assert _is_supported_os(("ubuntu", "22.04"))
    assert _is_supported_os(("ubuntu", "24.04"))


def test_unsupported_ubuntu_version_fails():
    assert not _is_supported_os(("ubuntu", "20.04"))


def test_rocky_linux_9_and_10_pass_but_8_fails():
    assert _is_supported_os(("rocky", "9.4"))
    assert _is_supported_os(("rocky", "10"))
    assert not _is_supported_os(("rocky", "8.10"))


def test_rhel_and_centos_stream_9_10_pass():
    assert _is_supported_os(("rhel", "9"))
    assert _is_supported_os(("centos", "9"))
    assert not _is_supported_os(("rhel", "7"))


def test_debian_bullseye_bookworm_trixie_pass():
    assert _is_supported_os(("debian", "11"))
    assert _is_supported_os(("debian", "12"))
    assert _is_supported_os(("debian", "13"))


def test_fedora_range_and_opensuse_variants():
    assert _is_supported_os(("fedora", "42"))
    assert not _is_supported_os(("fedora", "38"))
    assert _is_supported_os(("opensuse-leap", "15.6"))
    assert _is_supported_os(("opensuse-tumbleweed", "20240101"))


def test_oracle_and_alma_linux_pass():
    assert _is_supported_os(("ol", "9.3"))
    assert _is_supported_os(("almalinux", "9"))


def test_unknown_distro_fails():
    assert not _is_supported_os(("arch", "rolling"))


def test_none_pair_fails():
    assert not _is_supported_os(None)


def test_valid_k8s_hostnames_pass():
    assert _is_valid_k8s_hostname("node-1")
    assert _is_valid_k8s_hostname("worker01")
    assert _is_valid_k8s_hostname("node-1.example.com")
    assert _is_valid_k8s_hostname("a" * 63)  # exactly at the per-label limit


def test_invalid_k8s_hostnames_fail():
    assert not _is_valid_k8s_hostname("")
    assert not _is_valid_k8s_hostname("NODE-01")  # uppercase not allowed
    assert not _is_valid_k8s_hostname("node_01")  # underscore not allowed
    assert not _is_valid_k8s_hostname("-node-1")  # can't start with '-'
    assert not _is_valid_k8s_hostname("node-1-")  # can't end with '-'
    assert not _is_valid_k8s_hostname("a" * 64)  # exceeds the 63-char per-label limit
    assert not _is_valid_k8s_hostname("node..1")  # empty label


def test_existing_k8s_markers_parses_the_probe_output():
    assert _existing_k8s_markers("EXISTING_K8S_MARKERS:admin.conf kubelet-active") == [
        "admin.conf",
        "kubelet-active",
    ]


def test_existing_k8s_markers_empty_when_no_markers():
    assert _existing_k8s_markers("EXISTING_K8S_MARKERS:") == []


def test_existing_k8s_markers_missing_line_returns_empty():
    assert _existing_k8s_markers("some unrelated output\n") == []


def _stub_target() -> SshTarget:
    return SshTarget(host="10.0.0.5", port=22, username="root", key_filename=None)


def test_run_check_fails_when_node_already_has_kubernetes():
    responses = [
        RemoteResult(returncode=0, stdout="", stderr=""),  # reachable
        RemoteResult(returncode=0, stdout="", stderr=""),  # sudo -n true
        RemoteResult(returncode=0, stdout="node-1\n", stderr=""),  # hostname
        RemoteResult(returncode=0, stdout='ID=ubuntu\nVERSION_ID="22.04"\n', stderr=""),  # os-release
        RemoteResult(returncode=0, stdout="EXISTING_K8S_MARKERS:admin.conf kubeadm-binary", stderr=""),
    ]
    with patch.object(preflight, "remote_exec", side_effect=responses):
        items = _run_check(_stub_target())
    by_id = {item.id: item for item in items}
    assert by_id["ssh"].status == "passed"
    assert by_id["sudo"].status == "passed"
    assert by_id["hostname"].status == "passed"
    assert by_id["os"].status == "passed"
    assert by_id["existing_k8s"].status == "failed"
    assert "already has a Kubernetes installation" in by_id["existing_k8s"].detail
    assert "admin.conf" in by_id["existing_k8s"].detail


def test_run_check_passes_when_node_is_clean():
    responses = [
        RemoteResult(returncode=0, stdout="", stderr=""),  # reachable
        RemoteResult(returncode=0, stdout="", stderr=""),  # sudo -n true
        RemoteResult(returncode=0, stdout="node-1\n", stderr=""),  # hostname
        RemoteResult(returncode=0, stdout='ID=ubuntu\nVERSION_ID="22.04"\n', stderr=""),  # os-release
        RemoteResult(returncode=0, stdout="EXISTING_K8S_MARKERS:", stderr=""),
    ]
    with patch.object(preflight, "remote_exec", side_effect=responses):
        items = _run_check(_stub_target())
    assert [item.id for item in items] == ["ssh", "sudo", "hostname", "os", "existing_k8s"]
    assert all(item.status == "passed" for item in items)


def test_run_check_fails_when_hostname_is_not_kubernetes_compatible():
    # An uppercase/underscore hostname is a common real-world case (e.g. a
    # Windows-style or legacy asset-tag hostname) -- since this bootstrap no
    # longer lets Kubespray rename it (override_system_hostname: false), it
    # must be flagged here rather than surfacing as an opaque kubelet/
    # kubeadm failure much later.
    responses = [
        RemoteResult(returncode=0, stdout="", stderr=""),  # reachable
        RemoteResult(returncode=0, stdout="", stderr=""),  # sudo -n true
        RemoteResult(returncode=0, stdout="NODE_01\n", stderr=""),  # hostname
        RemoteResult(returncode=0, stdout='ID=ubuntu\nVERSION_ID="22.04"\n', stderr=""),  # os-release
        RemoteResult(returncode=0, stdout="EXISTING_K8S_MARKERS:", stderr=""),
    ]
    with patch.object(preflight, "remote_exec", side_effect=responses):
        items = _run_check(_stub_target())
    by_id = {item.id: item for item in items}
    assert by_id["hostname"].status == "failed"
    assert "NODE_01" in by_id["hostname"].detail
    assert "not a valid Kubernetes node name" in by_id["hostname"].detail


def test_run_check_skips_remaining_checks_when_ssh_unreachable():
    responses = [RemoteResult(returncode=255, stdout="", stderr="Permission denied")]
    with patch.object(preflight, "remote_exec", side_effect=responses):
        items = _run_check(_stub_target())
    by_id = {item.id: item for item in items}
    assert by_id["ssh"].status == "failed"
    assert by_id["sudo"].status == "skipped"
    assert by_id["hostname"].status == "skipped"
    assert by_id["os"].status == "skipped"
    assert by_id["existing_k8s"].status == "skipped"
