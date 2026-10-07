"""Fast, read-only per-node preflight checks (design §3.2): SSH reachability,
passwordless sudo, a Kubernetes-compatible hostname, and supported OS -- run
once the user submits the node list, *before* the (much slower) real
Kubespray ``ansible-playbook`` run so obvious per-node problems surface in
seconds instead of minutes.

Deliberately reuses ``llm_d_bench.utils.ssh.remote_exec`` (paramiko) rather
than shelling out to Ansible for this cheap check.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass

from llm_d_bench.cluster.bootstrap.models import BootstrapNode, PreflightCheckItem
from llm_d_bench.utils.ssh import RemoteResult, SshTarget, remote_exec

#: Linux distributions/versions Kubespray v2.31.0 lists as supported (see
#: https://github.com/kubernetes-sigs/kubespray/blob/v2.31.0/README.md
#: "Supported Linux Distributions"). This intentionally mirrors *Kubespray's*
#: support matrix, not an arbitrary narrower Prism-only whitelist -- a node
#: only needs to run something Kubespray's ``cluster.yml`` playbook actually
#: knows how to bootstrap. Distros Kubespray marks "experimental" (Flatcar,
#: Fedora CoreOS, Kylin, UOS, openEuler, Amazon Linux 2) are deliberately
#: left out for now since they need distro-specific bootstrap steps beyond
#: what this MVP's inventory renders (see design §1.3 non-goals).
_SUPPORTED_OS_RULES: dict[str, re.Pattern[str]] = {
    "ubuntu": re.compile(r"^(22\.04|24\.04)$"),
    "debian": re.compile(r"^(11|12|13)$"),  # bullseye, bookworm, trixie
    "centos": re.compile(r"^(9|10)($|\.)"),  # CentOS Stream
    "rhel": re.compile(r"^(9|10)($|\.)"),
    "fedora": re.compile(r"^(39|40|41|42)$"),
    "opensuse-leap": re.compile(r"^15($|\.)"),
    "opensuse-tumbleweed": re.compile(r".*"),  # rolling release
    "ol": re.compile(r"^(9|10)($|\.)"),  # Oracle Linux
    "almalinux": re.compile(r"^(9|10)($|\.)"),
    "rocky": re.compile(r"^(9|10)($|\.)"),
}

_CHECK_TIMEOUT_SECONDS = 15.0


@dataclass(frozen=True)
class PreflightCheck:
    ok: bool
    error: str | None = None


def _target(node: BootstrapNode) -> SshTarget:
    return SshTarget(
        host=node.host,
        port=node.port,
        username=node.username,
        key_filename=node.private_key_path,
        password=node.password,
        host_key_fingerprint=node.host_key_fingerprint,
    )


def _os_release_pair(stdout: str) -> tuple[str, str] | None:
    values: dict[str, str] = {}
    for line in stdout.splitlines():
        if "=" not in line:
            continue
        key, _, raw_value = line.partition("=")
        values[key.strip()] = raw_value.strip().strip('"')
    os_id = values.get("ID", "").lower()
    version_id = values.get("VERSION_ID", "")
    if not os_id or not version_id:
        return None
    return os_id, version_id


def _is_supported_os(pair: tuple[str, str] | None) -> bool:
    if pair is None:
        return False
    os_id, version_id = pair
    pattern = _SUPPORTED_OS_RULES.get(os_id)
    return pattern is not None and pattern.match(version_id) is not None


#: Kubernetes node names must be a valid RFC 1123 DNS subdomain: one or more
#: dot-separated labels, each matching this pattern (lowercase alphanumeric,
#: '-', must start/end with alphanumeric), max 63 chars per label. See
#: https://kubernetes.io/docs/concepts/overview/working-with-objects/names/#dns-subdomain-names
_DNS_LABEL_RE = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")
#: Overall DNS subdomain length cap (RFC 1123), applied in addition to the
#: per-label ``_DNS_LABEL_RE`` check above.
_MAX_HOSTNAME_LENGTH = 253


def _is_valid_k8s_hostname(hostname: str) -> bool:
    """Whether ``hostname`` (as reported live by the target node's own
    ``hostname`` command) is already a valid Kubernetes node name.

    This matters specifically *because* ``render_group_vars`` pins
    ``override_system_hostname: false`` (Kubespray otherwise renames the
    machine's real hostname to match the inventory name) -- kubelet
    registers the Node object under the machine's actual, unmodified
    hostname, so it must already satisfy Kubernetes' naming rules
    (lowercase RFC 1123 DNS subdomain) on its own, or ``kubeadm join``/
    kubelet startup fails on that node with an otherwise cryptic error.
    """
    hostname = hostname.strip()
    if not hostname or len(hostname) > _MAX_HOSTNAME_LENGTH:
        return False
    return all(_DNS_LABEL_RE.match(label) for label in hostname.split("."))


#: Read-only shell probe for signs of a pre-existing Kubernetes install --
#: guarded with ``[ ... ] && ...`` / ``command -v`` so it always exits 0
#: (no matches just means an empty marker line) and never mutates the node.
_EXISTING_K8S_PROBE = (
    'existing=""; '
    '[ -f /etc/kubernetes/admin.conf ] && existing="$existing admin.conf"; '
    '[ -f /etc/kubernetes/manifests/kube-apiserver.yaml ] && existing="$existing kube-apiserver-manifest"; '
    'systemctl is-active --quiet kubelet 2>/dev/null && existing="$existing kubelet-active"; '
    'command -v kubeadm >/dev/null 2>&1 && existing="$existing kubeadm-binary"; '
    "echo EXISTING_K8S_MARKERS:$existing"
)


def _existing_k8s_markers(stdout: str) -> list[str]:
    for line in stdout.splitlines():
        if line.startswith("EXISTING_K8S_MARKERS:"):
            return line[len("EXISTING_K8S_MARKERS:") :].split()
    return []


def _run_check(target: SshTarget) -> list[PreflightCheckItem]:
    items: list[PreflightCheckItem] = []

    try:
        reachable = remote_exec(target, "true", timeout=_CHECK_TIMEOUT_SECONDS)
    except Exception as error:  # paramiko raises a variety of exception types
        items.append(
            PreflightCheckItem(
                id="ssh", label="SSH connectivity", status="failed", detail=f"SSH connection failed: {error}"
            )
        )
        items.append(
            PreflightCheckItem(
                id="sudo", label="Passwordless sudo", status="skipped", detail="skipped -- SSH connectivity failed"
            )
        )
        items.append(
            PreflightCheckItem(
                id="hostname",
                label="Kubernetes-compatible hostname",
                status="skipped",
                detail="skipped -- SSH connectivity failed",
            )
        )
        items.append(
            PreflightCheckItem(
                id="os", label="Supported OS", status="skipped", detail="skipped -- SSH connectivity failed"
            )
        )
        items.append(
            PreflightCheckItem(
                id="existing_k8s",
                label="No existing Kubernetes install",
                status="skipped",
                detail="skipped -- SSH connectivity failed",
            )
        )
        return items
    if reachable.returncode != 0:
        error_detail = reachable.stderr.strip() or "SSH command failed"
        items.append(PreflightCheckItem(id="ssh", label="SSH connectivity", status="failed", detail=error_detail))
        items.append(
            PreflightCheckItem(
                id="sudo", label="Passwordless sudo", status="skipped", detail="skipped -- SSH connectivity failed"
            )
        )
        items.append(
            PreflightCheckItem(
                id="hostname",
                label="Kubernetes-compatible hostname",
                status="skipped",
                detail="skipped -- SSH connectivity failed",
            )
        )
        items.append(
            PreflightCheckItem(
                id="os", label="Supported OS", status="skipped", detail="skipped -- SSH connectivity failed"
            )
        )
        items.append(
            PreflightCheckItem(
                id="existing_k8s",
                label="No existing Kubernetes install",
                status="skipped",
                detail="skipped -- SSH connectivity failed",
            )
        )
        return items
    items.append(
        PreflightCheckItem(
            id="ssh",
            label="SSH connectivity",
            status="passed",
            detail=f"connected as {target.username}@{target.host}:{target.port}",
        )
    )

    sudo: RemoteResult = remote_exec(target, "sudo -n true", timeout=_CHECK_TIMEOUT_SECONDS)
    if sudo.returncode != 0:
        items.append(
            PreflightCheckItem(
                id="sudo",
                label="Passwordless sudo",
                status="failed",
                detail="passwordless sudo is not available for this user",
            )
        )
    else:
        items.append(
            PreflightCheckItem(
                id="sudo", label="Passwordless sudo", status="passed", detail="passwordless sudo is available"
            )
        )

    hostname_result = remote_exec(target, "hostname", timeout=_CHECK_TIMEOUT_SECONDS)
    live_hostname = hostname_result.stdout.strip() if hostname_result.returncode == 0 else ""
    if hostname_result.returncode != 0 or not live_hostname:
        items.append(
            PreflightCheckItem(
                id="hostname",
                label="Kubernetes-compatible hostname",
                status="failed",
                detail="could not read this node's hostname",
            )
        )
    elif not _is_valid_k8s_hostname(live_hostname):
        items.append(
            PreflightCheckItem(
                id="hostname",
                label="Kubernetes-compatible hostname",
                status="failed",
                detail=(
                    f"hostname '{live_hostname}' is not a valid Kubernetes node name (must be a lowercase RFC 1123 "
                    "DNS subdomain: letters, digits, '-' or '.', each dot-separated label starting/ending with an "
                    "alphanumeric character, max 63 chars per label); this bootstrap keeps each node's existing "
                    "hostname rather than overriding it, so fix the hostname on the machine itself "
                    "(e.g. `hostnamectl set-hostname <name>`) before retrying"
                ),
            )
        )
    else:
        items.append(
            PreflightCheckItem(
                id="hostname",
                label="Kubernetes-compatible hostname",
                status="passed",
                detail=f"'{live_hostname}' is a valid node name",
            )
        )

    os_release = remote_exec(target, "cat /etc/os-release", timeout=_CHECK_TIMEOUT_SECONDS)
    pair = _os_release_pair(os_release.stdout) if os_release.returncode == 0 else None
    if os_release.returncode != 0:
        items.append(
            PreflightCheckItem(id="os", label="Supported OS", status="failed", detail="could not read /etc/os-release")
        )
    elif not _is_supported_os(pair):
        items.append(
            PreflightCheckItem(
                id="os",
                label="Supported OS",
                status="failed",
                detail=(
                    f"unsupported OS {pair}; Kubespray v2.31.0 supports Ubuntu 22.04/24.04, "
                    "Debian 11/12/13, CentOS Stream/RHEL 9/10, Fedora 39-42, "
                    "openSUSE Leap 15.x/Tumbleweed, Oracle Linux 9/10, AlmaLinux 9/10, Rocky Linux 9/10"
                ),
            )
        )
    else:
        os_id, version_id = pair
        items.append(
            PreflightCheckItem(
                id="os",
                label="Supported OS",
                status="passed",
                detail=f"{os_id} {version_id} is supported by Kubespray v2.31.0",
            )
        )

    existing_k8s = remote_exec(target, _EXISTING_K8S_PROBE, timeout=_CHECK_TIMEOUT_SECONDS)
    markers = _existing_k8s_markers(existing_k8s.stdout) if existing_k8s.returncode == 0 else []
    if markers:
        items.append(
            PreflightCheckItem(
                id="existing_k8s",
                label="No existing Kubernetes install",
                status="failed",
                detail=(
                    f"this node already has a Kubernetes installation (detected: {', '.join(markers)}); "
                    "bootstrapping a new cluster on top of an existing one is not supported -- remove the "
                    "existing installation first (e.g. `kubeadm reset`) or pick a clean node"
                ),
            )
        )
    else:
        items.append(
            PreflightCheckItem(
                id="existing_k8s",
                label="No existing Kubernetes install",
                status="passed",
                detail="no existing installation detected",
            )
        )

    return items


def _overall(items: list[PreflightCheckItem]) -> PreflightCheck:
    for item in items:
        if item.status != "passed":
            return PreflightCheck(ok=False, error=item.detail)
    return PreflightCheck(ok=True)


async def run_preflight(nodes: list[BootstrapNode]) -> list[BootstrapNode]:
    """Run the preflight check for every node concurrently and update each
    node's ``preflight_state``/``preflight_error``/``preflight_checks`` in
    place; returns the same list for convenience."""

    async def _check_one(node: BootstrapNode) -> None:
        node.preflight_state = "running"
        items = await asyncio.to_thread(_run_check, _target(node))
        node.preflight_checks = items
        result = _overall(items)
        node.preflight_state = "passed" if result.ok else "failed"
        node.preflight_error = result.error

    await asyncio.gather(*(_check_one(node) for node in nodes))
    return nodes
