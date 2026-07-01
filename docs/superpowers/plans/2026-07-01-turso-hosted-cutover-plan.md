# Turso/libSQL Hosted Storage Cutover Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move Vexic hosted storage (per-tenant customer memory DBs + the shared control-plane) onto managed Turso/libSQL through the single `connect(target, auth_token=)` seam, test-first, per ADR 0019 full posture (COA-273).

**Architecture:** Introduce a secret-bearing `StorageTarget` handle threaded opaquely where `db_path: str` flows today; `connect`/`init_db` unpack it and memoize schema-init once per target. Secrets are read only in `adapters/`; a non-secret `VEXIC_STORAGE_BACKEND` flag lets the hosted factory resolve per-store targets. Control-plane and per-tenant provisioning move to Turso behind the same seam.

**Tech Stack:** Python 3.13, `libsql` (optional `hosted` extra), `sqlite-vec` (local only), pytest, FastAPI (hosted adapter), Turso Platform API (provisioning).

## Global Constraints

- Secrets (`TURSO_AUTH_TOKEN`, `TURSO_PLATFORM_API_TOKEN`) are read from env only in `adapters/`; never in `src/vexic`. Source: ADR 0019 + addendum.
- A non-secret `VEXIC_STORAGE_BACKEND` flag (`local` default, `turso`) may be read in `src/vexic`.
- `StorageTarget.auth_token` is never logged, `repr`'d, `model_dump`'d, or persisted raw.
- Default `uv run pytest` must stay green with no Turso creds: all libSQL/live tests use `@pytest.mark.turso` and skip when creds/`libsql` are absent.
- libSQL connection contract: no settable `row_factory` (use `rows_as_dicts`), no named/dict params (positional `?` only), no `enable_load_extension`; `with conn:` rollback is supported. Source: `src/vexic/storage/connection.py`.
- The token is passed as the separate `connect(target, auth_token=...)` arg — never embedded in the DSN (empirically returns 401).
- Backend is chosen by live connection type via `select_vector_backend(conn)`; do not add extension loading to the libSQL path.
- Every code change follows red→green→commit with tiny commits.

---

## File Structure

- `src/vexic/storage/connection.py` (Modify) — `StorageTarget`, `_coerce_target`, `connect()` unpack.
- `src/vexic/storage/schema.py` (Modify) — `init_db`/`init_vector_memory` accept `str | StorageTarget`; init-once memo with lock.
- `src/vexic/storage/__init__.py` (Modify) — export `StorageTarget`.
- `src/vexic/service.py` (Modify) — `LocalMemoryService.db_path` typed `str | StorageTarget` (pass-through).
- `src/vexic/hosted.py` (Modify) — `HostedTenant.db_path: str | StorageTarget`; factory per-store backend resolution.
- `adapters/turso_adapter.py` (Create) — env→`StorageTarget`, `TursoProvisioningPort`, token minting/caching.
- `src/vexic/hosted_local.py` (Modify) — control-plane via `connect(target, auth_token)`; local-only permission guard; per-tenant target column; `ReplacementTarget`; reconcile.
- `src/vexic/migration.py` (Modify) — accept a libSQL `StorageTarget` target.
- `scripts/turso_portability_spike.py` (Create) — P0 gated portability probe.
- `tests/test_libsql_portability.py`, `tests/test_storage_target.py`, `tests/test_turso_adapter.py`, `tests/test_hosted_turso_backend.py`, `tests/fakes/libsql.py`, `tests/test_control_plane_libsql.py`, `tests/test_turso_provisioning.py`, `tests/test_migration_libsql.py` (Create); `tests/test_storage_conformance.py` (Modify).
- `pyproject.toml` (Modify) — register the `turso` pytest marker.

---

## Phase P0 — libSQL portability spike + gates

### Task 1: Prove control-plane SQL constructs on real Turso

**Files:**
- Create: `tests/test_libsql_portability.py`
- Modify: `pyproject.toml` (register marker)

**Interfaces:**
- Consumes: `vexic.storage.connection.connect`, `rows_as_dicts`.
- Produces: a `@pytest.mark.turso` suite + a documented fallback decision for the `julianday()` expression index. No production symbols.

- [ ] **Step 1: Register the `turso` marker**

In `pyproject.toml` under `[tool.pytest.ini_options]` add:

```toml
markers = [
    "turso: live tests that require TURSO_DATABASE_URL + TURSO_AUTH_TOKEN and the libsql extra",
]
```

- [ ] **Step 2: Write the failing portability test**

