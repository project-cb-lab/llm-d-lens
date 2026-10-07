# Model Service — open TODOs (llm-d Gateway Mode)

Tracked work for the (in-progress) **multi-cluster / public-port** redesign.

## Done

- [x] Reconcile renders an HTTPRoute **per cluster** from the members that live in
  that cluster (a model service may have members on several clusters).
- [x] `/api/v1/model-service/models` returns one entry **per (model, cluster)** so
  "My services → Available models" lists a model under every cluster it is served
  from, with that cluster's connection info.
  Verified live: `Qwen/Qwen3-0.6B` → `8c5d9197` **and** `9dbd2700`, and both clusters
  have their own `qwen-qwen3-0-6b` HTTPRoute. (Required a backend restart; the dev
  workflow rule now restarts services automatically.)
- [x] Per-cluster **exposed port** (`clusters.gateway_port`, migration `d2a8b4c7e9f1`):
  Lens renders the data-plane Service as `NodePort` with that `nodePort`
  - Envoy AI Gateway: `EnvoyProxy.spec.provider.kubernetes.envoyService`
    `{type: NodePort, patch: {type: StrategicMerge, value: {spec: {ports: [{port: 80, nodePort: N}]}}}}`.
  - Istio: `ConfigMap.data.service` with `type: NodePort` + `ports[].nodePort`.
- [x] `/connection` derives each cluster's `baseUrl` from the **node IP + exposed
  port** (`http://<node-ip>:<port>/v1`); falls back to the Gateway address when no
  port is configured, and to `LENS_MODEL_GATEWAY_PUBLIC_URL` when set.
- [x] Frontend: `Model gateway` wizard step and `Edit cluster` expose an
  **Exposed port (NodePort)** input (manual public-URL input removed).
- [x] Verified on both clusters: env `nodePort=30999`, istio `nodePort=30998`,
  both `Programmed=True`.
- [x] Public reachability on kind: Lens starts a managed
  `kubectl port-forward --address 0.0.0.0 <ns>/<svc> <port>:80` on the Lens host when
  `gateway_port` is set (installed in `install_inference_gateway`, stopped on
  uninstall). Verified: `http://10.112.228.229:30998/v1/chat/completions` → HTTP 200.

## Open

- [x] **Exposure forward lifetime.** A dedicated backend task
  (`_run_gateway_exposure`, interval `LENS_GATEWAY_EXPOSURE_INTERVAL_SECONDS`) owns the
  per-cluster `0.0.0.0` tunnels and re-establishes them on restart; `install` also
  ensures them.
- [x] **NodePort conflict/pre-validation**: `install_inference_gateway` rejects ports
  outside 30000–32767 and ports already used by another Service (clear message).
- [x] **Envoy AI Gateway + IPP (decided: not supported).** The IPP chart wires ext_proc
  only for `istio`/`gke`. Tried an `EnvoyExtensionPolicy.extProc` for `envoy-ai-gateway`:
  the policy is `Accepted` but the IPP is never invoked before route matching (the
  request 404s first) and with the matched route it broke to 500 — so it was reverted.
  Conclusion: on `envoy-ai-gateway`, clients must send `X-Gateway-Base-Model-Name`
  (verified 200), or the cluster should use `istio`. This matches upstream: llm-d's IPP
  well-lit paths use istio/gke, and Envoy AI Gateway's own model routing is AIGatewayRoute.
- [ ] `gke` / `agentgateway`: confirm how each exposes an address; the NodePort
  mechanism is implemented for Envoy AI Gateway and Istio only.
- [ ] Remove `clusters.gateway_public_url` if the exposed-port mechanism fully
  replaces it (kept only as an optional API-level override; not in the UI).
- [ ] Docs: `model-service-gateway-deployment.md`,
  `docs/fern/pages/user-guide/{clusters,model-services}.mdx`,
  `docs/fern/pages/api-reference/{clusters,model-services}.mdx` — exposed-port flow
  and per-cluster models.
- [ ] `sprocean-benchuser` member of `Qwen/Qwen3-0.6B`: confirm the per-cluster
  HTTPRoute + model entry appear (test on the live cluster).
- [ ] Reuse catalog/map regenerated and `reuse check` / `verify-report` current.
