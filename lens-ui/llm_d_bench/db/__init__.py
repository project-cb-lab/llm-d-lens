"""SQLAlchemy-backed data access layer for llm-d-bench's structured records.

See docs/design/sqlalchemy-data-access-layer-design.md for the full design
(schema, DTO<->DAO mapping mechanism, transaction/concurrency model). This
package is being rolled out module by module (see design doc section 8); at
any point in time only a subset of the 15 target tables may have a Row/
DAO implementation here, with everything else still on the legacy
JSON-file stores.
"""

from __future__ import annotations