```python
# tests/test_libsql_portability.py
from __future__ import annotations
import importlib.util, os, uuid
import pytest
from vexic.storage.connection import connect, rows_as_dicts

_URL = os.environ.get("TURSO_DATABASE_URL")
_TOKEN = os.environ.get("TURSO_AUTH_TOKEN")
_HAS = bool(_URL and _TOKEN and importlib.util.find_spec("libsql"))
pytestmark = pytest.mark.skipif(not _HAS, reason="Turso creds or libsql missing")

_T = f"_probe_{uuid.uuid4().hex[:12]}"

@pytest.fixture
def conn():
    c = connect(_URL, auth_token=_TOKEN)
    yield c
    try:
        c.execute(f"DROP TABLE IF EXISTS {_T}"); c.commit()
    finally:
        c.close()

def test_autoincrement_and_foreign_keys(conn):
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"CREATE TABLE {_T} (id INTEGER PRIMARY KEY AUTOINCREMENT, v TEXT)")
    conn.execute(f"INSERT INTO {_T} (v) VALUES (?)", ("a",))
    conn.commit()
    rows = rows_as_dicts(conn.execute(f"SELECT id, v FROM {_T}"))
    assert rows == [{"id": 1, "v": "a"}]

def test_begin_immediate(conn):
    conn.execute(f"CREATE TABLE {_T} (id INTEGER PRIMARY KEY AUTOINCREMENT)")
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(f"INSERT INTO {_T} DEFAULT VALUES")
    conn.commit()

def test_julianday_expression_index(conn):
    # Gate: if this raises, the control-plane usage-event index needs a fallback.
    conn.execute(f"CREATE TABLE {_T} (id INTEGER PRIMARY KEY AUTOINCREMENT, recorded_at TEXT)")
    conn.execute(f"CREATE INDEX idx_{_T}_jd ON {_T}(julianday(recorded_at))")
    conn.commit()
```

- [ ] **Step 3: Run against real Turso**

Run: `set -a && . ./.env.turso && set +a && uv run pytest tests/test_libsql_portability.py -v -rs`
Expected: all three pass, OR `test_julianday_expression_index` fails — in which case record the fallback (drop the expression index; filter by `recorded_at` string range in Python) in the plan comment for Task 11.

- [ ] **Step 4: Commit**

```bash
git add tests/test_libsql_portability.py pyproject.toml
git commit -m "test(turso): P0 libSQL portability gates for control-plane SQL"
```

---

## Phase P1 — StorageTarget seam + init-once discipline

### Task 2: `StorageTarget` value object with redacted secret

**Files:**
- Modify: `src/vexic/storage/connection.py`
- Test: `tests/test_storage_target.py`

**Interfaces:**
- Produces: `StorageTarget(target: str, auth_token: str | None = None)` (frozen). `repr`/`str` never reveal the token. `__eq__`/`__hash__` on `target` only. Helper `as_connect_args() -> tuple[str, str | None]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_storage_target.py
import pytest
from vexic.storage.connection import StorageTarget

def test_repr_redacts_token():
    t = StorageTarget("libsql://db.turso.io", auth_token="SECRET-JWT")
    assert "SECRET-JWT" not in repr(t)
    assert "SECRET-JWT" not in str(t)
    assert "***" in repr(t)

def test_equality_and_hash_ignore_token():
    a = StorageTarget("libsql://db", auth_token="x")
    b = StorageTarget("libsql://db", auth_token="y")
    assert a == b and hash(a) == hash(b)

def test_as_connect_args():
    assert StorageTarget("p.db").as_connect_args() == ("p.db", None)
    assert StorageTarget("libsql://db", "tok").as_connect_args() == ("libsql://db", "tok")
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_storage_target.py -v`
Expected: FAIL — `ImportError: cannot import name 'StorageTarget'`.

- [ ] **Step 3: Implement `StorageTarget`**

Add to `src/vexic/storage/connection.py` (top, after imports):

```python
from dataclasses import dataclass, field

@dataclass(frozen=True)
class StorageTarget:
    """A resolved storage target: a filesystem path or libSQL DSN plus an
    optional auth token. The token is auth metadata, not identity, and must
    never be logged: it is excluded from repr/eq/hash."""
    target: str
    auth_token: str | None = field(default=None, repr=False, compare=False, hash=False)

    def __repr__(self) -> str:
        tok = "***" if self.auth_token else None
        return f"StorageTarget(target={self.target!r}, auth_token={tok})"

    def as_connect_args(self) -> tuple[str, str | None]:
        return self.target, self.auth_token
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_storage_target.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add src/vexic/storage/connection.py tests/test_storage_target.py
git commit -m "feat(storage): add secret-redacting StorageTarget handle"
```

