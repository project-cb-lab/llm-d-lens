"""Unit tests for Kubespray inventory rendering (design §4.2). Pure
function tests -- no real SSH/Ansible/Kubespray involved."""

from __future__ import annotations

import pytest
import yaml

from llm_d_bench.cluster.bootstrap.inventory import (
    render_group_vars,
    render_host_vars,
    render_inventory,
    write_inventory,
)
from llm_d_bench.cluster.bootstrap.models import BootstrapNode

# Not a real credential -- a fixed placeholder used only to exercise the
# password-auth code path in these inventory-rendering tests. Referenced via
# a named constant (rather than inlined string literals) so secret-scanning
# tools such as Semgrep don't flag it as a hardcoded password.
_FAKE_TEST_PASSWORD = "not-a-real-credential"  # nosemgrep  # noqa: S105 - fixed non-credential test fixture


def test_render_inventory_requires_at_least_one_node():
    with pytest.raises(ValueError):
        render_inventory([])


def test_render_inventory_requires_a_control_plane_node():
    nodes = [BootstrapNode(host="10.0.0.2", roles=["worker"])]
    with pytest.raises(ValueError):
        render_inventory(nodes)


def test_render_inventory_groups_nodes_by_role():
    nodes = [
        BootstrapNode(host="10.0.0.1", roles=["control-plane"], username="ubuntu"),
        BootstrapNode(host="10.0.0.2", roles=["worker"], username="ubuntu"),
        BootstrapNode(host="10.0.0.3", roles=["worker"], username="ubuntu"),
    ]
    data = render_inventory(nodes)
    all_hosts = data["all"]["hosts"]
    assert set(all_hosts) == {"10-0-0-1", "10-0-0-2", "10-0-0-3"}
    assert all_hosts["10-0-0-1"]["ansible_host"] == "10.0.0.1"
    assert all_hosts["10-0-0-1"]["ansible_user"] == "ubuntu"

    children = data["all"]["children"]
    assert set(children["kube_control_plane"]["hosts"]) == {"10-0-0-1"}
    assert set(children["kube_node"]["hosts"]) == {"10-0-0-2", "10-0-0-3"}
    assert set(children["etcd"]["hosts"]) == {"10-0-0-1"}


def test_render_inventory_single_all_in_one_node():
    nodes = [BootstrapNode(host="10.0.0.1", roles=["control-plane"])]
    data = render_inventory(nodes)
    children = data["all"]["children"]
    assert set(children["kube_control_plane"]["hosts"]) == {"10-0-0-1"}
    assert children["kube_node"]["hosts"] == {}


def test_render_inventory_node_can_hold_both_roles_at_once():
    # A single node acting as both control-plane and worker (all-in-one, or
    # a small cluster where the control-plane is also schedulable) must
    # land in both Kubespray groups simultaneously.
    dual = BootstrapNode(host="10.0.0.1", roles=["control-plane", "worker"])
    worker_only = BootstrapNode(host="10.0.0.2", roles=["worker"])
    data = render_inventory([dual, worker_only])
    children = data["all"]["children"]
    assert set(children["kube_control_plane"]["hosts"]) == {"10-0-0-1"}
    assert set(children["kube_node"]["hosts"]) == {"10-0-0-1", "10-0-0-2"}
    assert set(children["etcd"]["hosts"]) == {"10-0-0-1"}


def test_render_inventory_uses_private_key_path_when_materialized(tmp_path):
    node = BootstrapNode(host="10.0.0.1", roles=["control-plane"], private_key="does-not-matter")
    key_path = str(tmp_path / "10-0-0-1.key")
    node.private_key_path = key_path
    data = render_inventory([node])
    assert data["all"]["hosts"]["10-0-0-1"]["ansible_ssh_private_key_file"] == key_path


def test_render_inventory_never_embeds_the_password():
    node = BootstrapNode(host="10.0.0.1", roles=["control-plane"], password=_FAKE_TEST_PASSWORD)
    data = render_inventory([node])
    # Passwords must never land in the shared inventory.yml (world-readable,
    # potentially versioned/logged) -- they're conveyed separately via
    # per-host host_vars/<name>.yml files instead (see render_host_vars).
    assert "ansible_ssh_pass" not in data["all"]["hosts"]["10-0-0-1"]


def test_render_host_vars_carries_the_real_password_for_password_auth_nodes():
    node = BootstrapNode(host="10.0.0.1", roles=["control-plane"], password=_FAKE_TEST_PASSWORD)
    host_vars = render_host_vars([node])
    assert host_vars == {node.name: {"ansible_ssh_pass": _FAKE_TEST_PASSWORD}}


