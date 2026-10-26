# `llmd`

`llmd` plans, stands up, rolls out, and removes [curvebender-tools](https://github.com/project-cb-lab/curvebender-tools)
stacks inside one existing Kubernetes or OpenShift namespace. It never needs cluster-admin rights and never
creates namespaces, CRDs, cluster roles, PersistentVolumes, SCCs or network attachments: those are reported
for an administrator.

A *stack* is a directory of a curvebender-tools checkout: a per-cluster overlay such as
`manifests/production/<model>/<cluster>`, which the team applies with `oc apply -k`. An *instance* is a stack
running in a namespace, recorded in a ConfigMap `lens-instance-<name>` there, so every command works from any
machine.

## Install

From the repository root:

```sh
python3 -m venv .venv
.venv/bin/pip install -e ./cli
.venv/bin/llmd --version
```

`llmd` needs `kubectl` (or `oc` with `--kube-cli oc`), `git`, and `kustomize` (or `kubectl kustomize`).

## Sources

A stack comes from upstream directly, or from a local checkout:

```sh
llmd sources add cb https://github.com/project-cb-lab/curvebender-tools.git#main
llmd stacks manifests/production
```

`--source` takes a registered name, a local path, or `URL#REF`. REF is a branch, a tag, a commit, or any ref
the remote serves, so a pull request can be planned before it merges:
`--source https://github.com/project-cb-lab/curvebender-tools.git#refs/pull/<N>/head`. With exactly one
registered source, `--source` may be left out. A remote checkout is cached; `--refresh` fetches it again.

The namespace comes from `-n`, else `$LLMD_NAMESPACE`, else the kube context.

## An empty namespace

```sh
STACK=manifests/production/<model>/<cluster>
llmd plan "$STACK" -n <namespace>              # read-only: findings, prerequisites, a server dry run
llmd standup "$STACK" -n <namespace> --expected-digest <digest from the plan> --wait
llmd status <instance> -n <namespace>
llmd teardown <instance> -n <namespace>
```

`plan` renders the stack exactly as `oc apply -k` would, moves it into the namespace (rewriting in-stack
references to the namespace it was written for), and reports what blocks a standup: objects only an
administrator may apply (`--admin-yaml FILE` writes them; `--skip-admin-parts` once they are applied),
privileged containers and hostPath volumes (`--allow-privileged` once an administrator granted them), missing
Secrets, claims and network attachments, missing permissions, and objects another tool already deployed.
For a stack that grants an SCC to its pods, the administrator applies the binding from `--admin-yaml`; the
team then passes both `--skip-admin-parts` and `--allow-privileged`.

`standup` records the instance before it applies anything, then applies with server-side apply. `teardown`
deletes exactly the recorded objects whose UID still matches; claims are kept unless `--delete-volumes`.

## A stack already deployed with `oc apply` or Helm

```sh
llmd import "$STACK" -n <namespace> --name <instance> --dry-run   # do the running objects match the files?
llmd import "$STACK" -n <namespace> --name <instance>
llmd status <instance> -n <namespace>
```

`import` records the running objects by UID and changes nothing else: no labels, no apply, no field
ownership, so the team's own `oc apply -k` keeps working. `--dry-run` compares the running objects with the
stack's files first; import the overlay that matches what runs. `llmd teardown <instance> --forget` stops
managing it and leaves every object running.

## Rollouts

A rollout moves an instance to what another ref of the same stack renders, in place. For a stack with one
DisaggregatedSet whose pod templates change, its controller (lws) replaces every group: with `maxSurge: 0`
and `maxUnavailable: N` per role, each step removes up to N old groups of every role and creates as many new
ones.

```sh
NEW=https://github.com/project-cb-lab/curvebender-tools.git#refs/pull/<N>/head
llmd rollouts plan "$STACK" -n <namespace> --name <instance> --source "$NEW" \
  --floor prefill=4 --floor decode=3
llmd rollouts start "$STACK" -n <namespace> --name <instance> --source "$NEW" \
  --floor prefill=4 --floor decode=3 \
  --drain 'python3 {source}/manifests/production/<model>/<cluster>/guards/drain_group.py roll {role} --batch {max_unavailable}' \
  --watch
```

`rollouts plan` is read-only. It shows the objects that change, whether the DisaggregatedSet rolls or only
scales, the steps, and the groups each role keeps serving during a step. It refuses a rollout while a roll is
in progress or a group is not serving, when several DisaggregatedSets would roll at once, when roles are
added or removed, and when a step would leave a role below its `--floor`. It also refuses a step that leaves
a role with no group serving (a role of one group with `maxSurge: 0`); `--allow-outage` accepts that
outage.

`rollouts start` starts the stack's drain command once per role (`--drain`, with `{role}`, `{source}`,
`{namespace}`, `{set}`, `{old_revision}` and `{max_unavailable}` filled in), then applies only the objects that
change. An imported stack's objects are applied with client-side `kubectl apply`, as its team applies them.
curvebender-tools stacks guard their groups with admission policies that let the controller remove a group
only after the drain tool took it out of the pool, so the drainers are what let each step proceed. The
drainers run `oc` with your kube context: select the cluster with `kubectl config use-context` rather than
`--context`. Their output is under `$LENS_DATA_DIR/llmd/rollouts/<namespace>/<instance>/`.

```sh
llmd rollouts status <instance> -n <namespace> [--watch]   # per role and revision; the drainers
llmd rollouts pause <instance> -n <namespace>               # stop the drainers: the guards hold the roll
llmd rollouts resume <instance> -n <namespace> [--watch]    # start them again, on the same machine
llmd rollouts abort <instance> -n <namespace> [--watch]     # re-apply what ran before; it rolls back the same way
llmd rollouts finish <instance> -n <namespace>              # end it once the old revision is gone
```

`--watch` follows the roll and finishes it once every group serves the new revision. A drainer that exits
while old groups remain stops the watch with its log. A group counts as serving when all its pods are Ready
and in the router's pool (`--pool-label`, `ops.llm-d.ai/production-released` by default).

## Development

```sh
cd cli
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest
.venv/bin/ruff check llmd tests
.venv/bin/ruff format --check llmd tests
```

The tests use an in-memory cluster (`tests/fakes.py`) that answers llmd's kubectl calls and acts as the
Deployment and lws controllers; git and kustomize run for real.