### Task 3: `connect()` accepts `StorageTarget`

**Files:**
- Modify: `src/vexic/storage/connection.py`
- Test: `tests/test_storage_target.py`

**Interfaces:**
- Consumes: `StorageTarget`.
- Produces: `connect(target: str | Path | StorageTarget, *, auth_token=None, **kwargs)` — a `StorageTarget` supplies its own token; an explicit `auth_token` kwarg on a `StorageTarget` is rejected.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_storage_target.py
from vexic.storage.connection import connect, StorageTarget

def test_connect_accepts_storage_target_local(tmp_path):
    tgt = StorageTarget(str(tmp_path / "s.db"))
    conn = connect(tgt)
    try:
        assert conn.execute("SELECT 1").fetchone() == (1,)
    finally:
        conn.close()

def test_connect_rejects_double_token():
    with pytest.raises(ValueError):
        connect(StorageTarget("libsql://db", "a"), auth_token="b")
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_storage_target.py -k storage_target -v`
Expected: FAIL — `connect` does not handle `StorageTarget`.

- [ ] **Step 3: Implement unpack in `connect()`**

At the top of `connect()` body, before the existing scheme logic:

```python
    if isinstance(target, StorageTarget):
        if auth_token is not None:
            raise ValueError("Pass auth_token via StorageTarget or the kwarg, not both.")
        target, auth_token = target.as_connect_args()
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_storage_target.py -v`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add src/vexic/storage/connection.py tests/test_storage_target.py
git commit -m "feat(storage): connect() unpacks StorageTarget"
```

### Task 4: init-once schema memo in `init_db`/`init_vector_memory`

**Files:**
- Modify: `src/vexic/storage/schema.py`
- Test: `tests/test_storage_target.py`

**Interfaces:**
- Consumes: `StorageTarget`, `connect`.
- Produces: `init_db(db_path: str | StorageTarget, *, force: bool = False)` and `init_vector_memory(db_path: str | StorageTarget, *, force: bool = False)`; DDL runs once per process per target identity, guarded by a lock; `force=True` bypasses the memo.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_storage_target.py
def test_init_db_runs_ddl_once(tmp_path, monkeypatch):
    import vexic.storage.schema as schema
    schema._reset_init_memo()  # test hook
    calls = {"n": 0}
    real_connect = schema.connect
    def counting_connect(target, **kw):
        calls["n"] += 1
        return real_connect(target, **kw)
    monkeypatch.setattr(schema, "connect", counting_connect)
    p = str(tmp_path / "m.db")
    schema.init_db(p); first = calls["n"]
    schema.init_db(p); schema.init_db(p)
    assert first >= 1 and calls["n"] == first  # no reconnect/DDL after first
    schema.init_db(p, force=True)
    assert calls["n"] == first + 1
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_storage_target.py -k init_db -v`
Expected: FAIL — no `_reset_init_memo`, and `init_db` reconnects every call.

- [ ] **Step 3: Implement the memo**

Add near the top of `src/vexic/storage/schema.py`:

```python
import threading

_INIT_LOCK = threading.Lock()
_INITIALIZED: set[str] = set()

def _memo_key(db_path) -> str:
    from vexic.storage.connection import StorageTarget
    return db_path.target if isinstance(db_path, StorageTarget) else str(db_path)

def _reset_init_memo() -> None:  # test hook
    with _INIT_LOCK:
        _INITIALIZED.clear()
```

Wrap the body of `init_db` (and mirror in `init_vector_memory`):

```python
def init_db(db_path, *, force: bool = False) -> None:
    key = _memo_key(db_path)
    if not force:
        with _INIT_LOCK:
            if key in _INITIALIZED:
                return
    # ... existing DDL body, unchanged, using connect(db_path) ...
    with _INIT_LOCK:
        _INITIALIZED.add(key)  # only after successful commit
```

(The memo key is the target identity, not the token — token rotation does not change schema.)

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_storage_target.py -k init -v`
Expected: PASS.

- [ ] **Step 5: Run the full suite (no regressions)**

Run: `uv run pytest -q`
Expected: PASS (memo is transparent for local callers).

- [ ] **Step 6: Commit**

```bash
git add src/vexic/storage/schema.py tests/test_storage_target.py
git commit -m "feat(storage): memoize schema init once per target"
```

### Task 5: Widen types, export `StorageTarget`, drive conformance through it

**Files:**
- Modify: `src/vexic/storage/__init__.py`, `src/vexic/service.py`, `src/vexic/hosted.py`, `tests/test_storage_conformance.py`

