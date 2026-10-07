# Model Service Gateway Deployment and Operations Guide (llm-d Gateway Mode)

> Design: [`model-service-llmd-routing-design.zh-CN.md`](model-service-llmd-routing-design.zh-CN.md).
> This is the Gateway Mode deployment checklist/runbook. The community llm-d data plane
> (shared cluster Gateway + IPP + InferencePool + EPP) replaces the earlier Edge Envoy /
> Agent Router / Tunnel-Controller design. The control plane and renderers are implemented;
> actual data-plane installation and acceptance require a real cluster.

## 1. Data-plane layers

The three Gateway Mode layers have separate lifecycles:

| Layer | Created when | Resources | Code |
| --- | --- | --- | --- |
| Shared cluster | Cluster creation/update | Gateway API / GAIE CRDs, provider, **Gateway**, **IPP** | `llm_d_bench/model_service/gateway_providers.py` (`render_inference_gateway`), `gateway_ops.py` (`install_inference_gateway` / `install_ipp`) |
| Deployment | Deployment creation | Model server + EPP + **InferencePool** (1:1, **no own proxy/gateway**) | `llm_d_bench/deploy/...` (disable chart proxy; see `router-gateway-mode.values.yaml`) |
| Model service | Publication | **HTTPRoute**, cross-namespace **ReferenceGrant**, IPP model-mapping **ConfigMap** | `gateway_providers.py` (`render_model_route`), `gateway_ops.py` (`reconcile_cluster_gateway`) |

```
Client ──► Shared cluster Gateway ──► HTTPRoute (base-model match)
                                          │ backendRefs: InferencePool
                                          ▼
                                  InferencePool ──► EPP ──► model server (vLLM)
                    ▲
        IPP (ext_proc: body.model → X-Gateway-Base-Model-Name + ClearRouteCache)
```

One Gateway per cluster; **one HTTPRoute per model service**, whose `backendRefs` points to
**one** InferencePool. llm-d uses one EPP per pool; Lens does not split traffic by weights
across pools.

## 2. Cluster prerequisites

- Lens backend/database are running, and the target cluster is registered with kubeconfig in Lens DB.
- Kubernetes 1.32+ with Gateway API and Gateway API Inference Extension
  (GAIE, `InferencePool` `inference.networking.k8s.io/v1`) CRDs.
- Install the selected **Gateway provider**, configured through `gatewayProvider` during
  cluster creation (§4.1): `gke`, `istio`, `agentgateway`, or `envoy-ai-gateway` (default).
- **Envoy AI Gateway requires InferencePool backend support.** Otherwise HTTPRoute references
  are `InvalidKind` (`ResolvedRefs=False`) and requests receive 500 direct responses.
  Include the inference-pool addon when installing Envoy Gateway:

  ```bash
  helm upgrade -i eg oci://docker.io/envoyproxy/gateway-helm \
    --version v1.8.1 -n envoy-gateway-system --create-namespace \
    -f https://raw.githubusercontent.com/envoyproxy/ai-gateway/v0.7.0/manifests/envoy-gateway-values.yaml \
    -f https://raw.githubusercontent.com/envoyproxy/ai-gateway/v0.7.0/examples/inference-pool/envoy-gateway-values-addon.yaml
  ```

  The addon sets `extensionApis.enableBackend: true`, `extensionManager.hooks`, and:

  ```yaml
  extensionManager:
    backendResources:
    - group: inference.networking.k8s.io
      kind: InferencePool
      version: v1
  ```

  > If automation repeatedly runs `helm upgrade eg`, include these values there.
  > Manual edits to `envoy-gateway-system/envoy-gateway-config` are overwritten on upgrade.

