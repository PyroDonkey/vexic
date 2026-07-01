from __future__ import annotations

from collections.abc import Mapping

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
