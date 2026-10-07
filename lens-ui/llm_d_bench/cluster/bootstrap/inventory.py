"""Render a Kubespray-compatible Ansible inventory + group_vars overrides
from the node list collected in the bootstrap sub-wizard.

See ``docs/design/CLUSTER_BOOTSTRAP_DESIGN.md`` section 4.2. Pure/side-effect-free
(besides writing the rendered files to a caller-supplied directory) so it
can be unit tested without any real SSH/Ansible/Kubespray involved.
"""

from __future__ import annotations

import stat
from pathlib import Path

import yaml

from llm_d_bench.cluster.bootstrap.models import BootstrapNode

#: Pinned to match the Kubespray checkout this design targets (see
#: ``docs/design/CLUSTER_BOOTSTRAP_DESIGN.md`` section 4.1) so behaviour doesn't drift
#: silently if upstream changes its defaults.
KUBESPRAY_NETWORK_PLUGIN = "calico"


def render_inventory(nodes: list[BootstrapNode]) -> dict:
    """Build the Kubespray inventory as a plain dict (YAML-serializable).

    A node's ``roles`` decides which Kubespray groups it lands in --
    ``kube_node`` if "worker" is one of its roles, ``kube_control_plane`` +
    ``etcd`` if "control-plane" is one of its roles. **A node may (and
    commonly does, e.g. single-node/all-in-one or small clusters) belong to
    both groups at once** -- Kubespray has no problem with a host appearing
    in more than one group; it just needs to be schedulable
    (``kube_node`` membership) to actually run workload Pods, on top of
    whatever control-plane/etcd duties it also has.
    """
    if not nodes:
        raise ValueError("at least one node is required")
    control_planes = [node for node in nodes if node.is_control_plane]
    workers = [node for node in nodes if node.is_worker]
    if not control_planes:
        raise ValueError("at least one control-plane node is required")

    hosts: dict[str, dict] = {}
    for node in nodes:
        host_vars: dict[str, object] = {
            "ansible_host": node.host,
            "ansible_port": node.port,
            "ansible_user": node.username,
        }
        if node.private_key_path:
            host_vars["ansible_ssh_private_key_file"] = node.private_key_path
        # Password-auth nodes intentionally get *no* password field here --
        # the real secret is written to a separate ``host_vars/<name>.yml``
        # file (see ``render_host_vars``/``write_inventory`` below) instead
        # of this shared ``inventory.yml``, and only for the duration of
        # the ansible-playbook subprocess run.
        hosts[node.name] = host_vars

    return {
        "all": {
            "hosts": hosts,
            "children": {
                "kube_control_plane": {"hosts": {node.name: None for node in control_planes}},
                "kube_node": {"hosts": {node.name: None for node in workers}},
                "etcd": {"hosts": {node.name: None for node in control_planes}},
                "k8s_cluster": {"children": {"kube_control_plane": None, "kube_node": None}},
                "calico_rr": {"hosts": {}},
            },
        }
    }


#: Kubespray does *not* automatically append cluster-internal addresses to
#: ``no_proxy`` -- it just exports whatever the caller supplied, verbatim
#: (see its own ``roles/kubespray_defaults/defaults/main/main.yml``:
#: ``no_proxy: "{{ no_proxy | default('') }}"``). Without these, once
#: ``http_proxy``/``https_proxy`` are set on a node, *all* HTTPS traffic on
#: that node -- including ``kubectl``/kubelet/etcd talking to
#: ``127.0.0.1:6443`` or another node's IP -- gets routed through the
#: corporate proxy, which typically rejects it. That surfaces to users as
#: a confusing ``kubectl get pods: Unable to connect to the server:
#: Forbidden`` *after* an otherwise-successful bootstrap. So these are
#: defensively merged into ``no_proxy`` whenever a proxy is configured,
#: regardless of "auto" vs "custom" mode. CIDRs match Kubespray v2.31.0's
#: own un-overridden defaults (``kube_service_addresses``/
#: ``kube_pods_subnet``) -- see ``docs/design/CLUSTER_BOOTSTRAP_DESIGN.md`` §4.2.
_DEFAULT_NO_PROXY_ADDITIONS = [
    "localhost",
    "127.0.0.1",
    "10.233.0.0/18",  # kube_service_addresses (Kubespray default)
    "10.233.64.0/18",  # kube_pods_subnet (Kubespray default)
    "kubernetes",
    "kubernetes.default",
    "kubernetes.default.svc",
    "kubernetes.default.svc.cluster.local",
    ".svc",
    ".svc.cluster.local",
    ".cluster.local",
]