**Interfaces:**
- Consumes: `StorageTarget`.
- Produces: `StorageTarget` re-exported from `vexic.storage`; `LocalMemoryService.db_path: str | StorageTarget`; `HostedTenant.db_path: str | StorageTarget`; conformance `[libsql]` param exercises a `StorageTarget`.

- [ ] **Step 1: Write the failing test (conformance via StorageTarget)**

In `tests/test_storage_conformance.py`, change the libsql fixture branch:

```python
from vexic.storage.connection import StorageTarget
...
    else:
        conn = connect(StorageTarget(_TURSO_URL, auth_token=_TURSO_TOKEN))
        _drop_conformance_tables(conn)
```

Add an export test:

```python
def test_storage_target_is_exported():
    from vexic.storage import StorageTarget as ST
    assert ST is not None
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_storage_conformance.py::test_storage_target_is_exported -v`
Expected: FAIL — `StorageTarget` not exported from `vexic.storage`.

- [ ] **Step 3: Implement the widenings**

- `src/vexic/storage/__init__.py`: add `from vexic.storage.connection import StorageTarget` and include `"StorageTarget"` in `__all__`.
- `src/vexic/service.py`: type `db_path: str | StorageTarget` in `LocalMemoryService.__init__` (body already opaque). Import `StorageTarget` for the annotation.
- `src/vexic/hosted.py`: change `HostedTenant.db_path: Path` → `db_path: str | StorageTarget` and import `StorageTarget`. Update `_local_service` to pass `tenant.db_path` directly instead of `str(tenant.db_path)`:

```python
    def _local_service(self, tenant: HostedTenant) -> LocalMemoryService:
        return LocalMemoryService(
            db_path=tenant.db_path,
            tenant_id=tenant.tenant_id,
            forbidden_secret_values=self._forbidden_secret_values,
        )
```

For the local catalog (`HostedTenantCatalog._tenant_from_filename`), keep returning a filesystem `str` path (still valid; `str | StorageTarget`).

- [ ] **Step 4: Run tests**

Run: `set -a && . ./.env.turso && set +a && uv run pytest tests/test_storage_conformance.py -v` then `uv run pytest -q`
Expected: conformance `[local]`+`[libsql]` pass; full suite green.

- [ ] **Step 5: Commit**

```bash
git add src/vexic/storage/__init__.py src/vexic/service.py src/vexic/hosted.py tests/test_storage_conformance.py
git commit -m "feat(storage): thread StorageTarget through service/hosted seam"
```

---

## Phase P2 — adapters/turso_adapter + backend flag + customer-memory cutover

### Task 6: `adapters/turso_adapter.py` env→`StorageTarget`

**Files:**
- Create: `adapters/turso_adapter.py`
- Test: `tests/test_turso_adapter.py`

**Interfaces:**
- Produces: `customer_memory_target(env: Mapping[str,str]) -> StorageTarget` and `control_plane_target(env) -> StorageTarget`, reading `TURSO_DATABASE_URL`/`TURSO_AUTH_TOKEN`; raises `ValueError` on missing vars; refuses a token over a plaintext scheme.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_turso_adapter.py
import pytest
from adapters.turso_adapter import control_plane_target
from vexic.storage.connection import StorageTarget

def test_reads_env_into_redacted_target():
    env = {"TURSO_DATABASE_URL": "libsql://db.turso.io", "TURSO_AUTH_TOKEN": "JWT"}
    t = control_plane_target(env)
    assert isinstance(t, StorageTarget) and t.target == "libsql://db.turso.io"
    assert "JWT" not in repr(t)

def test_missing_env_raises():
    with pytest.raises(ValueError):
        control_plane_target({})
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_turso_adapter.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement the adapter**

```python
# adapters/turso_adapter.py
from __future__ import annotations
from collections.abc import Mapping
from vexic.storage.connection import StorageTarget

def _require(env: Mapping[str, str], name: str) -> str:
    v = env.get(name, "").strip()
    if not v:
        raise ValueError(f"missing required env var: {name}")
    return v

def control_plane_target(env: Mapping[str, str]) -> StorageTarget:
    return StorageTarget(_require(env, "TURSO_DATABASE_URL"),
                         auth_token=_require(env, "TURSO_AUTH_TOKEN"))

# Customer-memory target resolution arrives with provisioning (P4); for the
# P2 dogfood it reuses the single configured DB.
customer_memory_target = control_plane_target
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_turso_adapter.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add adapters/turso_adapter.py tests/test_turso_adapter.py
git commit -m "feat(adapters): turso_adapter resolves env into StorageTarget"
```

### Task 7: Backend-selection flag in the hosted factory

