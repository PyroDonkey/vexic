from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from vexic.storage.connection import StorageTarget


def _require(env: Mapping[str, str], name: str) -> str:
    v = env.get(name, "").strip()
    if not v:
        raise ValueError(f"missing required env var: {name}")
    return v


def control_plane_target(env: Mapping[str, str]) -> StorageTarget:
    return StorageTarget(
        _require(env, "TURSO_DATABASE_URL"),
        auth_token=_require(env, "TURSO_AUTH_TOKEN"),
    )


# Customer-memory target resolution arrives with provisioning (P4); for the
# P2 dogfood it reuses the single configured DB.
customer_memory_target = control_plane_target


@dataclass(frozen=True)
class ReconcileReport:
    """Result of reconciling the Turso Platform API's list of databases
    against the control-plane catalog's tenant -> customer_target mapping
    (COA-273 Task 13, P2->P3 split-brain window).

    - ``matched``: tenant_id -> target present both in the catalog and on
      the platform.
    - ``orphan_databases``: platform database identifiers not referenced by
      any tenant in the catalog.
    - ``dangling_targets``: tenant_id -> target referenced by a tenant's
      catalog row but not present among the platform databases.

    Tenants whose ``customer_target`` is ``None`` (local-only storage) are
    excluded from all three collections -- they are not part of the Turso
    split-brain question.
    """

    matched: Mapping[str, str]
    orphan_databases: frozenset[str]
    dangling_targets: Mapping[str, str]


def reconcile_tenant_databases(
    platform_db_targets: Iterable[str],
    catalog_targets: Mapping[str, str | None],
) -> ReconcileReport:
    """Pure reconcile of Turso platform databases against the catalog.

    ``platform_db_targets`` is the set of database identifiers/DSNs the
    caller already obtained from the Turso Platform API's list-databases
    call (no network I/O happens here). ``catalog_targets`` is the
    tenant_id -> customer_target mapping from the control-plane catalog
    (Task 11's column); a ``None`` target means the tenant is local-only
    and is ignored entirely (neither matched nor dangling).

    Comparison is a simple string equality between a platform target and a
    tenant's ``customer_target`` string -- both are expected to be the same
    DSN/identifier form the catalog stores. No normalization beyond that is
    attempted here; callers supplying differently-shaped identifiers (e.g.
    a bare database name vs. a full ``libsql://`` DSN) must normalize before
    calling.

    No network access, no secrets, no env reads -- deterministic given its
    two arguments.
    """
    platform_set = frozenset(platform_db_targets)

    matched: dict[str, str] = {}
    dangling_targets: dict[str, str] = {}
    referenced_targets: set[str] = set()

    for tenant_id, target in catalog_targets.items():
        if target is None:
            continue
        referenced_targets.add(target)
        if target in platform_set:
            matched[tenant_id] = target
        else:
            dangling_targets[tenant_id] = target

    orphan_databases = frozenset(
        db for db in platform_set if db not in referenced_targets
    )

    return ReconcileReport(
        matched=matched,
        orphan_databases=orphan_databases,
        dangling_targets=dangling_targets,
    )