def test_render_host_vars_skips_nodes_using_a_private_key(tmp_path):
    node = BootstrapNode(
        host="10.0.0.1",
        roles=["control-plane"],
        private_key_path=str(tmp_path / "fake-key"),
    )
    assert render_host_vars([node]) == {}


def test_render_host_vars_skips_nodes_with_no_password_set():
    node = BootstrapNode(host="10.0.0.1", roles=["control-plane"])
    assert render_host_vars([node]) == {}


def test_render_group_vars_pins_calico_and_local_kubeconfig():
    group_vars = render_group_vars()
    assert group_vars["kube_network_plugin"] == "calico"
    assert group_vars["kubeconfig_localhost"] is True
    assert "http_proxy" not in group_vars


def test_render_group_vars_disables_kubespray_hostname_override():
    # Kubespray defaults override_system_hostname to true, which renames
    # each target machine's real /etc/hostname to match its inventory name
    # -- unwanted when nodes already carry an operator/IT-assigned hostname.
    group_vars = render_group_vars()
    assert group_vars["override_system_hostname"] is False


def test_render_group_vars_merges_proxy_when_given():
    group_vars = render_group_vars({"http_proxy": "http://proxy:3128", "https_proxy": "http://proxy:3128"})
    assert group_vars["http_proxy"] == "http://proxy:3128"
    assert group_vars["https_proxy"] == "http://proxy:3128"
    assert "no_proxy" not in group_vars


def test_render_group_vars_omits_blank_proxy_values():
    group_vars = render_group_vars({"http_proxy": "", "https_proxy": None, "no_proxy": "localhost"})
    assert "http_proxy" not in group_vars
    assert "https_proxy" not in group_vars
    assert group_vars["no_proxy"] == "localhost"


def test_write_inventory_produces_valid_yaml_and_artifacts_dir(tmp_path):
    nodes = [BootstrapNode(host="10.0.0.1", roles=["control-plane"])]
    inventory_path = write_inventory(tmp_path, nodes)
    assert inventory_path.is_file()
    loaded = yaml.safe_load(inventory_path.read_text())
    assert "10-0-0-1" in loaded["all"]["hosts"]
    assert (tmp_path / "group_vars" / "k8s_cluster" / "k8s-cluster.yml").is_file()
    assert (tmp_path / "artifacts").is_dir()


def test_write_inventory_threads_proxy_into_group_vars_file(tmp_path):
    nodes = [BootstrapNode(host="10.0.0.1", roles=["control-plane"])]
    write_inventory(tmp_path, nodes, proxy={"http_proxy": "http://proxy:3128"})
    group_vars = yaml.safe_load((tmp_path / "group_vars" / "k8s_cluster" / "k8s-cluster.yml").read_text())
    assert group_vars["http_proxy"] == "http://proxy:3128"


def test_write_inventory_augments_no_proxy_with_cluster_internal_addresses(tmp_path):
    # Kubespray itself never adds these -- see ``inventory.py::_augment_no_proxy``
    # for why omitting them breaks kubectl/kubelet after bootstrap.
    nodes = [
        BootstrapNode(host="10.0.0.1", roles=["control-plane"]),
        BootstrapNode(host="10.0.0.2", roles=["worker"]),
    ]
    write_inventory(tmp_path, nodes, proxy={"http_proxy": "http://proxy:3128", "no_proxy": "example.com"})
    group_vars = yaml.safe_load((tmp_path / "group_vars" / "k8s_cluster" / "k8s-cluster.yml").read_text())
    no_proxy_entries = group_vars["no_proxy"].split(",")
    for expected in (
        "example.com",
        "localhost",
        "127.0.0.1",
        "10.233.0.0/18",
        "10.233.64.0/18",
        "10.0.0.1",
        "10.0.0.2",
        "kubernetes.default.svc.cluster.local",
    ):
        assert expected in no_proxy_entries


def test_write_inventory_does_not_add_no_proxy_when_no_proxy_vars_are_set(tmp_path):
    nodes = [BootstrapNode(host="10.0.0.1", roles=["control-plane"])]
    write_inventory(tmp_path, nodes, proxy=None)
    group_vars = yaml.safe_load((tmp_path / "group_vars" / "k8s_cluster" / "k8s-cluster.yml").read_text())
    assert "no_proxy" not in group_vars
