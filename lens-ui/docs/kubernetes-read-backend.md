# Kubernetes SDK / CLI backend

Lens uses the official asynchronous Python client (`kubernetes==36.0.3`) for
resource operations, with explicit CLI rollback. SDK data stays as raw Kubernetes
JSON; existing callers retain `CommandResult` and legacy list/error contracts.
Typed list responses supply missing `kind` and `apiVersion` on their items,
matching kubectl output, including on subsequent pages. Explicit item types and
generic `List` responses are preserved. Monitoring classification and Storage
PVC/PV association depend on this compatibility.

## Activation and rollback

Install the Python dependencies (`pip install -e .` in the backend virtualenv).
SDK reads **and** supported writes are enabled by default when neither backend
variable is set. Restart an existing backend process after updating the code.
To explicitly select the full SDK mode, including over an existing legacy setting:

```sh
export PRISM_KUBERNETES_BACKEND=sdk
```

Set `PRISM_KUBERNETES_BACKEND=cli` to roll back. Unsupported operations still
select CLI before execution; failed SDK requests do not retry through CLI.
The earlier `PRISM_KUBERNETES_READ_BACKEND=sdk` enables SDK reads only and does
not enable mutations; the new variable takes precedence. Invalid values fail.
No service was restarted or runtime flag changed during implementation.

## Migrated paths

| Capability | SDK coverage |
| --- | --- |
| Resources | Core, apps, batch, storage, networking, RBAC and CRD definitions; discover custom resources from the served preferred API version. |
| Queries | Named/list JSON or YAML, namespace/all namespaces, label/field selectors, pagination, multi-resource lists and selected raw API GETs. |
| Mutations | Namespace creation; single core/apps/batch manifest creation from stdin; strategic/merge/JSON patches; labels with overwrite conflict checking; scale; cordon/uncordon; named or selector deletion and UID-aware deletion waits. |
| Logs | Bounded named-Pod logs, container selection, tail, previous and timestamps. |
| Existing Python callers | Public cluster helpers, scoped runners used by storage/model cache/simulation/monitoring, evaluation snapshots, and validated deployment runners. |
| Node planning | A selected session's four allowlisted planning queries use the Python session endpoint. The Node host's default-context queries remain local CLI. |
| Benchmark diagnostics | SDK Watch wakes pod failure checks; periodic pod/log reconciliation remains. CLI mode keeps existing polling. |

Only recognized argv forms enter the adapter. Unknown flags, unsupported output
formats or ambiguous selection combinations select CLI before any effects. API
failure never triggers a CLI retry. Explicit JSON output is preserved; default
mutation logs contain status lines rather than resource bodies/Secret data.

## Preserved CLI operations

Helm and Kustomize, **client-side kubectl apply**, rollout/wait variants not handled
by the adapter, Pod exec, port-forward, streaming/workload-selector log variants,
SSH remote commands, advanced flags such as force/grace/dry-run, and legacy
`auth-provider` credentials keep their existing CLI paths. Keep kubectl installed.
The adapter does not translate arbitrary shell scripts. Client-side apply is not
silently replaced by server-side apply: field ownership and pruning semantics
would differ. Remote API reachability is not assumed.

## Credentials, connections and errors

- Registry/session kubeconfig resolution is reused; SDK paths reject an unresolved
  explicit cluster rather than inheriting another default cluster.
- When a proxy is configured, the selected API-server host is added to the process
  `NO_PROXY`/`no_proxy` before the first SDK request (`utils/kubernetes_proxy.py`).
  The aiohttp transport cannot bypass CIDR entries the way `kubectl` does, so an
  API server covered only by a `10.112.0.0/16`-style entry would otherwise fail with
  `ClientHttpProxyError`. The change is additive, skips an explicit kubeconfig
  `proxy-url`, and never removes existing bypass entries.
- KUBECONFIG merging, context namespace, static token/token-file and TLS credentials
  are supported. ExecCredential plugins use argument arrays, noninteractive stdin,
  validated output/version/expiry, sanitized errors and bounded process-group
  cleanup on POSIX timeout/cancellation. Legacy auth-provider selects CLI before
  authentication. No credentials are persisted back into kubeconfig.
- Each acquisition reloads configuration. During application lifecycle, ordinary
  HTTP clients are pooled by credential/TLS identity, with 16 client/borrower bounds,
  idle-only LRU and lazy 30-second idle expiration. Shutdown closes clients and
  private exec certificate files. Outside that lifecycle clients close per use.
- Watch connections bypass the ordinary pool and close/reload credentials on each
  bounded reconnect. Bookmarks update resourceVersion; 410 emits RESET and relists;
  401/403 propagate; transient reconnects use bounded backoff. Early-exit consumers
  must close their async iterator (`contextlib.aclosing`).
- Strict `query_resource` and mutation helpers preserve API exceptions. Existing
  `list_resources` retains empty-list compatibility for request failures and logs
  sanitized categories; an empty list alone still cannot prove no resources exist.
  Public scoped runners retain timeout exceptions; deployment tuple results keep
  exit-code/evidence behavior. Deployment policy validation still precedes SDK I/O.

## Validation

Local tests use real SDK requests, HTTP streams, mutual TLS and real isolated
credential helper subprocesses. Coverage includes pagination failures, credentials
and session rotation, cancellation, process descendants, pool cleanup, writes,
Watch reconnects and existing storage/evaluation/deployment boundaries.

A read-only comparison against the machine's configured cluster confirmed equal
server versions and equal name/UID sets for five nodes. No live mutation, rollout,
service restart or backend-mode switch was performed. This does not establish
write compatibility for every cluster, authentication provider or CRD.

See [implementation plan](../specs/changes/kubernetes-sdk-expansion.md) for scope and
validation results. Helm/apply/exec/port-forward retention is intentional; switching
the mode does not remove every external command from the product.
