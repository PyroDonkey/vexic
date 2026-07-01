import contextlib
import importlib.util
import os
import tempfile
import time
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic_ai.messages import ModelRequest, UserPromptPart

from vexic.contract import (
    MemoryCapability,
    MemoryScope,
    Principal,
    PrincipalType,
    SearchTranscriptRequest,
    TrustBoundary,
)
from vexic.hosted import HostedMemoryService, resolve_storage_backend  # new helper
from vexic.hosted_local import HostedApiKeyStore, HostedTenantCatalog
from vexic.service import LocalMemoryService
from vexic.storage import StorageTarget, single_message_adapter
from vexic.storage.connection import connect as storage_connect

# Live gate (Task 8 / COA-273 P2): the round-trip and latency tests below hit
# the real Turso dev DB named by TURSO_DATABASE_URL/TURSO_AUTH_TOKEN, so they
# only run when creds AND the optional `libsql` (vexic[hosted]) extra are
# present. Default `uv run pytest` (no creds) collects but SKIPS them.
_TURSO_URL = os.environ.get("TURSO_DATABASE_URL")
_TURSO_TOKEN = os.environ.get("TURSO_AUTH_TOKEN")
_HAS_TURSO = bool(_TURSO_URL and _TURSO_TOKEN and importlib.util.find_spec("libsql"))


def test_default_is_local():
    assert resolve_storage_backend({}) == "local"


def test_turso_flag_selected():
    assert resolve_storage_backend({"VEXIC_STORAGE_BACKEND": "turso"}) == "turso"


def test_unknown_flag_rejected():
    with pytest.raises(ValueError):
        resolve_storage_backend({"VEXIC_STORAGE_BACKEND": "postgres"})


class _FakeTursoTargetResolver:
    """Test double for the injected resolver seam; never reads real env/secrets."""

    def __init__(self, target: StorageTarget) -> None:
        self._target = target
        self.control_plane_calls = 0
        self.customer_memory_calls = 0

    def control_plane_target(self, env):
        self.control_plane_calls += 1
        return StorageTarget("libsql://fake-control-plane", auth_token="unused")

    def customer_memory_target(self, env):
        self.customer_memory_calls += 1
        return self._target


def _hosted_root(tmp_path: Path) -> Path:
    return tmp_path


def _scope(*, tenant_id: str, capabilities: set[MemoryCapability]) -> MemoryScope:
    return MemoryScope(
        tenant_id=tenant_id,
        project_id="project-a",
        session_id="default",
        agent_id=None,
        principal=Principal(principal_id="caller", principal_type=PrincipalType.HUMAN),
        trust_boundary=TrustBoundary.LOCAL_TRUSTED,
        capabilities=capabilities,
    )


def test_factory_wires_customer_memory_override_from_fake_resolver(monkeypatch, tmp_path):
    from vexic.hosted_http import create_service_from_env

    monkeypatch.setenv("VEXIC_STORAGE_BACKEND", "turso")
    monkeypatch.setenv("VEXIC_HOSTED_ROOT", str(tmp_path))
    fake_target = StorageTarget("libsql://fake", auth_token="x")
    resolver = _FakeTursoTargetResolver(fake_target)

    service = create_service_from_env(turso_target_resolver=resolver)

    assert isinstance(service, HostedMemoryService)
    assert service._customer_memory_target_override == fake_target
    assert resolver.customer_memory_calls == 1


def test_local_service_uses_override_for_db_path(tmp_path, monkeypatch):
    root = _hosted_root(tmp_path)
    catalog = HostedTenantCatalog(root)
    keys = HostedApiKeyStore(root)
    catalog.provision_tenant("tenant-a", project_ids={"project-a"})
    tenant = catalog.get_tenant("tenant-a")
    fake_target = StorageTarget("libsql://fake", auth_token="x")
    # `_local_service` requests schema init against the override target
    # (`init_db`'s process-level memo makes that a cheap no-op after the
    # first real call, but "libsql://fake" is never a reachable host) --
    # this unit test is only about the db_path wiring, so stub init_schema.
    monkeypatch.setattr(LocalMemoryService, "init_schema", lambda self: None)

    service = HostedMemoryService(
        catalog,
        keys,
        customer_memory_target_override=fake_target,
    )
    local_service = service._local_service(tenant)

    assert isinstance(local_service, LocalMemoryService)
    assert local_service.db_path == fake_target


