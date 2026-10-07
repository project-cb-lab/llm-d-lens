#!/usr/bin/env bash
# Start the local Prism stack with every rendered Kubernetes workload pinned to
# one node. Change TARGET_NODE in the config file, or pass another config path.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ACTION="${1:-start}"
CONFIG_FILE="${2:-$ROOT_DIR/.dev-target-node.env}"

case "$ACTION" in
    stop|status)
        cd "$ROOT_DIR"
        exec scripts/dev.sh "$ACTION"
        ;;
    start|restart|check) ;;
    *)
        echo "Usage: $0 {start|restart|check|status|stop} [config-file]" >&2
        exit 2
        ;;
esac

if [[ ! -f "$CONFIG_FILE" ]]; then
    cat >&2 <<EOF
Target-node config not found: $CONFIG_FILE
Create it from .dev-target-node.env.example, then retry.
EOF
    exit 1
fi

set -a
# shellcheck disable=SC1090
source "$CONFIG_FILE"
set +a

: "${TARGET_NODE:?TARGET_NODE must be set in $CONFIG_FILE}"
export PRISM_K8S_TARGET_NODE="$TARGET_NODE"

KUBECTL="${LLM_D_BENCH_KUBECTL_PATH:-$(command -v kubectl || true)}"
if [[ -z "$KUBECTL" || ! -x "$KUBECTL" ]]; then
    echo "kubectl is missing or not executable" >&2
    exit 1
fi
export LLM_D_BENCH_KUBECTL_PATH="$(readlink -f "$KUBECTL")"
export KUBECONFIG="${KUBECONFIG:-$HOME/.kube/config}"

node_json="$($KUBECTL get node "$TARGET_NODE" -o json)" || {
    echo "Kubernetes node '$TARGET_NODE' is not accessible in the current context" >&2
    exit 1
}
ready="$(jq -r '.status.conditions[] | select(.type == "Ready") | .status' <<<"$node_json")"
unschedulable="$(jq -r '.spec.unschedulable // false' <<<"$node_json")"
if [[ "$ready" != "True" || "$unschedulable" == "true" ]]; then
    echo "Node '$TARGET_NODE' is not Ready and schedulable" >&2
    exit 1
fi

if [[ -z "${PRISM_GPU_PCI_ALLOWLIST:-}" ]]; then
    PRISM_GPU_PCI_ALLOWLIST="$($KUBECTL get resourceslices.resource.k8s.io -o json | jq -r \
        --arg node "$TARGET_NODE" \
        '.items[] | select(.spec.nodeName == $node and .spec.driver == "gpu.intel.com") | .spec.devices[]?.attributes.pciAddress.string' \
        | sort -u | paste -sd, -)"
    export PRISM_GPU_PCI_ALLOWLIST
fi
if [[ -z "$PRISM_GPU_PCI_ALLOWLIST" ]]; then
    echo "No gpu.intel.com DRA devices were found on '$TARGET_NODE'" >&2
    exit 1
fi

export LLM_D_BENCH_DEPLOY_RUNTIME_ENABLED="${LLM_D_BENCH_DEPLOY_RUNTIME_ENABLED:-true}"
export LLM_D_BENCH_NAMESPACE_PREFIX="${LLM_D_BENCH_NAMESPACE_PREFIX:-llm-d-bench-}"
export LLM_D_BENCH_KUBECTL_TIMEOUT_SECONDS="${LLM_D_BENCH_KUBECTL_TIMEOUT_SECONDS:-120}"
export LLM_D_BENCH_READINESS_TIMEOUT_SECONDS="${LLM_D_BENCH_READINESS_TIMEOUT_SECONDS:-900}"

if [[ -z "${LLM_D_ROOT:-}" || ! -d "$LLM_D_ROOT/guides" ]]; then
    echo "LLM_D_ROOT must point to an llm-d checkout containing guides/" >&2
    exit 1
fi

if [[ -z "${LLM_D_BENCH_HELM_PATH:-}" ]]; then
    helm_path="$(command -v helm || true)"
    [[ -n "$helm_path" ]] && export LLM_D_BENCH_HELM_PATH="$(readlink -f "$helm_path")"
fi
if [[ -z "${LLM_D_BENCH_DOCKER_PATH:-}" ]]; then
    docker_path="$(command -v docker || true)"
    [[ -n "$docker_path" ]] && export LLM_D_BENCH_DOCKER_PATH="$(readlink -f "$docker_path")"
fi

echo "Starting Prism for Kubernetes node: $TARGET_NODE"
echo "GPU PCI allowlist: $PRISM_GPU_PCI_ALLOWLIST"
echo "Kubernetes context: $($KUBECTL config current-context)"
cd "$ROOT_DIR"
exec scripts/dev.sh "$ACTION"