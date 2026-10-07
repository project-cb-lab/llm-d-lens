# Accessing the database from service-layer code

This document is for developers writing **service-layer** code (routers, background
jobs, business logic modules, etc.) who need to read or write structured data that
now lives in the database. It does not cover how the DAO/DAL layer is implemented
internally -- see `docs/design/sqlalchemy-data-access-layer-design.md` for that.

## The short version

- Don't import SQLAlchemy, `Session`, or any `*Row` class in service-layer code.
- Import a `*Dao` class (from `llm_d_bench.db.dao.<table>`) and a plain DTO/record
  type (a Pydantic model or dataclass that already exists in your feature's own
  module, e.g. `StorageVolume`, `ConfigurationArtifactRecord`, `Cluster`).
- Call DAO methods with/returning that plain DTO. The DAO takes care of opening a
  session, committing/rolling back, and converting to/from the ORM row for you.

```python
from llm_d_bench.db.dao.storage_volume import StorageVolumeDao
from llm_d_bench.storage.contracts import NfsSpec, StorageVolume

dao = StorageVolumeDao()

volume = StorageVolume(
    cluster_id=cluster_id,
    name="scratch",
    kind="nfs",
    capacity="100Gi",
    read_only=False,
    nfs=NfsSpec(server="nfs.example.com", path="/exports/scratch"),
)
dao.create(volume)  # write
same = dao.get(volume.id)  # read: StorageVolume | None
everything = dao.list()  # read: list[StorageVolume]
dao.delete(volume.id)  # write
```

That's it for the majority of call sites in this codebase. The rest of this
document covers the handful of things worth knowing beyond the happy path.

## Why a DTO instead of the ORM row directly?

Every DAO method takes/returns a plain object (Pydantic model or dataclass), never
the `*Row` ORM class. Two reasons this matters to you as a caller:

1. **ORM rows are tied to the `Session` they were loaded in.** DAO methods open and
   close their session inside the method call; if a `*Row` instance escaped that
   scope, touching its attributes afterwards could raise `DetachedInstanceError` or
   silently issue a surprise extra query. The DTO you get back has already been
   detached/copied, so it's safe to hold onto, log, return from an API handler,
   `json.dumps` it (via `.model_dump()`), pass across `await` boundaries, etc.
2. **Your feature code shouldn't need to know SQLAlchemy exists.** Business logic,
   validation, and API response models are written against the DTO type your
   feature already defines. If storage ever changes again, only the DAO+Row layer
   needs to change -- service-layer code and tests are unaffected.

## Instantiating a DAO

DAOs are cheap and (mostly) stateless -- constructing one is fine per call, or you
can hold one as an attribute on a longer-lived service/registry object, whichever
reads better for your module:

```python
class MyFeatureStore:
    def __init__(self) -> None:
        self._dao = StorageVolumeDao()
```

Do not cache/reuse a DAO's *session* -- there isn't one to cache. Each DAO method
opens its own short-lived session internally, so it's safe to share one DAO
instance across requests/threads.

## Reads: `get`, `list`, and filtered lookups

Every DAO exposes at least `get(id) -> DTO | None` and `list() -> list[DTO]`. Some
DAOs also expose narrower/filtered queries where the table supports it (check the
specific DAO's module -- e.g. `SimulationTaskDao`, `EvaluateWorkflowDao`). Reads
never need a transaction (no commit is required), so they're always cheap:

```python
artifact = configuration_artifact_dao.get(artifact_id)
if artifact is None:
    raise HTTPException(status_code=404, detail="artifact not found")

recent = configuration_artifact_dao.list()  # already ordered, newest first
```

## Writes: `create`, `save`, `delete`

- `create(dto)` -- insert a brand-new row; raises a DAO-specific `*DaoError` if the
  id already exists (these tables don't silently upsert on `create`).
- `save(dto)` -- update an existing row in place (some DAOs, e.g.
  `EvaluateRunDao.save`, upsert if the row doesn't exist yet -- check the specific
  DAO's docstring/behavior before relying on that).
- `delete(id)` -- idempotent; deleting an id that doesn't exist is a no-op, not an
  error.

```python
from llm_d_bench.db.dao.evaluate_run import EvaluateRunDao, EvaluateDaoError
from llm_d_bench.db.evaluate_persistence_models import EvaluateRunRecord

dao = EvaluateRunDao()

record = EvaluateRunRecord(id=run_id, status="running", cluster_id=cluster_id)
try:
    dao.create(record)
except EvaluateDaoError:
    # id collision -- shouldn't normally happen with generated ids
    ...

record.status = "succeeded"
dao.save(record)
```

## Handling concurrent writes (`ConcurrentUpdateError`)

Tables with a `version_id` optimistic-lock column will raise
`llm_d_bench.db.dao.base.ConcurrentUpdateError` from `save()` if another
transaction updated the same row in between your `get()` and your `save()`. Treat
this the same way you'd treat an HTTP 409: re-fetch, decide whether your change
still applies, and retry (or surface a conflict to the caller).

```python
from llm_d_bench.db.dao.base import ConcurrentUpdateError

try:
    dao.save(record)
except ConcurrentUpdateError:
    raise HTTPException(status_code=409, detail="record was modified concurrently, please retry")
```

## Nested/related data (e.g. a run with its cases)

A few tables have real parent/child relationships (e.g. a deployment run and its
jobs). Their DAOs still hand you a single DTO with the children already populated
as a list field -- you never touch the child `*Row` class or write a join yourself:

```python
from llm_d_bench.db.dao.deployment_batch import DeploymentBatchDao

run = deployment_batch_dao.get(run_id)  # DeploymentRun, with run.cases already loaded
for case in run.cases:
    ...
```

## What NOT to do

- Don't open your own `Session`/`sessionmaker` in service code -- always go through
  a DAO's `create`/`get`/`list`/`save`/`delete` (or another explicitly documented
  method on that DAO).
- Don't import anything from `llm_d_bench.db.models.*` (the `*Row` classes) outside
  of `llm_d_bench/db/dao/*` and `llm_d_bench/db/migrations/*`.
- Don't mutate a DTO you got from `dao.get(...)` and expect that change to persist
  on its own -- you must call `dao.save(...)` with it.
- Don't assume `list()` is paginated/limited unless the specific DAO says so --
  check before calling it in a hot path against a table that can grow large.

## Adding a new table?

That's a DAO/Row-layer concern, not a service-layer one -- see
`docs/design/sqlalchemy-data-access-layer-design.md` (sections 5-6) for how to
define a new `*Row` model, its `column_map`, and its DAO. Once that exists, service
code consumes it exactly like every example above.
