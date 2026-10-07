"""ORM Row classes, one module per migrated table.

Importing this package registers every table on ``Base.metadata``. Keep this
list in one place: ``system_router._create_tables``, the root ``conftest.py``
and ``db/migrations/env.py`` all rely on it, and a missing module here breaks
cross-table foreign keys (e.g. ``clusters.owner_user_id -> users.id``).
"""

from __future__ import annotations

from llm_d_bench.db.models import agentic_benchmark_record as agentic_benchmark_record  # noqa: F401
from llm_d_bench.db.models import ai_provider as ai_provider  # noqa: F401
from llm_d_bench.db.models import audit_log as audit_log  # noqa: F401
from llm_d_bench.db.models import cluster as cluster  # noqa: F401
from llm_d_bench.db.models import configuration_artifact as configuration_artifact  # noqa: F401
from llm_d_bench.db.models import deployment_batch as deployment_batch  # noqa: F401
from llm_d_bench.db.models import deployment_evidence as deployment_evidence  # noqa: F401
from llm_d_bench.db.models import deployment_job as deployment_job  # noqa: F401
from llm_d_bench.db.models import evaluate_run as evaluate_run  # noqa: F401
from llm_d_bench.db.models import evaluate_workflow as evaluate_workflow  # noqa: F401
from llm_d_bench.db.models import group as group  # noqa: F401
from llm_d_bench.db.models import identity_provider as identity_provider  # noqa: F401
from llm_d_bench.db.models import model_access_token as model_access_token  # noqa: F401
from llm_d_bench.db.models import model_cache_entry as model_cache_entry  # noqa: F401
from llm_d_bench.db.models import (  # noqa: F401
    model_service_gateway_operation as model_service_gateway_operation,
)
from llm_d_bench.db.models import model_service_group as model_service_group  # noqa: F401
from llm_d_bench.db.models import model_service_member as model_service_member  # noqa: F401
from llm_d_bench.db.models import (  # noqa: F401
    model_service_usage_snapshot as model_service_usage_snapshot,
)
from llm_d_bench.db.models import (  # noqa: F401
    monitoring_accelerator_operation as monitoring_accelerator_operation,
)
from llm_d_bench.db.models import (  # noqa: F401
    monitoring_cluster_stack_operation as monitoring_cluster_stack_operation,
)
from llm_d_bench.db.models import role as role  # noqa: F401
from llm_d_bench.db.models import session as session  # noqa: F401
from llm_d_bench.db.models import simulation_task as simulation_task  # noqa: F401
from llm_d_bench.db.models import storage_volume as storage_volume  # noqa: F401
from llm_d_bench.db.models import usage_record as usage_record  # noqa: F401
from llm_d_bench.db.models import user as user  # noqa: F401