def test_override_guard_raises_on_second_distinct_tenant(tmp_path, monkeypatch):
    root = _hosted_root(tmp_path)
    catalog = HostedTenantCatalog(root)
    keys = HostedApiKeyStore(root)
    catalog.provision_tenant("tenant-a", project_ids={"project-a"})
    catalog.provision_tenant("tenant-b", project_ids={"project-b"})
    tenant_a = catalog.get_tenant("tenant-a")
    tenant_b = catalog.get_tenant("tenant-b")
    fake_target = StorageTarget("libsql://fake", auth_token="x")
    monkeypatch.setattr(LocalMemoryService, "init_schema", lambda self: None)

    service = HostedMemoryService(
        catalog,
        keys,
        customer_memory_target_override=fake_target,
    )

    service._local_service(tenant_a)
    with pytest.raises((PermissionError, RuntimeError)):
        service._local_service(tenant_b)


# ---------------------------------------------------------------------------
# Task 8 (COA-273 P2, live): customer-memory ingest -> search round-trip on
# real Turso, plus a latency guard proving the init-once schema memo (Task 3)
# prevents per-request DDL against the hosted libSQL target.
#
# The control-plane (tenant catalog + API-key store) is built LOCAL on a tmp
# VEXIC_HOSTED_ROOT exactly as `create_service_from_env`'s "local" branch
# does; only VEXIC_STORAGE_BACKEND=turso is set so the resolved customer
# memory target comes from `adapters.turso_adapter` reading
# TURSO_DATABASE_URL/TURSO_AUTH_TOKEN. There is ONE shared Turso dev DB (no
# per-tenant provisioning yet -- that lands in P3/P4), so isolation is via a
# unique session_id/marker per test run, and teardown best-effort deletes the
# rows this run inserted (messages / messages_fts / source_transcript_ledger)
# by that session_id so the shared dev DB does not accumulate test data.


def _turso_env(hosted_root: Path) -> dict[str, str]:
    return {
        "VEXIC_STORAGE_BACKEND": "turso",
        "VEXIC_HOSTED_ROOT": str(hosted_root),
    }


def _provision_single_tenant(root: Path) -> tuple[HostedTenantCatalog, HostedApiKeyStore, str]:
    catalog = HostedTenantCatalog(root)
    keys = HostedApiKeyStore(root)
    catalog.provision_tenant("tenant-live", project_ids={"project-live"})
    raw_key = keys.create_key(
        tenant_id="tenant-live",
        principal_id="agent-live",
        capabilities={MemoryCapability.WRITE, MemoryCapability.SEARCH},
        project_ids={"project-live"},
    ).raw_key
    return catalog, keys, raw_key


def _cleanup_turso_rows(session_id: str) -> None:
    """Best-effort delete of this run's rows from the shared Turso dev DB.

    Deletes by the unique session_id across the three tables a transcript
    write touches: `messages_fts` (shadow FTS table, keyed by session_id),
    `source_transcript_ledger` (references messages.id, must go first to
    respect the FK), then `messages` itself. Never raises -- cleanup failure
    must not fail the test that already asserted its behavior.
    """
    conn = None
    try:
        conn = storage_connect(StorageTarget(_TURSO_URL, auth_token=_TURSO_TOKEN))
        conn.execute("DELETE FROM messages_fts WHERE session_id = ?", (session_id,))
        conn.execute(
            """
            DELETE FROM source_transcript_ledger
            WHERE message_id IN (SELECT id FROM messages WHERE session_id = ?)
            """,
            (session_id,),
        )
        conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
        conn.commit()
    except Exception:  # noqa: BLE001 -- best-effort cleanup on a shared remote DB
        pass
    finally:
        if conn is not None:
            with contextlib.suppress(Exception):
                conn.close()


@pytest.mark.turso
@pytest.mark.skipif(not _HAS_TURSO, reason="Turso creds/libsql missing")
def test_ingest_then_search_round_trip_on_turso(monkeypatch, tmp_path):
    """Live e2e: ingest a source transcript row on real Turso, then find it.

    Builds the hosted app via `create_service_from_env` with
    VEXIC_STORAGE_BACKEND=turso and a LOCAL control-plane rooted at a tmp
    VEXIC_HOSTED_ROOT (mirrors Task 7b: only customer memory routes to
    Turso). Provisions exactly one tenant/project/key in that local
    control-plane, then POSTs `/v1/ingest_source_transcript` with a message
    containing a unique marker and a unique session_id, and confirms
    `/v1/search_transcript` finds it on the shared Turso DB. Asserts the
    Turso auth token never appears anywhere in the response payload.
    """
    marker = f"cedar-{uuid.uuid4().hex[:8]}"
    session_id = f"turso-live-{uuid.uuid4().hex[:12]}"
    for key, value in _turso_env(tmp_path).items():
        monkeypatch.setenv(key, value)

    from vexic.hosted_http import create_service_from_env, create_app

    service = create_service_from_env()
    _catalog, _keys, raw_key = _provision_single_tenant(Path(tmp_path))
    # `create_service_from_env` built its own catalog/key-store rooted at the
    # same VEXIC_HOSTED_ROOT, so provisioning through a second handle to that
    # root is visible to the running service (same filesystem-backed control
    # plane, matching how `create_app`/`create_service_from_env` are used in
    # the real hosted process).
    client = TestClient(create_app(service))
    headers = {
        "Authorization": f"Bearer {raw_key}",
        "X-Vexic-Project-Id": "project-live",
        "X-Vexic-Session-Id": session_id,
    }
    message_json = single_message_adapter.dump_json(
        ModelRequest(parts=[UserPromptPart(content=f"live turso round trip {marker}")])
    ).decode()

    try:
        ingest_response = client.post(
            "/v1/ingest_source_transcript",
            headers=headers,
            json={
                "messages": [
                    {
                        "source_host": "turso-live-test",
                        "source_session_id": session_id,
                        "source_message_id": "msg-1",
                        "message_json": message_json,
                    }
                ],
                "redaction": {"forbidden_values": []},
            },
        )
        search_response = client.post(
            "/v1/search_transcript",
            headers=headers,
            json={"query": marker, "limit": 5},
        )

        assert ingest_response.status_code == 200, ingest_response.text
        assert ingest_response.json()["items"][0]["status"] == "inserted"
        assert search_response.status_code == 200, search_response.text
        hits = search_response.json()["hits"]
        assert len(hits) == 1
        assert marker in hits[0]["body"]

        assert _TURSO_TOKEN not in ingest_response.text
        assert _TURSO_TOKEN not in search_response.text
    finally:
        _cleanup_turso_rows(session_id)