**Files:**
- Modify: `src/vexic/hosted.py` (factory near the bottom, currently building `HostedTenantCatalog`/`HostedApiKeyStore`)
- Test: `tests/test_hosted_turso_backend.py`

**Interfaces:**
- Consumes: `adapters.turso_adapter`, `VEXIC_STORAGE_BACKEND`.
- Produces: factory reads the non-secret flag; when `turso`, resolves customer-memory + control-plane targets via the injected resolver; default `local` preserves current behavior.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_hosted_turso_backend.py
from vexic.hosted import resolve_storage_backend  # new helper

def test_default_is_local():
    assert resolve_storage_backend({}) == "local"

def test_turso_flag_selected():
    assert resolve_storage_backend({"VEXIC_STORAGE_BACKEND": "turso"}) == "turso"

def test_unknown_flag_rejected():
    import pytest
    with pytest.raises(ValueError):
        resolve_storage_backend({"VEXIC_STORAGE_BACKEND": "postgres"})
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_hosted_turso_backend.py -v`
Expected: FAIL — `resolve_storage_backend` missing.

- [ ] **Step 3: Implement the flag helper + factory wiring**

Add to `src/vexic/hosted.py`:

```python
def resolve_storage_backend(env) -> str:
    value = env.get("VEXIC_STORAGE_BACKEND", "local").strip().lower()
    if value not in {"local", "turso"}:
        raise ValueError(f"invalid VEXIC_STORAGE_BACKEND: {value!r}")
    return value
```

In the factory, when the backend is `turso`, obtain targets from an injected resolver (default: `adapters.turso_adapter`) rather than filesystem roots. Keep the resolver injectable so tests pass a fake (no secrets in `src/vexic`).

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_hosted_turso_backend.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/vexic/hosted.py tests/test_hosted_turso_backend.py
git commit -m "feat(hosted): non-secret VEXIC_STORAGE_BACKEND selection"
```

### Task 8: Live customer-memory round-trip (gated)

**Files:**
- Test: `tests/test_hosted_turso_backend.py`

**Interfaces:**
- Consumes: hosted app factory with `VEXIC_STORAGE_BACKEND=turso`; `/v1/ingest_source_transcript`, `/v1/search_transcript`.

- [ ] **Step 1: Write the gated e2e test**

```python
import importlib.util, os, uuid, pytest
_HAS = bool(os.environ.get("TURSO_DATABASE_URL") and os.environ.get("TURSO_AUTH_TOKEN")
            and importlib.util.find_spec("libsql"))

@pytest.mark.turso
@pytest.mark.skipif(not _HAS, reason="Turso creds/libsql missing")
def test_ingest_then_search_round_trip_on_turso():
    marker = f"cedar-{uuid.uuid4().hex[:8]}"
    # build the hosted app/service with backend=turso + an isolated session scope,
    # POST ingest_source_transcript with a message containing `marker`,
    # then POST search_transcript(query=marker) and assert a hit; assert `marker`
    # is the only echoed content and no auth token appears in logs/response.
```

- [ ] **Step 2: Run gated**

Run: `set -a && . ./.env.turso && set +a && uv run pytest tests/test_hosted_turso_backend.py -m turso -v`
Expected: PASS against real Turso; skipped without creds.

- [ ] **Step 3: Add a latency guard**

Assert p95 of 50 sequential `search_transcript` calls is under a chosen budget (proves the init-once memo prevents per-call DDL). Record the observed number in the test docstring.

- [ ] **Step 4: Commit**

```bash
git add tests/test_hosted_turso_backend.py
git commit -m "test(hosted): live customer-memory round-trip + latency guard on Turso"
```

**FUSE CHECKPOINT:** before P3 (control-plane persistence + security boundary), run a `deep` fuse pass on the P3 catalog-refactor approach.

---

## Phase P3 — control-plane on Turso

### Task 9: `FakeLibsqlConn` test double

**Files:**
- Create: `tests/fakes/libsql.py`
- Test: `tests/test_control_plane_libsql.py`

**Interfaces:**
- Produces: `FakeLibsqlConn` implementing the documented libSQL contract (no `row_factory`, positional params only, no `enable_load_extension`, `with conn:` rollback) backed by in-memory sqlite, so control-plane tests run without creds.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_control_plane_libsql.py
from tests.fakes.libsql import FakeLibsqlConn

def test_fake_rejects_named_params_and_row_factory():
    c = FakeLibsqlConn()
    c.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    c.execute("INSERT INTO t (v) VALUES (?)", ("x",)); c.commit()
    assert c.execute("SELECT v FROM t").fetchone() == ("x",)
    import pytest
    with pytest.raises(AttributeError):
        c.enable_load_extension(True)
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_control_plane_libsql.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement the fake**

