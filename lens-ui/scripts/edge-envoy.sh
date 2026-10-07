#!/usr/bin/env bash
# Run the Edge Envoy data plane for the model gateway.
#
# Generates the Edge config from published members (GatewayOpsService.reload_edge:
# tunnels to each cluster's Agent Router + ext_authz -> Python) and runs Envoy in
# Docker on the host network so it can reach the tunnels (127.0.0.1:<port>) and
# Python (127.0.0.1:8081).
#
# Usage: scripts/edge-envoy.sh {up|down|reload|config|logs}
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="${LENS_EDGE_ENVOY_CONFIG_PATH:-$HOME/.local/state/lens/edge/envoy.yaml}"
IMAGE="${LENS_EDGE_ENVOY_IMAGE:-envoyproxy/envoy:v1.31-latest}"
NAME="${LENS_EDGE_ENVOY_CONTAINER:-lens-edge-envoy}"

gen_config() {
    mkdir -p "$(dirname "$CONFIG")"
    # No reload command while generating: just write the config file.
    LENS_EDGE_ENVOY_CONFIG_PATH="$CONFIG" LENS_EDGE_ENVOY_RELOAD_COMMAND="" \
        "$ROOT/.venv/bin/python" - <<'PY'
import asyncio
from llm_d_bench.model_service.gateway_ops import GatewayOpsService

op = asyncio.run(GatewayOpsService().reload_edge())
print(f"reload_edge: {op.status} — {op.message}")
PY
}

case "${1:-up}" in
    config) gen_config ;;
    up)
        gen_config
        docker rm -f "$NAME" >/dev/null 2>&1 || true
        docker run -d --name "$NAME" --network host \
            -v "$CONFIG:/etc/envoy/envoy.yaml:ro" \
            "$IMAGE" -c /etc/envoy/envoy.yaml -l warning
        echo "Edge Envoy running as '$NAME' (config: $CONFIG)"
        echo "Tip: set LENS_MODEL_GATEWAY_PUBLIC_URL=http://127.0.0.1:8443/v1 so the UI shows the right base URL."
        ;;
    reload)
        gen_config
        docker restart "$NAME"
        ;;
    down) docker rm -f "$NAME" ;;
    logs) docker logs -f "$NAME" ;;
    *)
        echo "usage: $0 {up|down|reload|config|logs}" >&2
        exit 2
        ;;
esac