def _augment_no_proxy(proxy: dict[str, str] | None, nodes: list[BootstrapNode]) -> dict[str, str] | None:
    if not proxy or not (proxy.get("http_proxy") or proxy.get("https_proxy")):
        return proxy
    existing = [item.strip() for item in (proxy.get("no_proxy") or "").split(",") if item.strip()]
    merged = list(existing)
    for item in (*_DEFAULT_NO_PROXY_ADDITIONS, *(node.host for node in nodes)):
        if item not in merged:
            merged.append(item)
    return {**proxy, "no_proxy": ",".join(merged)}


def render_group_vars(proxy: dict[str, str] | None = None) -> dict:
    """``group_vars/k8s_cluster/k8s-cluster.yml`` overrides (see design §4.2).

    ``kubeconfig_localhost``/``kubectl_localhost`` make Kubespray write
    ``admin.conf`` under the inventory's own ``artifacts/`` directory on the
    machine running ``ansible-playbook`` (the Prism backend host itself),
    so no extra SSH round-trip back to a control-plane node is needed to
    fetch the kubeconfig.

    ``proxy`` (``{"http_proxy": ..., "https_proxy": ..., "no_proxy": ...}``,
    any key optional/absent) is merged in verbatim -- these are real
    Kubespray-recognised vars (see its ``inventory/sample/group_vars/all/
    all.yml``) that get exported as environment variables on the *target*
    nodes for OS package-manager calls and the ``container-engine/*``
    roles' direct-from-GitHub binary downloads (e.g. ``runc``). Without
    these, a target node that has no direct internet route (common behind
    a corporate proxy) fails those downloads outright -- see the "ansible-
    playbook exited with status 2 .../runc: Download_file" failure this
    was added to fix.

    ``override_system_hostname`` defaults to ``true`` in Kubespray's own
    ``roles/kubernetes/preinstall/defaults/main.yml``, which renames each
    target machine's actual OS hostname (``/etc/hostname`` + live
    ``hostname``) to match its inventory name (this node's ``name`` field
    here). That's surprising/unwanted for nodes that already have a
    meaningful hostname assigned by the operator/IT (e.g. matching DNS,
    monitoring, or other fleet tooling) -- Kubespray only needs a
    consistent *inventory* name, not to rewrite the machine's real
    hostname, so this is pinned to ``false``.
    """
    group_vars: dict = {
        "kube_network_plugin": KUBESPRAY_NETWORK_PLUGIN,
        "kubeconfig_localhost": True,
        "kubectl_localhost": False,
        "override_system_hostname": False,
    }
    if proxy:
        group_vars.update({key: value for key, value in proxy.items() if value})
    return group_vars


def render_host_vars(nodes: list[BootstrapNode]) -> dict[str, dict]:
    """Per-host var overrides that must *never* land in the shared
    ``inventory.yml`` -- currently just ``ansible_ssh_pass`` for nodes
    authenticating with a password rather than a private key (same
    precedence as ``render_inventory``: password is only used when no
    private key path is set). Returned as ``{host_name: vars}`` so the
    caller can write one ``host_vars/<name>.yml`` file per host -- Ansible
    auto-loads those from the inventory file's own directory, so this
    plain per-host YAML file is enough without any ``--extra-vars``
    plumbing on the command line (which would leak the password into
    process listings/shell history)."""
    return {
        node.name: {"ansible_ssh_pass": node.password} for node in nodes if not node.private_key_path and node.password
    }


def write_inventory(work_dir: Path, nodes: list[BootstrapNode], proxy: dict[str, str] | None = None) -> Path:
    """Write ``inventory.yml`` + ``group_vars/k8s_cluster/k8s-cluster.yml``
    (+ any password ``host_vars/<name>.yml`` files) under ``work_dir`` and
    return the inventory file path."""
    inventory_path = work_dir / "inventory.yml"
    group_vars_dir = work_dir / "group_vars" / "k8s_cluster"
    group_vars_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "artifacts").mkdir(parents=True, exist_ok=True)

    inventory_path.write_text(yaml.safe_dump(render_inventory(nodes), sort_keys=False))
    (group_vars_dir / "k8s-cluster.yml").write_text(
        yaml.safe_dump(render_group_vars(_augment_no_proxy(proxy, nodes)), sort_keys=False)
    )

    host_vars = render_host_vars(nodes)
    if host_vars:
        host_vars_dir = work_dir / "host_vars"
        host_vars_dir.mkdir(parents=True, exist_ok=True)
        for name, values in host_vars.items():
            host_vars_path = host_vars_dir / f"{name}.yml"
            host_vars_path.write_text(yaml.safe_dump(values, sort_keys=False))
            host_vars_path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0600, mirrors _materialize_key

    return inventory_path


def artifacts_kubeconfig_path(work_dir: Path) -> Path:
    return work_dir / "artifacts" / "admin.conf"