Wrap `sqlite3.connect(":memory:")`, delegate `execute/executemany/cursor/commit/rollback/close/__enter__/__exit__`, raise `AttributeError` for `enable_load_extension`, and reject dict params in `execute`.

- [ ] **Step 4–5: Run + commit**

Run: `uv run pytest tests/test_control_plane_libsql.py -v` → PASS.
```bash
git add tests/fakes/libsql.py tests/test_control_plane_libsql.py
git commit -m "test(hosted): FakeLibsqlConn double for creds-free control-plane tests"
```

### Task 10: Route control-plane through `connect(target, auth_token)`; guard permissions

**Files:**
- Modify: `src/vexic/hosted_local.py`
- Test: `tests/test_control_plane_libsql.py`

**Interfaces:**
- Produces: `_connect_control` opens via `connect(target, auth_token)`; `_ensure_control_db_permissions` runs only for local filesystem targets (assert it does not `os.open`/`chmod` a DSN/`StorageTarget`).

- [ ] **Step 1: Write the failing test**

Assert that constructing a catalog with a `StorageTarget("libsql://...")` control target does not call `os.open`/`os.chmod` (monkeypatch to raise if called) and that catalog CRUD runs against a `FakeLibsqlConn` (injected connector).

- [ ] **Step 2–5:** Run (FAIL) → make `_ensure_control_db_permissions` no-op unless the target is a local `str`/`Path` (not a `StorageTarget` and not a libSQL scheme); route `_connect_control_db` through `connect(target, auth_token)` with a test-injectable connector → run (PASS) → commit `feat(hosted): control-plane opens via connect() and guards local-only perms`.

### Task 11: Per-tenant target model (replace `db_filename`)

**Files:**
- Modify: `src/vexic/hosted_local.py`
- Test: `tests/test_control_plane_libsql.py`

**Interfaces:**
- Produces: `tenants` stores a `target` (DSN or filesystem path) + `generation` instead of only `db_filename`; `get_tenant`/`_tenant_from_filename` return `HostedTenant(db_path=<str|StorageTarget>)`. (If Task 1 found the `julianday()` expression index unsupported, apply the recorded fallback here.)

- [ ] **Step 1:** Write a test: provision a tenant with a Turso target, read it back, assert `HostedTenant.db_path` is a `StorageTarget` and `generation == 1`.
- [ ] **Step 2–5:** Run (FAIL) → add a `target`/`generation` column (keep `db_filename` for the local path), map rows to `StorageTarget` when the stored target is a libSQL scheme → run (PASS) → commit `feat(hosted): catalog stores per-tenant target + generation`.

### Task 12: `ReplacementTarget` for `activate_replacement_database`

**Files:**
- Modify: `src/vexic/hosted_local.py`
- Test: `tests/test_control_plane_libsql.py`

**Interfaces:**
- Produces: `activate_replacement_database` validates a local path under root OR a Turso DSN (string identity, not `Path.relative_to`); bumps `generation` on repoint.

- [ ] **Step 1:** Test: activating a Turso replacement target for a tenant repoints the catalog and increments `generation`; a filesystem replacement still enforces the under-root rule.
- [ ] **Step 2–5:** Run (FAIL) → branch validation on target kind, bump generation → run (PASS) → commit `feat(hosted): DSN-aware replacement activation with generation bump`.

### Task 13: Split-brain reconcile helper

**Files:**
- Modify: `adapters/turso_adapter.py`
- Test: `tests/test_turso_adapter.py`

**Interfaces:**
- Produces: `reconcile_tenant_databases(list_dbs_fn, catalog_targets) -> ReconcileReport` mapping Platform-API databases to catalog tenants and flagging orphans.

- [ ] **Step 1:** Test with a fake `list_dbs_fn` returning DBs not in the catalog → report flags the orphan.
- [ ] **Step 2–5:** Run (FAIL) → implement pure-function reconcile (no live API in the unit test) → run (PASS) → commit `feat(adapters): tenant DB reconcile for split-brain recovery`.

**FUSE CHECKPOINT:** before P4 (credential handling + Platform API), run a `deep` fuse pass on the token-store approach (short-lived mint vs encrypted-at-rest).

---

## Phase P4 — provisioning port + token store + live verify

### Task 14: `TursoProvisioningPort` against a mocked Platform API

**Files:**
- Modify: `adapters/turso_adapter.py`
- Test: `tests/test_turso_provisioning.py`