@pytest.mark.turso
@pytest.mark.skipif(not _HAS_TURSO, reason="Turso creds/libsql missing")
def test_search_transcript_p95_latency_stays_under_budget_on_turso(monkeypatch, tmp_path):
    """Latency guard: p95 of 50 sequential search_transcript calls on Turso.

    The init-once schema memo (Task 3, `vexic.storage.schema.init_db`) keys
    on the resolved target so repeated requests against the same hosted
    libSQL database skip DDL after the first call in-process. Without that
    memo every request would re-run `CREATE TABLE IF NOT EXISTS` /
    `CREATE INDEX IF NOT EXISTS` / FTS5 rebuild checks as a network round
    trip, which dominates hosted latency far more than a single search query
    does. 1.5s budget: comfortably above observed network latency to a
    Turso replica from this dev environment, tight enough to fail if
    per-request DDL regresses.

    Observed in this environment (single run, 2026-07-01, dev Turso DB):
    min 0.157s, p50 0.197s, p95 0.952s, max 0.978s -- all well under the
    1.5s budget. (Also recorded in the Task 8 report,
    .superpowers/sdd/task-8-report.md, alongside the -rs run output.)
    """
    marker = f"cedar-{uuid.uuid4().hex[:8]}"
    session_id = f"turso-latency-{uuid.uuid4().hex[:12]}"
    for key, value in _turso_env(tmp_path).items():
        monkeypatch.setenv(key, value)

    from vexic.hosted_http import create_service_from_env, create_app

    service = create_service_from_env()
    client = TestClient(create_app(service))
    _catalog, _keys, raw_key = _provision_single_tenant(Path(tmp_path))
    headers = {
        "Authorization": f"Bearer {raw_key}",
        "X-Vexic-Project-Id": "project-live",
        "X-Vexic-Session-Id": session_id,
    }
    message_json = single_message_adapter.dump_json(
        ModelRequest(parts=[UserPromptPart(content=f"latency guard seed {marker}")])
    ).decode()

    try:
        seed = client.post(
            "/v1/ingest_source_transcript",
            headers=headers,
            json={
                "messages": [
                    {
                        "source_host": "turso-live-test",
                        "source_session_id": session_id,
                        "source_message_id": "seed-1",
                        "message_json": message_json,
                    }
                ],
                "redaction": {"forbidden_values": []},
            },
        )
        assert seed.status_code == 200, seed.text
        assert seed.json()["items"][0]["status"] == "inserted"

        # One untimed warm-up call so any first-call effects other than the
        # schema memo (e.g. TLS/connection setup) don't skew the sample.
        client.post(
            "/v1/search_transcript",
            headers=headers,
            json={"query": marker, "limit": 5},
        )

        durations: list[float] = []
        for _ in range(50):
            started = time.perf_counter()
            response = client.post(
                "/v1/search_transcript",
                headers=headers,
                json={"query": marker, "limit": 5},
            )
            durations.append(time.perf_counter() - started)
            assert response.status_code == 200, response.text
            assert len(response.json()["hits"]) == 1

        durations.sort()
        p95_index = min(len(durations) - 1, int(len(durations) * 0.95))
        p95 = durations[p95_index]

        assert p95 < 1.5, (
            f"p95 search_transcript latency {p95:.3f}s exceeded 1.5s budget "
            f"(all durations: {[round(d, 3) for d in durations]})"
        )
    finally:
        _cleanup_turso_rows(session_id)