- **The cluster must allocate a LoadBalancer address.** With empty `status.addresses`, Gateway
  stays `Programmed=False` (`reason=AddressNotAssigned`). Cloud providers allocate addresses;
  local kind requires `cloud-provider-kind` or MetalLB.
  If installing a load balancer is impossible, set **`LENS_GATEWAY_SERVICE_TYPE=NodePort`**
  and reconcile. Lens uses provider-supported Service overrides: `EnvoyProxy` for Envoy AI
  Gateway; a `ConfigMap` referenced by `Gateway.spec.infrastructure.parametersRef` for Istio
  (matching llm-d's `guides/recipes/gateway/istio`).
- **Istio:** install with
  `istioctl install -y --set values.pilot.env.ENABLE_GATEWAY_API_INFERENCE_EXTENSION=true`
  (the llm-d guide pins 1.29.2). Lens renders `istio.io/enable-inference-extproc: "true"` on
  the Gateway; the recipe requires it for EPP inference ext_proc.

## 3. Environment variables

| Variable | Default | Description |
| --- | --- | --- |
| `LENS_IPP_CHART` | `oci://ghcr.io/llm-d/charts/payload-processor` | IPP Helm chart path or OCI reference |
| `LENS_IPP_VERSION` | `v0.1.0` | IPP chart version; also configurable through cluster `ipp_version` |
| `LENS_MODEL_RECONCILE_INTERVAL_SECONDS` | `300` | Background shared-Gateway/model-route reconciliation interval, seconds |
| `LENS_MODEL_HEALTH_INTERVAL_SECONDS` | `10` | Member backend health-probe interval, seconds; each round reprobes HTTPRoute/InferencePool/EPP/model server. Do not set zero, which causes busy polling |
| `LENS_MODEL_GATEWAY_PUBLIC_URL` | Empty | Gateway entry URL displayed to users |
| `LENS_GATEWAY_SERVICE_TYPE` | Empty | `LoadBalancer`/`NodePort`/`ClusterIP` using official provider overrides (`EnvoyProxy` for Envoy, ConfigMap parametersRef for Istio). Use NodePort without LB; empty retains provider default |

## 4. Deployment steps

### 4.1 Create cluster and install the shared Gateway

New cluster fields (migration `c1f7a2b9d4e6`):

| Field | Description |
| --- | --- |
| `gateway_provider` | `gke` / `istio` / `agentgateway` / `envoy-ai-gateway` |
| `gateway_namespace` / `gateway_name` | Default `lens-gateway` / `lens-inference-gateway` |
| `router_version` / `gie_version` / `ipp_version` | Pinned versions |

- **Wizard:** choose provider and optional IPP version in **Model gateway**. After saving,
  the backend calls `install_inference_gateway` in the background.
- **API:** `POST /api/v1/model-service/admin/gateway/install` with `cluster_id` and optional
  `provider`, or PATCH cluster `gatewayProvider`. `cluster/router.py` schedules installation
  after create/PATCH.
- Render Namespace, GatewayClass, and Gateway (HTTP `:80`). Envoy AI Gateway additionally
  renders ClientTrafficPolicy with a default `50Mi` connection buffer.

### 4.2 Publish model services and members

- Create a model group/public model name through `POST /api/v1/model-service/admin/groups`.
  Groups carry `cluster_id`, `served_name`, and `base_model`; `(cluster_id, name)` is unique.
- Publish a deployment as a member through `POST /api/v1/model-service/admin/members`.
  Select cluster/deployment; execution supplies `namespace / service / port / endpointKind`.
- `pool_name` defaults from EPP Service: `<release>-epp` → `<release>` (`default_pool_name`),
  qualified as `<namespace>/<release>` (`qualified_pool_name`) to prevent namespace collisions.
  HTTPRoute rendering splits it back into name/namespace. `epp_ref` records the EPP reference.
- **EPP must serve plaintext.** Gateway Mode values in `router-gateway-mode.values.yaml`
  disable the chart proxy and set `router.epp.flags.secure-serving=false`. Otherwise default
  TLS ext_proc (`secure-serving=true`) resets shared-Gateway plaintext h2c calls, causing 500s.
- Multiple members may be published, but routing uses the **highest-priority active member's**
  pool; others are backups. Reconciliation creates cross-namespace ReferenceGrants as needed.
- **Deleting deployments cascades to members:** health probing (default 60s) removes dangling
  members when their execution records no longer exist. If a model service has no members,
  reconciliation removes its leftover HTTPRoute and model-map ConfigMap.

### 4.3 Install IPP

IPP (`lens-ipp`, Deployment) runs in the Gateway namespace and injects model headers via ext_proc:

```python
from llm_d_bench.model_service.gateway_ops import GatewayOpsService

await GatewayOpsService().install_ipp("cluster-a")  # Default chart/version/name
await GatewayOpsService().uninstall_ipp("cluster-a")
```

Version precedence: request → `LENS_IPP_VERSION` → cluster `ipp_version` → default.

### 4.4 Reconciliation and resources

`reconcile_cluster_gateway` ensures Gateway exists, then renders/applies resources for
**each model service with an active member**:

```python
from llm_d_bench.model_service.gateway_ops import GatewayOpsService

await GatewayOpsService().reconcile_cluster("cluster-a")
```

- **HTTPRoute:** one per model service in Gateway namespace, named with a DNS-safe model slug.
  `parentRefs` points to the cluster Gateway; matches use IPP-injected
  `X-Gateway-Base-Model-Name = base_model`. `backendRefs` contains **one**
  `{group: inference.networking.k8s.io, kind: InferencePool, name: <pool_name>, [namespace]}`
  for the highest-priority active member. **Do not set `port`:** InferencePool `spec.targetPorts`
  owns it, matching the official llm-d-router-gateway chart.
- **ReferenceGrant:** when namespaces differ, create one in each pool namespace permitting
  Gateway-namespace HTTPRoutes to reference that InferencePool.
- **ConfigMap:** `<slug>-model-map` in Gateway namespace, label
  `inference.llm-d.ai/ipp-managed=true`, containing `data.baseModel` and `adapters`.

`llm_d_bench/api/main.py` reconciles all clusters with non-disabled members every
`LENS_MODEL_RECONCILE_INTERVAL_SECONDS`. Apply is idempotent and safe to repeat.

### 4.5 Administration UI and APIs

The **Data plane** panel at `?view=admin/model-service` shows an actionable topology:
one Gateway per cluster, IPP, and each pool/EPP/model server. Operations use
`POST /api/v1/model-service/admin/gateway/stream` (SSE logs) or matching REST endpoints:

| Operation | Endpoint | Permission |
| --- | --- | --- |
| Status | `GET …/gateway/status` | `model-service:gateway:read` |
| Install Gateway | `POST …/gateway/install` | `model-service:gateway:manage` |
| Reconcile | `POST …/gateway/reconcile` | `model-service:gateway:manage` |
| Scale components | `POST …/gateway/scale` | `model-service:gateway:manage` |
| Install/uninstall IPP | `POST …/gateway/stream` (`op=install-ipp`/`uninstall-ipp`) | `model-service:gateway:manage` |
| Install/uninstall/reconcile Gateway | `POST …/gateway/stream` (`op=install-gateway`/`uninstall-gateway`/`reconcile`) | `model-service:gateway:manage` |
| Probe | `POST …/gateway/probe` | `model-service:gateway:manage` |
| Preflight | `POST …/gateway/preflight` | `model-service:router:manage` |

Gateway uninstall cleans resources labeled `app.kubernetes.io/part-of=lens-inference-gateway`
and legacy `lens-agent-router` resources (`…=lens-agent-router`). All operations are audited
in `model_service_gateway_operations`.

## 5. Acceptance

```bash
# Gateway ready (requires an address)
kubectl get gateway lens-inference-gateway -n lens-gateway
# NAME                     CLASS              ADDRESS       PROGRAMMED
# lens-inference-gateway   envoy-ai-gateway   10.x.x.x      True

# Route references resolved
kubectl get httproute -n lens-gateway -o yaml | grep -A3 ResolvedRefs   # ResolvedRefs=True

# Direct local verification without an LB address
kubectl port-forward -n envoy-gateway-system \
  svc/$(kubectl get svc -n envoy-gateway-system \
        -l gateway.envoyproxy.io/owning-gateway-name=lens-inference-gateway \
        -o jsonpath='{.items[0].metadata.name}') 8899:80
curl -s localhost:8899/v1/completions -H 'Content-Type: application/json' \
  -H 'X-Gateway-Base-Model-Name: Qwen/Qwen3-0.6B' \
  -d '{"model":"Qwen/Qwen3-0.6B","prompt":"hi"}'
```

## 6. Troubleshooting

| Symptom | Cause/check |
| --- | --- |
| `Programmed=False` (`AddressNotAssigned`) | No LB address; inspect `EXTERNAL-IP` with `kubectl get svc -n envoy-gateway-system`; install cloud-provider-kind/MetalLB for kind |
| HTTPRoute `ResolvedRefs=False` (`InvalidKind`) | Missing InferencePool addon (§2); verify `extensionManager.backendResources` includes `inference.networking.k8s.io/v1 InferencePool` |
| `Invalid resource name "…/…"` | Model ID contains `/`; resource names require DNS normalization (`resource_name` handles it; reconcile old names) |
| Multiple members but no route | Confirm derived `pool_name`, `status=active`; reconcile and inspect `kubectl get httproute -A`: one route, one backendRef for the highest-priority active member |
| Route exists but requests return 500 | `ResolvedRefs` must be True; InvalidKind generates a 500 direct response |
| All requests reach one replica | Check pool selector against model-server pod labels; Lens does not weight across pools |

## 7. Security

- Backends expose ClusterIP only; the shared Gateway is the external entry point.
- Plaintext tokens are returned once at creation; logs contain only `token_hint`.
- Kubeconfig resides in Lens DB. Rendering/reconciliation is server-side and does not accept
  arbitrary client-supplied destination addresses.

## 8. Known gaps

- **Data-plane metering is integrated:** periodically read per-cluster Prometheus EPP counters
  `llm_d_epp_request_{input,output,cached}_tokens_sum`, native llm-d metrics. Attribute deltas by
  `fairness_id` into `usage_records` (`POST …/admin/gateway/usage/sync`, background
  `LENS_GATEWAY_USAGE_INTERVAL_SECONDS`). `default-flow` without injected identity aggregates by model.
- **HTTP-provider ext_authz injection is rendered:** with `LENS_MODEL_GATEWAY_AUTHZ_HOST`,
  `render_inference_gateway` creates in-cluster lens-authz Service/Endpoints pointing to Lens.
  Envoy uses `SecurityPolicy.extAuth.http` + `headersToBackend`; Istio uses mesh
  `extensionProviders.enovyHttpService` + CUSTOM AuthorizationPolicy; agentgateway uses
  `AgentgatewayPolicy.traffic.extAuth.http` + `allowedResponseHeaders`. Injected
  `x-llm-d-inference-fairness-id` becomes EPP `fairness_id` for per-user metering.
  Istio actually uses `envoyExtAuthzHttp` (not `envoyHttpService`) in meshConfig through an
  IstioOperator overlay passed to `istioctl install -f`; explicitly include Authorization in
  `includeRequestHeadersInCheck`. Read model name from X-Gateway-Base-Model-Name, not the body.
  Lens must be cluster-reachable (default bind `0.0.0.0`).
  **gke is unsupported:** its managed Gateway lacks a Gateway API ext_authz attachment point;
  Cloud Service Mesh/IAP is required, so this provider renders no ext_authz.
  The host must be reachable from the cluster. It defaults from LENS_MODEL_GATEWAY_AUTHZ_HOST;
  leaving it unset disables ext_authz to avoid disrupting clusters before reachability is verified.
- Provider/version compatibility (IPP ↔ Router ↔ GIE ↔ provider) has not passed cluster acceptance.
- gke/istio/agentgateway renderers have recipes, but only envoy-ai-gateway has real-cluster
  end-to-end verification.