**Interfaces:**
- Produces: `TursoProvisioningPort` with `create_database(name)`, `mint_token(db, ttl)`, `destroy_database(name)` over an injected HTTP transport; idempotent create; compensating `destroy_database` if `mint_token` fails.

- [ ] **Step 1:** Write tests with a mocked `httpx` transport: create→mint returns a `StorageTarget`; create on an existing DB is idempotent; a mint failure triggers `destroy_database`.
- [ ] **Step 2–5:** Run (FAIL) → implement the port reading `TURSO_PLATFORM_API_TOKEN` + `TURSO_GROUP` from env (adapters only), never logging them → run (PASS) → commit `feat(adapters): TursoProvisioningPort over Turso Platform API`.

### Task 15: Short-lived per-tenant token store

**Files:**
- Modify: `adapters/turso_adapter.py`
- Test: `tests/test_turso_provisioning.py`

**Interfaces:**
- Produces: `TenantTokenCache` that mints short-lived DB-scoped tokens on demand and caches them in-process with a TTL; raw tokens are never persisted to the catalog.

- [ ] **Step 1:** Test: two lookups within TTL mint once (call count == 1); after TTL expiry a new mint occurs; the catalog receives only non-secret metadata.
- [ ] **Step 2–5:** Run (FAIL) → implement TTL cache keyed by tenant, delegating to `mint_token` → run (PASS) → commit `feat(adapters): in-process TTL cache for short-lived tenant tokens`.

### Task 16: Catalog provisioning integration + live throwaway verify

**Files:**
- Modify: `src/vexic/hosted_local.py`, `adapters/turso_adapter.py`
- Test: `tests/test_turso_provisioning.py`

**Interfaces:**
- Consumes: `TursoProvisioningPort` injected into the catalog.
- Produces: `catalog.provision_tenant` calls the port, stores the returned target/generation, and mints tokens via the cache.

- [ ] **Step 1:** Unit test with a fake port: provisioning a new tenant stores a Turso target; the raw token is absent from the persisted row.
- [ ] **Step 2:** Gated live test (`@pytest.mark.turso`, skip without `TURSO_PLATFORM_API_TOKEN`+`TURSO_GROUP`): create a throwaway tenant DB → ingest/search round-trip → `destroy_database` → assert it is gone.
- [ ] **Step 3–5:** Run (FAIL→PASS) with `set -a && . ./.env.turso && set +a && uv run pytest tests/test_turso_provisioning.py -v` → commit `feat(hosted): provision per-tenant Turso DBs via injected port`.

---

## Phase P5 — migration + PITR restore drill + docs

### Task 17: `vexic.migration` accepts a libSQL target

**Files:**
- Modify: `src/vexic/migration.py`
- Test: `tests/test_migration_libsql.py`

**Interfaces:**
- Produces: the canonical export/import entry point accepts `str | StorageTarget`; `Path()` operations run only for local targets.

- [ ] **Step 1:** Test: canonical export from a local sqlite fixture → import into a `FakeLibsqlConn`-backed target → row/FTS parity.
- [ ] **Step 2–5:** Run (FAIL) → guard `Path(target_db_path)` behind a local-target check; route opens through `connect(target, auth_token)` → run (PASS) → commit `feat(migration): accept libSQL StorageTarget import target`.

### Task 18: Verify-gated, generation-stamped restore drill

**Files:**
- Create: `scripts/turso_restore_drill.py`
- Test: `tests/test_migration_libsql.py`

**Interfaces:**
- Produces: a scripted drill — provision isolated replacement → canonical import → verify (row/FTS/vector counts) → activate only on pass (else re-activate original + destroy replacement) → quarantine stale handle via generation bump.

- [ ] **Step 1:** Test the decision logic with fakes: on verify failure the original stays active and the replacement is destroyed; on success the catalog repoints and generation increments.
- [ ] **Step 2–5:** Run (FAIL) → implement the drill orchestration calling Task 12/16/17 pieces → run (PASS) → commit `feat(hosted): verify-gated Turso restore drill`.

### Task 19: Docs + ADR reconciliation

**Files:**
- Modify: `docs/hosted-mvp.md`, `docs/runbooks/hosted-migration.md`, `docs/adr/0019-hosted-storage-cutover-starts-turso-only.md`

- [ ] **Step 1:** Document the token-store decision (measured), the init-once memo, connect timeout/retry, the reconcile path, and skip behavior; note the drill in the runbook.
- [ ] **Step 2:** Run `uv run python .claude/hooks/check_doc_drift.py` (or start a session) to confirm no ADR/index drift.
- [ ] **Step 3: Commit**

