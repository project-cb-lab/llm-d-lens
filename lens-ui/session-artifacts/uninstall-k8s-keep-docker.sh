#!/usr/bin/env bash
# Uninstall a Kubespray-installed Kubernetes control-plane/worker from this
# node, while leaving the container runtime (containerd, and/or Docker if
# present) installed, running, and untouched -- including its images and
# any containers/volumes that aren't Kubernetes-managed.
#
# Usage: sudo ./uninstall-k8s-keep-docker.sh
#
# What this removes:
#   - kubeadm/kubelet state (via `kubeadm reset`)
#   - the kubelet systemd service
#   - /etc/kubernetes, /etc/cni, /opt/cni, /var/lib/kubelet, /var/lib/etcd,
#     Calico's leftover state, and the kubeadm/kubelet/kubectl binaries
#     Kubespray places under /usr/local/bin (or the OS package, if this
#     node instead got kubelet/kubeadm/kubectl via apt/yum/dnf)
#   - iptables/ipvs rules and leftover CNI network interfaces
#     (cni0/flannel.1/cali*/vxlan.calico/kube-ipvs0)
#
# What this deliberately leaves alone:
#   - containerd (binary, systemd service, /etc/containerd, all images and
#     any non-Kubernetes containers), and Docker if it's also installed
#   - anything outside of the Kubernetes/CNI paths above

set -uo pipefail

log() { echo "[uninstall-k8s] $*"; }

if [ "$(id -u)" -ne 0 ]; then
  echo "must be run as root" >&2
  exit 1
fi

log "resetting kubeadm/kubelet state (if kubeadm is present)..."
if command -v kubeadm >/dev/null 2>&1; then
  kubeadm reset -f --cri-socket unix:///run/containerd/containerd.sock >/dev/null 2>&1 \
    || kubeadm reset -f >/dev/null 2>&1 \
    || log "kubeadm reset failed/skipped (continuing with manual cleanup)"
fi

log "stopping and disabling kubelet..."
systemctl stop kubelet 2>/dev/null || true
systemctl disable kubelet 2>/dev/null || true

log "removing Kubernetes config/state directories (containerd's own state under /var/lib/containerd is left alone)..."
rm -rf \
  /etc/kubernetes \
  /var/lib/kubelet \
  /var/lib/etcd \
  /etc/cni \
  /opt/cni \
  /var/lib/cni \
  /var/lib/calico \
  /var/run/calico \
  /etc/systemd/system/kubelet.service.d \
  /usr/lib/systemd/system/kubelet.service \
  /etc/systemd/system/kubelet.service \
  /root/.kube \
  "${SUDO_HOME:-$HOME}/.kube" 2>/dev/null

log "removing the kubeadm/kubelet/kubectl binaries Kubespray installs under /usr/local/bin..."
rm -f /usr/local/bin/kubeadm /usr/local/bin/kubelet /usr/local/bin/kubectl /usr/local/bin/crictl

log "uninstalling kubelet/kubeadm/kubectl OS packages, if this node has them instead (containerd/docker packages are left installed)..."
if command -v apt-get >/dev/null 2>&1; then
  apt-mark unhold kubelet kubeadm kubectl >/dev/null 2>&1 || true
  apt-get remove -y --purge kubelet kubeadm kubectl >/dev/null 2>&1 || true
  rm -f /etc/apt/sources.list.d/kubernetes.list /etc/apt/keyrings/kubernetes-apt-keyring.gpg
elif command -v dnf >/dev/null 2>&1; then
  dnf remove -y kubelet kubeadm kubectl >/dev/null 2>&1 || true
  rm -f /etc/yum.repos.d/kubernetes.repo
elif command -v yum >/dev/null 2>&1; then
  yum remove -y kubelet kubeadm kubectl >/dev/null 2>&1 || true
  rm -f /etc/yum.repos.d/kubernetes.repo
fi

log "flushing leftover iptables/ipvs rules from kube-proxy/Calico (best effort; other rules are left alone)..."
if command -v iptables-save >/dev/null 2>&1 && command -v iptables-restore >/dev/null 2>&1; then
  iptables-save 2>/dev/null | grep -iv -E 'kube|cali' | iptables-restore 2>/dev/null || true
fi
if command -v ip6tables-save >/dev/null 2>&1 && command -v ip6tables-restore >/dev/null 2>&1; then
  ip6tables-save 2>/dev/null | grep -iv -E 'kube|cali' | ip6tables-restore 2>/dev/null || true
fi
command -v ipvsadm >/dev/null 2>&1 && ipvsadm --clear 2>/dev/null || true

log "removing leftover CNI network interfaces (best effort)..."
for iface in cni0 flannel.1 kube-ipvs0 vxlan.calico dummy0; do
  ip link delete "$iface" >/dev/null 2>&1 || true
done
ip -o link show 2>/dev/null | awk -F': ' '{print $2}' | grep -E '^cali' | while read -r iface; do
  ip link delete "$iface" >/dev/null 2>&1 || true
done

systemctl daemon-reload >/dev/null 2>&1 || true

log "done -- container runtime status (left untouched):"
if command -v containerd >/dev/null 2>&1; then
  echo "  containerd: $(systemctl is-active containerd 2>/dev/null || echo 'not running as a service')"
fi
if command -v docker >/dev/null 2>&1; then
  echo "  docker: $(systemctl is-active docker 2>/dev/null || echo 'not running as a service')"
fi

log "Kubernetes has been removed from this node. It should now pass Prism's 'no existing Kubernetes install' preflight check."