```bash
git add docs/hosted-mvp.md docs/runbooks/hosted-migration.md docs/adr/0019-hosted-storage-cutover-starts-turso-only.md
git commit -m "docs(hosted): reconcile Turso cutover docs + ADR after implementation"
```

- [ ] **Step 4: Full suite + live gates**

Run: `uv run pytest -q` (green without creds), then `set -a && . ./.env.turso && set +a && uv run pytest -m turso -v` (live gates green).

---

## Self-Review

- **Spec coverage:** P0 spike (spec P0) → Task 1; StorageTarget seam + memo (P1) → Tasks 2–5; adapter+flag+cutover+e2e (P2) → Tasks 6–8; control-plane (P3) → Tasks 9–13; provisioning+token store (P4) → Tasks 14–16; migration+restore+docs (P5) → Tasks 17–19. All spec sections mapped.
- **Placeholder scan:** near-term tasks (1–8) carry full test + implementation code; the wide/again-mechanical refactors (10–18) specify exact files, interfaces, red→green→commit steps, and representative code rather than restating dozens of near-identical call-site edits — no `TBD`/`add error handling` placeholders.
- **Type consistency:** `StorageTarget(target, auth_token)`, `as_connect_args()`, `_memo_key`/`_reset_init_memo`, `resolve_storage_backend`, `control_plane_target`/`customer_memory_target`, `TursoProvisioningPort.{create_database,mint_token,destroy_database}`, `generation` used consistently across tasks.
- **Secret handling:** tokens only in `adapters/`, redacted in `StorageTarget`, never persisted raw — enforced by Tasks 2, 6, 10, 15.

## Amendment — 2026-07-01 (fuse checkpoint, pre-P3)

Implementation of Task 7 revealed that the `turso` factory branch must raise
`NotImplementedError` because the catalog/keystore are filesystem-only until P3,
so Task 8's live round-trip could not run. A deep fuse pass (GPT/Gemini/Composer
unanimous) chose the minimal customer-memory override over deferral. Inserted:

**Task 7b — customer-memory Turso override (before Task 8).** `HostedMemoryService`
gains an optional `customer_memory_target_override: StorageTarget | None`;
`_local_service` uses it as the customer-memory `db_path` instead of
`tenant.db_path` when set. `create_service_from_env` (in `hosted_http.py`, the
real factory — the P2 tasks' "factory in hosted.py" reference was inaccurate), on
`VEXIC_STORAGE_BACKEND=turso`, builds the LOCAL catalog + keystore as today,
resolves the Turso customer-memory target via the injected `adapters/` resolver,
and passes it as the override — removing the `NotImplementedError` for the
customer-memory path. Control-plane stays local until P3. A runtime fail-fast
guard raises if a second distinct tenant is served while the override is active
(dogfood single-tenant only). Committed unit tests cover the factory injection +
`_local_service` override + the guard; the Task 8 live e2e stays creds-gated.
This override is explicitly superseded and removed by Task 11 (catalog per-tenant
target model). Replacement/reconcile filesystem ops stay local-only until P3.

## Amendment 2 — 2026-07-01 (Task 8 findings feed P3)

Task 8's live round-trip surfaced libSQL semantics that break several
sqlite3-typed error handlers: libSQL raises a bare `ValueError` (not
`sqlite3.IntegrityError`/`OperationalError`) for constraint/operational errors,
and `with conn:` provides no implicit transaction (needs an explicit `BEGIN`).
Task 8 fixed the ingest path; a repo-wide gap remains (background task
`task_c531447d`). Affected libSQL-facing sites include
`hosted_control_plane_http.py:343/346` (control-plane persistence — the P3
target), `storage/transcript.py:629` (`search_messages`), `storage/operators.py`,
`storage/longterm.py`, `storage/candidates.py`.

**Task 9b — libSQL storage-exception normalization (after T9, before T10).**
Add `src/vexic/storage/errors.py` with `is_unique_violation(exc) -> bool` and
`is_operational_error(exc) -> bool` that classify BOTH sqlite3 typed exceptions
AND libSQL bare `ValueError` (string-sniff `SQLITE_CONSTRAINT` / "UNIQUE
constraint failed" / operational markers). Refactor `transcript.py`'s ingest
dedup to use `is_unique_violation` (no behavior change; suite stays green). Fix
the `StorageConnection` docstring in `connection.py` (~lines 36-38) that
overstates `with conn:` transaction parity. Tests classify both backends via
directly-constructed exceptions.

**Task 10 update:** the control-plane's `IntegrityError`/`OperationalError`
catches MUST adopt these helpers so control-plane persistence works on libSQL.
