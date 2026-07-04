# Console Operational Depth Implementation Plan (Build-Out Plan 1 of 3+)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give console users key lifecycle detail (last-used, revoked history, stale badges), per-day and per-key usage analytics, and a dream-job visibility tab — slices 1–3 of the console build-out spec (`docs/superpowers/specs/2026-07-03-console-buildout-design.md`).

**Architecture:** Each slice is a vertical: hosted-adapter store/schema change → `/control/v1/*` endpoint → console client/store/API layer → console UI, with tests at every layer. The hosted adapter (`src/vexic/hosted_local.py`, `hosted.py`, `hosted_control_plane_http.py`) owns storage and HTTP; the console (`console/`) is a control-plane client with an in-memory stub store for local dev. No `MemoryService` contract changes in this plan.

**Tech Stack:** Python 3.13 + FastAPI + sqlite3/libSQL (hosted adapter, tested with `uv run pytest`); Next.js 16 App Router + React 19 + Tailwind 4 (console, tested with `node --test`, no new npm dependencies).

**Scope note:** This is Plan 1 of the build-out. Billing scaffold + event history are Plan 2; retention, async job substrate, export, and deletes follow their design checks. Do not implement anything from those slices here.

## Global Constraints

- Console never renders memory content — control-plane metadata only (ADR 0012).
- No raw API keys, key hashes, control-plane credentials, request payloads, transcript text, or provider secrets in any response, error, or log (ADR 0013).
- Customer-facing job data must NOT include failure reasons (`error_type`) — status only. Failure reasons are internal Support View material (deferred to Plan 2 support work).
- Control-plane schema changes use the existing idempotent pattern: `PRAGMA table_info(...)` check + `ALTER TABLE ... ADD COLUMN` inside `_init_control_plane_schema` (see `hosted_local.py:877-893`). Never rewrite the base `CREATE TABLE` statements — existing databases must upgrade in place.
- `HostedUsageEvent(*row)` and similar positional constructions mean: new dataclass fields go LAST with a default, and new SELECT columns go LAST, in matching order.
- Python tests run from repo root: `uv run pytest tests/<file>.py -k <name>`. Console tests run from `console/`: `node --test tests/<file>.test.mjs`.
- Commit after every green test cycle. Conventional Commits format.

## File Structure

**Python (hosted adapter):**
- Modify: `src/vexic/hosted.py` — `HostedUsageEvent` + `HostedJobEvent` dataclasses, `_record_request` ok-path, `record_job_usage`, `HostedBackgroundJobRunner._record_job`
- Modify: `src/vexic/hosted_local.py` — schema statements + `_init_control_plane_schema` (both catalog and key-store variants), `authenticate`, `_load_key`, `_HostedApiKey`, `HostedApiKeyRecord`, `list_control_plane_keys`, `record_usage_event`, `record_job_event`, `usage_events`, `job_events`, new `usage_daily`/`usage_by_key` queries
- Modify: `src/vexic/hosted_control_plane_http.py` — `_key_payload`, key-list `include` param, usage `granularity` param, new `/usage/by-key` and `/jobs` routes
- Create: `tests/test_console_ops_depth.py` — all new Python tests for this plan (own harness, does not disturb `test_hosted_http.py`)

**Console:**
- Modify: `console/lib/control-plane-client.mjs` — `listAgentKeys` option, `usageDaily`, `usageByKey`, `listJobs`
- Modify: `console/lib/control-plane-store.mjs` — stub + fail-closed + dispatch for the same four surfaces
- Modify: `console/lib/control-plane-api.mjs` — `listAgentKeysResponse` query param, new `usageDailyResponse`, `usageByKeyResponse`, `listJobsResponse`
- Modify: `console/lib/console-ui-state.mjs` — `keyFreshness`, `capStatus`, `dailyUsageRows`, `jobRuns` helpers
- Create: `console/app/api/control-plane/projects/[projectId]/usage/daily/route.ts`
- Create: `console/app/api/control-plane/projects/[projectId]/usage/by-key/route.ts`
- Create: `console/app/api/control-plane/projects/[projectId]/jobs/route.ts`
- Create: `console/components/tremor/daily-bars.tsx` — dependency-free SVG daily bar chart
- Create: `console/app/console/projects/[projectId]/jobs-tab.tsx`
- Modify: `console/app/console/projects/[projectId]/project-workspace.tsx` — keys table columns, revoked section, usage tab additions, Jobs tab wiring
- Create: `console/tests/console-ops.test.mjs` — API-layer tests for new responses
- Modify: `console/tests/console-ui-state.test.mjs` — tests for new helpers

---

### Task 1: `last_used_at` on API keys (store layer)

**Files:**
- Modify: `src/vexic/hosted_local.py`
- Create: `tests/test_console_ops_depth.py`

**Interfaces:**
- Consumes: existing `HostedApiKeyStore.authenticate(raw_key)`, `_HostedApiKey`, `_now()`.
- Produces: `_HostedApiKey.last_used_at: str | None` (new last field); `hosted_api_keys.last_used_at` column; `authenticate` records last-use with a ≥60-second database-side guard. Task 2 reads this via `list_control_plane_keys`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_console_ops_depth.py`:

```python
from __future__ import annotations

import contextlib
import sqlite3
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from vexic.hosted import (
    HostedInMemoryRateLimiter,
    HostedJobEvent,
    HostedMemoryService,
    HostedUsageEvent,
)
from vexic.hosted_control_plane_http import create_app as create_control_plane_app
from vexic.hosted_local import HostedApiKeyStore, HostedTenantCatalog


class ConsoleOpsDepthHarness(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        root = Path(self.temp_dir.name)
        self.catalog = HostedTenantCatalog(root)
        self.keys = HostedApiKeyStore(root)
        self.service = HostedMemoryService(
            self.catalog,
            self.keys,
            telemetry=self.catalog,
            rate_limiter=HostedInMemoryRateLimiter(),
        )
        self.client = TestClient(
            create_control_plane_app(
                self.service,
                control_plane_tokens=("console-secret",),
            )
        )

    def _control_auth(self) -> dict[str, str]:
        return {"Authorization": "Bearer console-secret"}

    def _create_project(self, org: str = "org_123", name: str = "Alpha") -> dict:
        response = self.client.post(
            f"/control/v1/clerk-orgs/{org}/projects",
            headers=self._control_auth(),
            json={"name": name},
        )
        assert response.status_code == 201, response.text
        return response.json()["project"]

    def _create_key(self, org: str, project_id: str, name: str = "key-a") -> dict:
        response = self.client.post(
            f"/control/v1/clerk-orgs/{org}/projects/{project_id}/keys",
            headers=self._control_auth(),
            json={"name": name},
        )
        assert response.status_code == 201, response.text
        return response.json()

    def _control_db(self) -> sqlite3.Connection:
        return sqlite3.connect(Path(self.temp_dir.name) / "control-plane.db")


class LastUsedAtTests(ConsoleOpsDepthHarness):
    def test_authenticate_records_last_used_at(self) -> None:
        project = self._create_project()
        created = self._create_key("org_123", project["id"])

        self.keys.authenticate(created["rawKey"])

        with contextlib.closing(self._control_db()) as conn:
            row = conn.execute(
                "SELECT last_used_at FROM hosted_api_keys WHERE key_id = ?",
                (created["key"]["id"],),
            ).fetchone()
        self.assertIsNotNone(row[0])

    def test_last_used_at_write_is_throttled_to_one_minute(self) -> None:
        project = self._create_project()
        created = self._create_key("org_123", project["id"])

        self.keys.authenticate(created["rawKey"])
        with contextlib.closing(self._control_db()) as conn:
            first = conn.execute(
                "SELECT last_used_at FROM hosted_api_keys WHERE key_id = ?",
                (created["key"]["id"],),
            ).fetchone()[0]

        self.keys.authenticate(created["rawKey"])
        with contextlib.closing(self._control_db()) as conn:
            second = conn.execute(
                "SELECT last_used_at FROM hosted_api_keys WHERE key_id = ?",
                (created["key"]["id"],),
            ).fetchone()[0]

        self.assertEqual(first, second)

    def test_failed_authentication_does_not_record_last_used_at(self) -> None:
        project = self._create_project()
        created = self._create_key("org_123", project["id"])

        with self.assertRaises(PermissionError):
            self.keys.authenticate(f"vx_{created['key']['id']}_wrong-secret")

        with contextlib.closing(self._control_db()) as conn:
            row = conn.execute(
                "SELECT last_used_at FROM hosted_api_keys WHERE key_id = ?",
                (created["key"]["id"],),
            ).fetchone()
        self.assertIsNone(row[0])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_console_ops_depth.py -v`
Expected: FAIL — `sqlite3.OperationalError: no such column: last_used_at` (or `assertIsNotNone` failure).

- [ ] **Step 3: Implement**

In `src/vexic/hosted_local.py`:

1. Add the field to `_HostedApiKey` (after `active: bool = True`):

```python
    last_used_at: str | None = None
```

2. In `HostedApiKeyStore._init_control_plane_schema` (the one near line 1270 that creates `hosted_api_keys`), append after the two `CREATE TABLE` executes, before `conn.commit()`:

```python
            columns = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(hosted_api_keys)").fetchall()
            }
            if "last_used_at" not in columns:
                conn.execute("ALTER TABLE hosted_api_keys ADD COLUMN last_used_at TEXT")
```

3. In `_load_key`, add `last_used_at` as the LAST select column and constructor kwarg:

```python
                SELECT
                    key_id, key_hash, tenant_id, principal_id, capabilities,
                    project_ids, agent_ids, created_at, revoked_at, revoked_by,
                    last_used_at
```

and in the `_HostedApiKey(...)` construction add `last_used_at=row[10],` after `active=revoked_at is None,`.

4. In `authenticate`, record last-use on success. Replace the success branch:

```python
        if hmac.compare_digest(stored.key_hash, key_hash) and stored.active:
            self._touch_last_used(stored)
            return HostedAuthContext(
```

5. Add the touch method to `HostedApiKeyStore` (place next to `authenticate`). The guard lives in the database (`WHERE` clause), so it stays correct across restarts and across multiple adapter processes; a touch failure must never fail authentication:

```python
    _LAST_USED_MIN_INTERVAL_DAYS = 60.0 / 86400.0  # one minute, in julianday units

    def _touch_last_used(self, stored: _HostedApiKey) -> None:
        now = _now()
        if self._control_target is None:
            if _last_used_is_fresh(stored.last_used_at, now):
                return
            self._keys[stored.key_id] = replace(stored, last_used_at=now)
            return
        try:
            with closing(self._connect_control()) as conn:
                conn.execute(
                    """
                    UPDATE hosted_api_keys
                    SET last_used_at = ?
                    WHERE key_id = ?
                      AND (
                        last_used_at IS NULL
                        OR julianday(?) - julianday(last_used_at) >= ?
                      )
                    """,
                    (now, stored.key_id, now, self._LAST_USED_MIN_INTERVAL_DAYS),
                )
                conn.commit()
        except Exception:
            # Last-used telemetry must never break the auth hot path.
            pass
```

6. Add the module-level helper next to `_now()` at the bottom of the file:

```python
def _last_used_is_fresh(previous: str | None, now: str) -> bool:
    if previous is None:
        return False
    parse = lambda value: datetime.fromisoformat(value.replace("Z", "+00:00"))
    try:
        return (parse(now) - parse(previous)).total_seconds() < 60
    except ValueError:
        return False
```

(`datetime` and `UTC` are already imported in this module; `replace` is already imported from `dataclasses`.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_console_ops_depth.py -v`
Expected: 3 PASS.

- [ ] **Step 5: Run the full Python suite** (schema change touches every hosted test)

Run: `uv run pytest`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/vexic/hosted_local.py tests/test_console_ops_depth.py
git commit -m "feat(hosted): record api-key last_used_at with db-side one-minute throttle"
```

---

### Task 2: Expose `lastUsedAt` and revoked keys on the key-list endpoint

**Files:**
- Modify: `src/vexic/hosted_local.py` (`HostedApiKeyRecord`, `list_control_plane_keys`)
- Modify: `src/vexic/hosted_control_plane_http.py` (`_key_payload`, list handler)
- Test: `tests/test_console_ops_depth.py`

**Interfaces:**
- Consumes: Task 1's `hosted_api_keys.last_used_at` column.
- Produces: `HostedApiKeyRecord.last_used_at: str | None` (new last field); `list_control_plane_keys(*, tenant_id, project_id, include_revoked: bool = False)`; HTTP `GET .../keys?include=revoked`; key payload gains `"lastUsedAt"`. Task 3's console client relies on both.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_console_ops_depth.py`:

```python
class KeyListLifecycleTests(ConsoleOpsDepthHarness):
    def test_key_list_includes_last_used_at(self) -> None:
        project = self._create_project()
        created = self._create_key("org_123", project["id"])
        self.keys.authenticate(created["rawKey"])

        response = self.client.get(
            f"/control/v1/clerk-orgs/org_123/projects/{project['id']}/keys",
            headers=self._control_auth(),
        )

        self.assertEqual(response.status_code, 200)
        key = response.json()["keys"][0]
        self.assertIn("lastUsedAt", key)
        self.assertIsNotNone(key["lastUsedAt"])

    def test_key_list_excludes_revoked_by_default_and_includes_on_request(self) -> None:
        project = self._create_project()
        created = self._create_key("org_123", project["id"])
        key_id = created["key"]["id"]
        revoke = self.client.post(
            f"/control/v1/clerk-orgs/org_123/projects/{project['id']}/keys/{key_id}/revoke",
            headers=self._control_auth(),
        )
        self.assertEqual(revoke.status_code, 204)

        default = self.client.get(
            f"/control/v1/clerk-orgs/org_123/projects/{project['id']}/keys",
            headers=self._control_auth(),
        )
        self.assertEqual(default.json()["keys"], [])

        included = self.client.get(
            f"/control/v1/clerk-orgs/org_123/projects/{project['id']}/keys?include=revoked",
            headers=self._control_auth(),
        )
        keys = included.json()["keys"]
        self.assertEqual(len(keys), 1)
        self.assertEqual(keys[0]["id"], key_id)
        self.assertIsNotNone(keys[0]["revokedAt"])
        for forbidden in ("keyHash", "key_hash", "rawKey"):
            self.assertNotIn(forbidden, keys[0])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_console_ops_depth.py -k KeyListLifecycle -v`
Expected: FAIL — `lastUsedAt` missing; revoked list empty.

- [ ] **Step 3: Implement**

In `src/vexic/hosted_local.py`:

1. Add to `HostedApiKeyRecord` (after `revoked_at: str | None = None`):

```python
    last_used_at: str | None = None
```

2. Replace `list_control_plane_keys` with:

```python
    def list_control_plane_keys(
        self,
        *,
        tenant_id: str,
        project_id: str,
        include_revoked: bool = False,
    ) -> list[HostedApiKeyRecord]:
        if self._control_target is None:
            records = []
            for record in self._control_metadata.values():
                if record.tenant_id != tenant_id or record.project_id != project_id:
                    continue
                stored = self._keys[record.key_id]
                if stored.revoked_at is not None and not include_revoked:
                    continue
                records.append(
                    replace(
                        record,
                        revoked_at=stored.revoked_at,
                        last_used_at=stored.last_used_at,
                    )
                )
            return records
        revoked_filter = "" if include_revoked else "AND keys.revoked_at IS NULL"
        with closing(self._connect_control()) as conn:
            rows = conn.execute(
                f"""
                SELECT
                    meta.key_id, meta.tenant_id, meta.project_id, meta.name,
                    meta.capability, meta.agent_scope, meta.key_prefix,
                    meta.last4, meta.display, meta.created_at, keys.revoked_at,
                    keys.last_used_at
                FROM hosted_api_key_metadata AS meta
                JOIN hosted_api_keys AS keys ON keys.key_id = meta.key_id
                WHERE meta.tenant_id = ? AND meta.project_id = ? {revoked_filter}
                ORDER BY meta.created_at, meta.key_id
                """,
                (tenant_id, project_id),
            ).fetchall()
        return [
            HostedApiKeyRecord(
                key_id=row[0],
                tenant_id=row[1],
                project_id=row[2],
                name=row[3],
                capability=row[4],
                agent_scope=row[5],
                prefix=row[6],
                last4=row[7],
                display=row[8],
                created_at=row[9],
                revoked_at=row[10],
                last_used_at=row[11],
            )
            for row in rows
        ]
```

(`revoked_filter` is built from a two-value literal, never user input — no injection surface.)

In `src/vexic/hosted_control_plane_http.py`:

3. In the `list_control_plane_keys` handler, replace the `keys = ...` call with:

```python
        keys = service.api_keys.list_control_plane_keys(
            tenant_id=tenant_id,
            project_id=project_id,
            include_revoked=request.query_params.get("include") == "revoked",
        )
```

4. In `_key_payload`, add after `"revokedAt": key.revoked_at,`:

```python
        "lastUsedAt": key.last_used_at,
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_console_ops_depth.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/vexic/hosted_local.py src/vexic/hosted_control_plane_http.py tests/test_console_ops_depth.py
git commit -m "feat(control-plane): expose lastUsedAt and ?include=revoked on key list"
```

---

### Task 3: Console keys tab — last-used column, stale badge, revoked section

**Files:**
- Modify: `console/lib/control-plane-client.mjs`
- Modify: `console/lib/control-plane-store.mjs`
- Modify: `console/lib/control-plane-api.mjs`
- Modify: `console/lib/console-ui-state.mjs`
- Modify: `console/app/api/control-plane/projects/[projectId]/keys/route.ts`
- Modify: `console/app/console/projects/[projectId]/project-workspace.tsx`
- Test: `console/tests/console-ui-state.test.mjs`, `console/tests/console-ops.test.mjs`

**Interfaces:**
- Consumes: Task 2's `lastUsedAt` payload field and `?include=revoked`.
- Produces: `keyFreshness(lastUsedAt, nowIso)` in `console-ui-state.mjs` returning `{ label: string, stale: boolean }`; `listAgentKeys(orgId, projectId, { includeRevoked })` client signature; API layer forwards `?include=revoked`.

- [ ] **Step 1: Write the failing UI-state tests**

Append to `console/tests/console-ui-state.test.mjs` (match the file's existing `import test from "node:test"` / `assert` style):

```js
import { keyFreshness } from "../lib/console-ui-state.mjs";

test("keyFreshness labels never-used keys", () => {
  assert.deepEqual(keyFreshness(null, "2026-07-03T00:00:00Z"), {
    label: "Never used",
    stale: false
  });
});

test("keyFreshness flags keys unused for more than 30 days as stale", () => {
  const fresh = keyFreshness("2026-06-20T00:00:00Z", "2026-07-03T00:00:00Z");
  assert.equal(fresh.stale, false);

  const stale = keyFreshness("2026-05-01T00:00:00Z", "2026-07-03T00:00:00Z");
  assert.equal(stale.stale, true);
  assert.match(stale.label, /May 1|2026/);
});
```

(If the existing test file's imports are aggregated at the top, merge `keyFreshness` into the existing import statement instead of adding a second one.)

- [ ] **Step 2: Write the failing API-layer test**

Create `console/tests/console-ops.test.mjs`:

```js
import assert from "node:assert/strict";
import test from "node:test";

import {
  createAgentKeyResponse,
  createProjectResponse,
  listAgentKeysResponse,
  revokeAgentKeyResponse
} from "../lib/control-plane-api.mjs";
import { resetStoreForTests } from "../lib/control-plane-store.mjs";

const authedOrg = { userId: "user_123", orgId: "org_123", isInternalSupport: false };

test.afterEach(() => {
  resetStoreForTests();
});

function request(method, url, body) {
  return new Request(`https://console.test${url}`, {
    method,
    body: body ? JSON.stringify(body) : undefined,
    headers: body ? { "content-type": "application/json" } : undefined
  });
}

async function json(response) {
  return response.json();
}

async function projectWithRevokedKey() {
  const { project } = await json(
    await createProjectResponse(request("POST", "/api/control-plane/projects", { name: "Alpha" }), authedOrg)
  );
  const created = await json(
    await createAgentKeyResponse(
      request("POST", `/api/control-plane/projects/${project.id}/keys`, { name: "old key" }),
      authedOrg,
      project.id
    )
  );
  await revokeAgentKeyResponse(
    request("POST", `/api/control-plane/projects/${project.id}/keys/${created.key.id}/revoke`),
    authedOrg,
    project.id,
    created.key.id
  );
  return { project, key: created.key };
}

test("key list excludes revoked keys by default and includes them with ?include=revoked", async () => {
  const { project, key } = await projectWithRevokedKey();

  const defaultList = await json(
    await listAgentKeysResponse(
      request("GET", `/api/control-plane/projects/${project.id}/keys`),
      authedOrg,
      project.id
    )
  );
  assert.equal(defaultList.keys.length, 0);

  const withRevoked = await json(
    await listAgentKeysResponse(
      request("GET", `/api/control-plane/projects/${project.id}/keys?include=revoked`),
      authedOrg,
      project.id
    )
  );
  assert.equal(withRevoked.keys.length, 1);
  assert.equal(withRevoked.keys[0].id, key.id);
  assert.ok(withRevoked.keys[0].revokedAt);
});
```

- [ ] **Step 3: Run console tests to verify they fail**

Run (from `console/`): `npm test`
Expected: FAIL — `keyFreshness` not exported; revoked-key test fails (stub returns `null`-filtered list without the option).

- [ ] **Step 4: Implement the lib layers**

1. `console/lib/console-ui-state.mjs` — add:

```js
const STALE_KEY_DAYS = 30;

export function keyFreshness(lastUsedAt, nowIso = new Date().toISOString()) {
  if (!lastUsedAt) {
    return { label: "Never used", stale: false };
  }
  const last = new Date(lastUsedAt);
  const ageDays = (new Date(nowIso).getTime() - last.getTime()) / 86_400_000;
  return {
    label: last.toLocaleDateString(undefined, { dateStyle: "medium" }),
    stale: ageDays > STALE_KEY_DAYS
  };
}
```

2. `console/lib/control-plane-client.mjs` — replace `listAgentKeys`:

```js
export async function listAgentKeys(orgId, projectId, { includeRevoked = false } = {}) {
  const suffix = includeRevoked ? "?include=revoked" : "";
  const data = await request(orgId, "GET", `/projects/${encodeURIComponent(projectId)}/keys${suffix}`);
  return data.keys;
}
```

3. `console/lib/control-plane-store.mjs`:
   - Dispatch: `export function listAgentKeys(orgId, projectId, options = {}) { return selectedStore().listAgentKeys(orgId, projectId, options); }`
   - Stub: replace `stubListAgentKeys` with:

```js
function stubListAgentKeys(orgId, projectId, { includeRevoked = false } = {}) {
  if (!stubGetProject(orgId, projectId)) {
    return null;
  }
  const keys = keysByProject.get(projectId) ?? [];
  return keys.filter((key) => includeRevoked || !key.revokedAt).map(publicKey);
}
```

   - In `publicKey(key)`, add `lastUsedAt: key.lastUsedAt ?? null,` after `revokedAt`.
   - In `stubCreateAgentKey`'s key object, add `lastUsedAt: null,` after `revokedAt: null`.

4. `console/lib/control-plane-api.mjs` — replace `listAgentKeysResponse`:

```js
export async function listAgentKeysResponse(request, auth, projectId) {
  const denied = requireOrg(auth);
  if (denied) return denied;
  const includeRevoked = new URL(request.url).searchParams.get("include") === "revoked";
  return storeResponse(
    () => listAgentKeys(auth.orgId, projectId, { includeRevoked }),
    (keys) => (keys ? json({ keys }) : json({ error: "not_found" }, 404))
  );
}
```

(`console/app/api/control-plane/projects/[projectId]/keys/route.ts` already passes `request` through — verify it does; if its GET handler drops the request argument, thread it.)

- [ ] **Step 5: Run console tests to verify they pass**

Run (from `console/`): `npm test`
Expected: all PASS.

- [ ] **Step 6: Wire the UI**

In `console/app/console/projects/[projectId]/project-workspace.tsx`:

1. `loadKeys` fetch URL becomes `/api/control-plane/projects/${projectId}/keys?include=revoked`.
2. Derive the two lists where keys are rendered:

```tsx
const activeKeys = keys.filter((key) => !key.revokedAt);
const revokedKeys = keys.filter((key) => key.revokedAt);
```

Render `activeKeys` in the existing table. Add a "Last used" `TableHead`/`TableCell` column to that table:

```tsx
<TableCell>
  {(() => {
    const freshness = keyFreshness(key.lastUsedAt);
    return (
      <span className="inline-flex items-center gap-2">
        {freshness.label}
        {freshness.stale ? <Badge variant="outline">Unused 30+ days</Badge> : null}
      </span>
    );
  })()}
</TableCell>
```

3. Below the active table, render a collapsed revoked section (native `details` keeps it dependency-free):

```tsx
{revokedKeys.length > 0 ? (
  <details className="mt-4">
    <summary className="cursor-pointer text-sm text-muted-foreground">
      Revoked keys ({revokedKeys.length})
    </summary>
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Name</TableHead>
          <TableHead>Key</TableHead>
          <TableHead>Revoked</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {revokedKeys.map((key) => (
          <TableRow key={key.id}>
            <TableCell>{key.name}</TableCell>
            <TableCell className="font-mono text-xs">{key.display}</TableCell>
            <TableCell>{dateFormatter.format(new Date(key.revokedAt!))}</TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  </details>
) : null}
```

4. Add `lastUsedAt: string | null; revokedAt: string | null;` to the `AgentKey` type, and import `keyFreshness` from `@/lib/console-ui-state.mjs`.

- [ ] **Step 7: Build and verify**

Run (from `console/`): `npm test && npm run build`
Expected: tests pass, build succeeds.

- [ ] **Step 8: Commit**

```bash
git add console/
git commit -m "feat(console): key last-used column, stale badge, revoked-key history"
```

---

### Task 4: `key_id` on usage events (backend attribution)

**Files:**
- Modify: `src/vexic/hosted.py` (`HostedUsageEvent`, `_record_request`, `_call` ok-path, `record_job_usage`, `HostedBackgroundJobRunner.run_dream_phase`)
- Modify: `src/vexic/hosted_local.py` (schema, `record_usage_event`, `usage_events`)
- Test: `tests/test_console_ops_depth.py`

**Interfaces:**
- Consumes: `HostedAuthContext.key_id` (already on every authenticated call).
- Produces: `HostedUsageEvent.key_id: str | None = None` (new LAST field); `hosted_usage_events.key_id` column (LAST select column). Task 5 aggregates on it. Events recorded before this task have `NULL` key_id and surface as "unattributed".

- [ ] **Step 1: Write the failing test**

Append to `tests/test_console_ops_depth.py`:

```python
class UsageKeyAttributionTests(ConsoleOpsDepthHarness):
    def test_usage_events_carry_key_id(self) -> None:
        self.catalog.record_usage_event(
            HostedUsageEvent(
                kind="request",
                operation="append_transcript",
                tenant_id="tenant-a",
                principal_id="shared",
                status="ok",
                recorded_at="2026-07-01T00:00:00Z",
                project_id="proj_a",
                key_id="key_abc",
            )
        )

        events = self.catalog.usage_events("tenant-a")

        self.assertEqual(events[0].key_id, "key_abc")

    def test_usage_events_without_key_id_load_as_none(self) -> None:
        self.catalog.record_usage_event(
            HostedUsageEvent(
                kind="request",
                operation="append_transcript",
                tenant_id="tenant-a",
                principal_id="shared",
                status="ok",
                recorded_at="2026-07-01T00:00:00Z",
            )
        )

        events = self.catalog.usage_events("tenant-a")

        self.assertIsNone(events[0].key_id)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_console_ops_depth.py -k UsageKeyAttribution -v`
Expected: FAIL — `HostedUsageEvent` has no `key_id` keyword.

- [ ] **Step 3: Implement**

In `src/vexic/hosted.py`:

1. `HostedUsageEvent`: add as the LAST field (after `project_id: str | None = None`):

```python
    key_id: str | None = None
```

2. `_record_request`: thread the key. The signature already takes `auth`; derive `key_id = auth.key_id if auth is not None else None` and pass `key_id=key_id` in the `HostedUsageEvent(...)` construction.
3. The success path in `_call` currently drops auth — change line 699 to:

```python
        self._record_request(operation, bound, status="ok", auth=auth)
```

(`_record_request` prefers `request.scope` for tenant/principal when `request` is not None, and `auth` is now also supplied for `key_id` — no behavior change for the existing fields.)

4. `record_job_usage`: add parameter `key_id: str | None = None` and pass `key_id=key_id` in its `HostedUsageEvent(...)`.
5. `HostedBackgroundJobRunner.run_dream_phase`: both `record_job_usage(...)` calls gain `key_id=auth.key_id,`.

In `src/vexic/hosted_local.py`:

6. `_CONTROL_PLANE_SCHEMA_STATEMENTS` untouched. In `HostedTenantCatalog._init_control_plane_schema`, extend the existing `columns` check block (it already checks `project_id`):

```python
            if "key_id" not in columns:
                conn.execute("ALTER TABLE hosted_usage_events ADD COLUMN key_id TEXT")
```

7. `record_usage_event`: add `key_id` to the column list and `event.key_id` as the LAST value (14 placeholders now).
8. `usage_events`: add `key_id` as the LAST select column (constructor is positional `HostedUsageEvent(*row)` — order must match the dataclass field order exactly).

- [ ] **Step 4: Run the full suite** (positional-row change is regression-prone)

Run: `uv run pytest`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add src/vexic/hosted.py src/vexic/hosted_local.py tests/test_console_ops_depth.py
git commit -m "feat(hosted): attribute usage events to api key_id"
```

---

### Task 5: Daily-bucket and by-key usage endpoints

**Files:**
- Modify: `src/vexic/hosted_local.py` (new aggregation queries on `HostedTenantCatalog`)
- Modify: `src/vexic/hosted_control_plane_http.py` (extend project usage handler, add `/usage/by-key`)
- Test: `tests/test_console_ops_depth.py`

**Interfaces:**
- Consumes: Task 4's `key_id` column.
- Produces:
  - `HostedTenantCatalog.usage_daily(tenant_id, *, project_id, recorded_at_gte, recorded_at_lt) -> list[dict]` with rows `{"date": "YYYY-MM-DD", "writes": int, "retrievals": int, "other": int}`.
  - `HostedTenantCatalog.usage_by_key(tenant_id, *, project_id, recorded_at_gte, recorded_at_lt) -> list[dict]` with rows `{"keyId": str | None, "requests": int}`.
  - HTTP: `GET .../projects/{id}/usage?granularity=day&days=30` adds a `"daily"` array to the usage payload; `GET .../projects/{id}/usage/by-key` returns `{"byKey": [...]}`. Task 6 consumes both.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_console_ops_depth.py`:

```python
class UsageAnalyticsEndpointTests(ConsoleOpsDepthHarness):
    def _seed_usage(self, project_id: str) -> None:
        rows = [
            ("append_transcript", "2026-07-01T10:00:00Z", "key_a"),
            ("append_transcript", "2026-07-01T11:00:00Z", "key_a"),
            ("search_long_term", "2026-07-01T12:00:00Z", "key_b"),
            ("search_transcript", "2026-07-02T09:00:00Z", "key_b"),
            ("expand_history", "2026-07-02T10:00:00Z", None),
        ]
        for operation, recorded_at, key_id in rows:
            self.catalog.record_usage_event(
                HostedUsageEvent(
                    kind="request",
                    operation=operation,
                    tenant_id=self.tenant_id,
                    principal_id="shared",
                    status="ok",
                    recorded_at=recorded_at,
                    project_id=project_id,
                    key_id=key_id,
                )
            )

    def _provisioned_project(self) -> dict:
        project = self._create_project()
        tenant = self.client.post(
            "/control/v1/clerk-orgs/org_123/tenant",
            headers=self._control_auth(),
        ).json()["tenant"]
        self.tenant_id = tenant["tenantId"]
        return project

    def test_daily_granularity_returns_bucketed_rows(self) -> None:
        project = self._provisioned_project()
        self._seed_usage(project["id"])

        response = self.client.get(
            f"/control/v1/clerk-orgs/org_123/projects/{project['id']}/usage"
            "?granularity=day&days=30",
            headers=self._control_auth(),
        )

        self.assertEqual(response.status_code, 200)
        daily = response.json()["usage"]["daily"]
        by_date = {row["date"]: row for row in daily}
        self.assertEqual(by_date["2026-07-01"]["writes"], 2)
        self.assertEqual(by_date["2026-07-01"]["retrievals"], 1)
        self.assertEqual(by_date["2026-07-02"]["retrievals"], 1)
        self.assertEqual(by_date["2026-07-02"]["other"], 1)

    def test_usage_without_granularity_has_no_daily_array(self) -> None:
        project = self._provisioned_project()

        response = self.client.get(
            f"/control/v1/clerk-orgs/org_123/projects/{project['id']}/usage",
            headers=self._control_auth(),
        )

        self.assertNotIn("daily", response.json()["usage"])

    def test_by_key_endpoint_aggregates_per_key(self) -> None:
        project = self._provisioned_project()
        self._seed_usage(project["id"])

        response = self.client.get(
            f"/control/v1/clerk-orgs/org_123/projects/{project['id']}/usage/by-key"
            "?days=30",
            headers=self._control_auth(),
        )

        self.assertEqual(response.status_code, 200)
        by_key = {row["keyId"]: row["requests"] for row in response.json()["byKey"]}
        self.assertEqual(by_key["key_a"], 2)
        self.assertEqual(by_key["key_b"], 2)
        self.assertEqual(by_key[None], 1)

    def test_by_key_requires_control_credential(self) -> None:
        project = self._provisioned_project()

        response = self.client.get(
            f"/control/v1/clerk-orgs/org_123/projects/{project['id']}/usage/by-key",
        )

        self.assertEqual(response.status_code, 401)
```

Note: these tests seed events dated 2026-07-01/02. The daily/by-key window is
`days` back from now, so they hold while "now" is within `days` of those dates —
the seeds use a 30-day window and the test suite's clock is real time. To keep
them stable forever, compute seed dates from `datetime.now(UTC)` minus 1–2 days
instead of literals; do that from the start:

```python
    # In _seed_usage, replace the literal dates:
    from datetime import UTC, datetime, timedelta

    day1 = (datetime.now(UTC) - timedelta(days=2)).strftime("%Y-%m-%dT10:00:00Z")
    day2 = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%dT09:00:00Z")
```

and assert on the derived `day1[:10]` / `day2[:10]` date strings rather than literals.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_console_ops_depth.py -k UsageAnalyticsEndpoint -v`
Expected: FAIL — no `daily` key, 404 on `/usage/by-key`.

- [ ] **Step 3: Implement the store queries**

In `src/vexic/hosted_local.py`, add to `HostedTenantCatalog` (next to `usage_events`):

```python
    _WRITE_OPERATIONS = ("append_transcript",)
    _RETRIEVAL_OPERATIONS = ("search_transcript", "search_long_term")

    def usage_daily(
        self,
        tenant_id: str,
        *,
        project_id: str,
        recorded_at_gte: str,
        recorded_at_lt: str,
    ) -> list[dict[str, object]]:
        events = self.usage_events(
            tenant_id,
            project_id=project_id,
            recorded_at_gte=recorded_at_gte,
            recorded_at_lt=recorded_at_lt,
        )
        buckets: dict[str, dict[str, object]] = {}
        for event in events:
            date = event.recorded_at[:10]
            bucket = buckets.setdefault(
                date, {"date": date, "writes": 0, "retrievals": 0, "other": 0}
            )
            if event.operation in self._WRITE_OPERATIONS:
                bucket["writes"] += 1
            elif event.operation in self._RETRIEVAL_OPERATIONS:
                bucket["retrievals"] += 1
            else:
                bucket["other"] += 1
        return [buckets[date] for date in sorted(buckets)]

    def usage_by_key(
        self,
        tenant_id: str,
        *,
        project_id: str,
        recorded_at_gte: str,
        recorded_at_lt: str,
    ) -> list[dict[str, object]]:
        events = self.usage_events(
            tenant_id,
            project_id=project_id,
            recorded_at_gte=recorded_at_gte,
            recorded_at_lt=recorded_at_lt,
        )
        counts: dict[str | None, int] = {}
        for event in events:
            counts[event.key_id] = counts.get(event.key_id, 0) + 1
        return [
            {"keyId": key_id, "requests": count}
            for key_id, count in sorted(
                counts.items(), key=lambda item: (-item[1], item[0] or "")
            )
        ]
```

(Python-side aggregation over the already-indexed `usage_events` query keeps this one code path for both sqlite and libSQL backends; per-project event volume at this stage does not justify SQL `GROUP BY` divergence. Revisit if a project exceeds ~100k events/month.)

- [ ] **Step 4: Implement the HTTP surface**

In `src/vexic/hosted_control_plane_http.py`:

1. Add a helper next to `_usage_period()`:

```python
def _days_window(request: Request, *, default_days: int = 30) -> tuple[str, str]:
    raw = request.query_params.get("days", str(default_days))
    try:
        days = max(1, min(90, int(raw)))
    except ValueError:
        days = default_days
    now = datetime.now(UTC)
    start = now - timedelta(days=days)
    return _utc_iso(start), _utc_iso(now)
```

Add `timedelta` to the existing `from datetime import UTC, datetime` import.

2. In `get_control_plane_project_usage`, after building `payload` via `_usage_payload(...)` (restructure the `return` so the payload is a local variable first):

```python
        usage = _usage_payload(
            events,
            period_start=period_start,
            period_end=period_end,
            project_id=project_id,
        )
        if request.query_params.get("granularity") == "day":
            window_start, window_end = _days_window(request)
            usage["daily"] = service.catalog.usage_daily(
                tenant_id,
                project_id=project_id,
                recorded_at_gte=window_start,
                recorded_at_lt=window_end,
            )
        return JSONResponse({"usage": usage})
```

3. Add the by-key route (same auth/resolve/project-check shape as the project usage handler):

```python
    @app.get("/control/v1/clerk-orgs/{clerk_org_id}/projects/{project_id}/usage/by-key")
    @_control_plane_storage_boundary
    async def get_control_plane_project_usage_by_key(
        clerk_org_id: str,
        project_id: str,
        request: Request,
    ) -> JSONResponse:
        if not _has_control_plane_credential(request, control_plane_tokens):
            return _error_response(401, "unauthorized", "Invalid control-plane credential.")
        tenant_id = _resolve_control_tenant(service, clerk_org_id)
        if tenant_id is None:
            return _error_response(404, "not_found", "Project not found.")
        try:
            service.catalog.get_control_project(tenant_id, project_id)
        except PermissionError:
            return _error_response(404, "not_found", "Project not found.")
        window_start, window_end = _days_window(request)
        return JSONResponse(
            {
                "byKey": service.catalog.usage_by_key(
                    tenant_id,
                    project_id=project_id,
                    recorded_at_gte=window_start,
                    recorded_at_lt=window_end,
                )
            }
        )
```

FastAPI route-matching note: register this route BEFORE the existing
`GET .../projects/{project_id}/usage` route in the file, or verify with the
Task 5 tests that `/usage/by-key` does not get captured by `/usage` — Starlette
matches in registration order and `{project_id}/usage` cannot swallow the
longer literal path, but the test must prove it.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_console_ops_depth.py -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add src/vexic/hosted_local.py src/vexic/hosted_control_plane_http.py tests/test_console_ops_depth.py
git commit -m "feat(control-plane): daily usage buckets and by-key attribution endpoints"
```

---

### Task 6: Console usage tab — daily chart, cap thresholds, by-key table

**Files:**
- Modify: `console/lib/control-plane-client.mjs`
- Modify: `console/lib/control-plane-store.mjs`
- Modify: `console/lib/control-plane-api.mjs`
- Modify: `console/lib/console-ui-state.mjs`
- Create: `console/app/api/control-plane/projects/[projectId]/usage/daily/route.ts`
- Create: `console/app/api/control-plane/projects/[projectId]/usage/by-key/route.ts`
- Create: `console/components/tremor/daily-bars.tsx`
- Modify: `console/app/console/projects/[projectId]/project-workspace.tsx`
- Test: `console/tests/console-ui-state.test.mjs`, `console/tests/console-ops.test.mjs`

**Interfaces:**
- Consumes: Task 5's `usage.daily` array and `/usage/by-key` payloads; existing `usageMeterDisplay`.
- Produces:
  - `capStatus(value, max)` in `console-ui-state.mjs` → `{ level: "ok" | "warn" | "alert" | "none" }` (warn ≥80%, alert ≥95%, none when no cap).
  - Client: `usageDaily(orgId, projectId)` → `[{date, writes, retrievals, other}]`; `usageByKey(orgId, projectId)` → `[{keyId, requests}]`.
  - API: `usageDailyResponse`, `usageByKeyResponse` (same `requireOrg` + `storeResponse` shape as existing).
  - `<DailyBars rows={...} />` component.

- [ ] **Step 1: Write the failing UI-state tests**

Append to `console/tests/console-ui-state.test.mjs`:

```js
import { capStatus } from "../lib/console-ui-state.mjs";

test("capStatus thresholds: ok below 80, warn at 80, alert at 95, none without cap", () => {
  assert.equal(capStatus(50, 100).level, "ok");
  assert.equal(capStatus(80, 100).level, "warn");
  assert.equal(capStatus(95, 100).level, "alert");
  assert.equal(capStatus(120, 100).level, "alert");
  assert.equal(capStatus(50, 0).level, "none");
});
```

(Merge the import into the existing import from `console-ui-state.mjs`.)

- [ ] **Step 2: Write the failing API tests**

Append to `console/tests/console-ops.test.mjs`:

```js
import { usageByKeyResponse, usageDailyResponse } from "../lib/control-plane-api.mjs";

test("usage daily and by-key respond for a stub project", async () => {
  const { project } = await json(
    await createProjectResponse(request("POST", "/api/control-plane/projects", { name: "Alpha" }), authedOrg)
  );

  const daily = await json(
    await usageDailyResponse(
      request("GET", `/api/control-plane/projects/${project.id}/usage/daily`),
      authedOrg,
      project.id
    )
  );
  assert.ok(Array.isArray(daily.daily));

  const byKey = await json(
    await usageByKeyResponse(
      request("GET", `/api/control-plane/projects/${project.id}/usage/by-key`),
      authedOrg,
      project.id
    )
  );
  assert.ok(Array.isArray(byKey.byKey));
});

test("usage daily requires an active org", async () => {
  const denied = await usageDailyResponse(
    request("GET", "/api/control-plane/projects/proj_x/usage/daily"),
    { userId: "user_123", orgId: null, isInternalSupport: false },
    "proj_x"
  );
  assert.equal(denied.status, 403);
});
```

- [ ] **Step 3: Run console tests to verify they fail**

Run (from `console/`): `npm test`
Expected: FAIL — missing exports.

- [ ] **Step 4: Implement the lib layers**

1. `console/lib/console-ui-state.mjs`:

```js
export function capStatus(value, max) {
  if (!max || max <= 0) {
    return { level: "none" };
  }
  const ratio = value / max;
  if (ratio >= 0.95) return { level: "alert" };
  if (ratio >= 0.8) return { level: "warn" };
  return { level: "ok" };
}
```

2. `console/lib/control-plane-client.mjs`:

```js
export async function usageDaily(orgId, projectId) {
  const data = await request(
    orgId,
    "GET",
    `/projects/${encodeURIComponent(projectId)}/usage?granularity=day&days=30`
  );
  return data.usage?.daily ?? [];
}

export async function usageByKey(orgId, projectId) {
  const data = await request(orgId, "GET", `/projects/${encodeURIComponent(projectId)}/usage/by-key?days=30`);
  return data.byKey ?? [];
}
```

3. `console/lib/control-plane-store.mjs` — dispatch functions:

```js
export function usageDaily(orgId, projectId) {
  return selectedStore().usageDaily(orgId, projectId);
}

export function usageByKey(orgId, projectId) {
  return selectedStore().usageByKey(orgId, projectId);
}
```

Stub implementations (deterministic, derived from today's date):

```js
function stubUsageDaily(orgId, projectId) {
  if (!stubGetProject(orgId, projectId)) {
    return null;
  }
  const rows = [];
  for (let back = 13; back >= 0; back -= 1) {
    const day = new Date(Date.now() - back * 86_400_000);
    rows.push({
      date: day.toISOString().slice(0, 10),
      writes: 3 + ((back * 7) % 9),
      retrievals: 20 + ((back * 13) % 31),
      other: (back * 3) % 5
    });
  }
  return rows;
}

function stubUsageByKey(orgId, projectId) {
  const keys = stubListAgentKeys(orgId, projectId, { includeRevoked: false });
  if (keys === null) {
    return null;
  }
  const rows = keys.map((key, index) => ({ keyId: key.id, requests: 240 - index * 60 }));
  rows.push({ keyId: null, requests: 12 });
  return rows;
}
```

Register both in `stubStore` and as `notConfigured` in `failClosedStore`.

4. `console/lib/control-plane-api.mjs`:

```js
export async function usageDailyResponse(_request, auth, projectId) {
  const denied = requireOrg(auth);
  if (denied) return denied;
  return storeResponse(
    () => usageDaily(auth.orgId, projectId),
    (daily) => (daily ? json({ daily }) : json({ error: "not_found" }, 404))
  );
}

export async function usageByKeyResponse(_request, auth, projectId) {
  const denied = requireOrg(auth);
  if (denied) return denied;
  return storeResponse(
    () => usageByKey(auth.orgId, projectId),
    (byKey) => (byKey ? json({ byKey }) : json({ error: "not_found" }, 404))
  );
}
```

(Add `usageDaily, usageByKey` to the store import at the top.)

5. Route files, both following the existing usage route pattern exactly:

`console/app/api/control-plane/projects/[projectId]/usage/daily/route.ts`:

```ts
import { usageDailyResponse } from "@/lib/control-plane-api.mjs";
import { readAuthContext } from "@/lib/auth";

export const dynamic = "force-dynamic";

export async function GET(request: Request, { params }: { params: Promise<{ projectId: string }> }) {
  const { projectId } = await params;
  return usageDailyResponse(request, await readAuthContext(), projectId);
}
```

`console/app/api/control-plane/projects/[projectId]/usage/by-key/route.ts`: same file with `usageByKeyResponse`.

- [ ] **Step 5: Run console tests to verify they pass**

Run (from `console/`): `npm test`
Expected: all PASS.

- [ ] **Step 6: Build the chart component**

Create `console/components/tremor/daily-bars.tsx` (match `bar-list.tsx` conventions — client component, Tailwind classes, no external chart lib):

```tsx
"use client";

type DailyRow = {
  date: string;
  writes: number;
  retrievals: number;
  other: number;
};

const SEGMENTS = [
  { key: "writes" as const, label: "Writes", className: "bg-chart-1" },
  { key: "retrievals" as const, label: "Retrievals", className: "bg-chart-2" },
  { key: "other" as const, label: "Other", className: "bg-chart-3" }
];

export function DailyBars({ rows }: { rows: DailyRow[] }) {
  if (rows.length === 0) {
    return <p className="text-sm text-muted-foreground">No usage recorded in the last 30 days.</p>;
  }
  const maxTotal = Math.max(...rows.map((row) => row.writes + row.retrievals + row.other), 1);
  return (
    <div>
      <div className="flex h-40 items-end gap-1" role="img" aria-label="Daily operations, last 30 days">
        {rows.map((row) => {
          const total = row.writes + row.retrievals + row.other;
          return (
            <div
              key={row.date}
              className="flex flex-1 flex-col justify-end"
              title={`${row.date}: ${row.writes} writes, ${row.retrievals} retrievals, ${row.other} other`}
            >
              {SEGMENTS.map(({ key, className }) =>
                row[key] > 0 ? (
                  <div
                    key={key}
                    className={className}
                    style={{ height: `${(row[key] / maxTotal) * 100}%` }}
                  />
                ) : null
              )}
              <span className="sr-only">{`${row.date}: ${total} operations`}</span>
            </div>
          );
        })}
      </div>
      <div className="mt-2 flex gap-4 text-xs text-muted-foreground">
        {SEGMENTS.map(({ key, label, className }) => (
          <span key={key} className="inline-flex items-center gap-1">
            <span className={`inline-block size-2 rounded-sm ${className}`} />
            {label}
          </span>
        ))}
      </div>
    </div>
  );
}
```

If `bg-chart-1/2/3` tokens do not exist in `globals.css`, use `bg-primary`, `bg-primary/60`, `bg-primary/30` instead — check `globals.css` first and match whatever chart color tokens the theme defines.

- [ ] **Step 7: Wire the usage tab**

In `project-workspace.tsx`:

1. New state + loaders following the exact `loadUsage` pattern:

```tsx
const [daily, setDaily] = useState<{ date: string; writes: number; retrievals: number; other: number }[]>([]);
const [byKey, setByKey] = useState<{ keyId: string | null; requests: number }[]>([]);
```

`loadDaily()` fetches `/api/control-plane/projects/${projectId}/usage/daily`, sets `daily` from `data.daily`; `loadByKey()` fetches `.../usage/by-key`, sets `byKey` from `data.byKey`. Both use the same try/catch + load-state + `toast.error` shape as `loadUsage`, and are called wherever `loadUsage()` is called.

2. In the Usage tab content, render in order: existing meters (now with threshold styling), the chart, the by-key table.

Threshold styling on existing meters — where `usageRows(usage)` rows render `UsageMeter`, wrap with:

```tsx
const status = capStatus(row.value, row.max);
```

and when `status.level === "warn"` render a `<Badge variant="outline">Approaching cap</Badge>` beside the meter label; when `"alert"`, `<Badge variant="destructive">Near cap</Badge>`.

Chart card:

```tsx
<Card>
  <CardHeader>
    <CardTitle>Operations per day</CardTitle>
    <CardDescription>Writes, retrievals, and other operations over the last 30 days.</CardDescription>
  </CardHeader>
  <CardContent>
    <DailyBars rows={daily} />
  </CardContent>
</Card>
```

By-key card — map `keyId` to key display via the already-loaded `keys` list:

```tsx
<Card>
  <CardHeader>
    <CardTitle>Usage by key</CardTitle>
    <CardDescription>Which agent keys are consuming operations (last 30 days).</CardDescription>
  </CardHeader>
  <CardContent>
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Key</TableHead>
          <TableHead className="text-right">Requests</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {byKey.map((row) => {
          const key = keys.find((item) => item.id === row.keyId);
          return (
            <TableRow key={row.keyId ?? "unattributed"}>
              <TableCell className="font-mono text-xs">
                {row.keyId === null ? "Unattributed (recorded before key tracking)" : key?.display ?? row.keyId}
              </TableCell>
              <TableCell className="text-right">{row.requests}</TableCell>
            </TableRow>
          );
        })}
      </TableBody>
    </Table>
  </CardContent>
</Card>
```

Imports: `DailyBars` from `@/components/tremor/daily-bars`, `capStatus` from `@/lib/console-ui-state.mjs`.

- [ ] **Step 8: Build and verify**

Run (from `console/`): `npm test && npm run build`
Expected: pass.

- [ ] **Step 9: Commit**

```bash
git add console/
git commit -m "feat(console): daily usage chart, cap thresholds, by-key attribution table"
```

---

### Task 7: `project_id` on job events (store layer)

**Files:**
- Modify: `src/vexic/hosted.py` (`HostedJobEvent`, `HostedBackgroundJobRunner._record_job`)
- Modify: `src/vexic/hosted_local.py` (schema, `record_job_event`, `job_events`)
- Test: `tests/test_console_ops_depth.py`

**Interfaces:**
- Consumes: `request.scope.project_id` (already in hand inside `_record_job`).
- Produces: `HostedJobEvent.project_id: str | None = None` (new LAST field); `hosted_job_events.project_id` column; `job_events(tenant_id, *, project_id=None, limit=None)` — with `limit`, rows come newest-first. Task 8's endpoint relies on the filtered, limited query. Historical events have `NULL` project_id and never appear in per-project views.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_console_ops_depth.py`:

```python
class JobEventProjectAttributionTests(ConsoleOpsDepthHarness):
    def _record(self, job_id: str, status: str, project_id: str | None, recorded_at: str) -> None:
        self.catalog.record_job_event(
            HostedJobEvent(
                job_id=job_id,
                operation="run_dream_phase",
                tenant_id="tenant-a",
                principal_id="shared",
                status=status,
                recorded_at=recorded_at,
                phase="light",
                project_id=project_id,
            )
        )

    def test_job_events_filter_by_project(self) -> None:
        self._record("job1", "ok", "proj_a", "2026-07-01T00:00:00Z")
        self._record("job2", "ok", "proj_b", "2026-07-01T01:00:00Z")
        self._record("job3", "ok", None, "2026-07-01T02:00:00Z")

        events = self.catalog.job_events("tenant-a", project_id="proj_a")

        self.assertEqual([event.job_id for event in events], ["job1"])

    def test_job_events_limit_returns_newest_first(self) -> None:
        for index in range(5):
            self._record(f"job{index}", "ok", "proj_a", f"2026-07-01T0{index}:00:00Z")

        events = self.catalog.job_events("tenant-a", project_id="proj_a", limit=2)

        self.assertEqual([event.job_id for event in events], ["job4", "job3"])

    def test_job_events_default_behavior_unchanged(self) -> None:
        self._record("job1", "ok", "proj_a", "2026-07-01T00:00:00Z")
        self._record("job2", "ok", "proj_b", "2026-07-01T01:00:00Z")

        events = self.catalog.job_events("tenant-a")

        self.assertEqual([event.job_id for event in events], ["job1", "job2"])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_console_ops_depth.py -k JobEventProject -v`
Expected: FAIL — `HostedJobEvent` has no `project_id` keyword.

- [ ] **Step 3: Implement**

In `src/vexic/hosted.py`:

1. `HostedJobEvent`: add LAST field `project_id: str | None = None`.
2. `HostedBackgroundJobRunner._record_job`: add `project_id=request.scope.project_id,` to the `HostedJobEvent(...)` construction.

In `src/vexic/hosted_local.py`:

3. In `HostedTenantCatalog._init_control_plane_schema`, add a job-events column check alongside the usage-events one:

```python
            job_columns = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(hosted_job_events)").fetchall()
            }
            if "project_id" not in job_columns:
                conn.execute("ALTER TABLE hosted_job_events ADD COLUMN project_id TEXT")
```

4. `record_job_event`: add `project_id` column + `event.project_id` value (LAST).
5. Replace `job_events` with:

```python
    def job_events(
        self,
        tenant_id: str,
        *,
        project_id: str | None = None,
        limit: int | None = None,
    ) -> list[HostedJobEvent]:
        conditions = ["tenant_id = ?"]
        params: list[object] = [tenant_id]
        if project_id is not None:
            conditions.append("project_id = ?")
            params.append(project_id)
        order = "ORDER BY id"
        if limit is not None:
            order = "ORDER BY id DESC LIMIT ?"
            params.append(limit)
        with closing(self._connect_control()) as conn:
            rows = conn.execute(
                f"""
                SELECT
                    job_id, operation, tenant_id, principal_id, status,
                    recorded_at, phase, error_type, project_id
                FROM hosted_job_events
                WHERE {" AND ".join(conditions)}
                {order}
                """,
                tuple(params),
            ).fetchall()
        return [
            HostedJobEvent(
                job_id=row[0],
                operation=row[1],
                tenant_id=row[2],
                principal_id=row[3],
                status=row[4],
                recorded_at=row[5],
                phase=row[6],
                error_type=row[7],
                project_id=row[8],
            )
            for row in rows
        ]
```

- [ ] **Step 4: Run the full suite**

Run: `uv run pytest`
Expected: all pass (existing `job_events(tenant_id)` callers keep identical behavior).

- [ ] **Step 5: Commit**

```bash
git add src/vexic/hosted.py src/vexic/hosted_local.py tests/test_console_ops_depth.py
git commit -m "feat(hosted): attribute job events to project_id with filtered query"
```

---

### Task 8: Customer-facing jobs endpoint

**Files:**
- Modify: `src/vexic/hosted_control_plane_http.py`
- Test: `tests/test_console_ops_depth.py`

**Interfaces:**
- Consumes: Task 7's `job_events(tenant_id, project_id=..., limit=...)`.
- Produces: `GET /control/v1/clerk-orgs/{org}/projects/{id}/jobs?limit=50` → `{"jobs": [{"jobId", "operation", "phase", "status", "recordedAt"}]}`, newest-first. **No `error_type` / failure reason in the payload — customer surface is status-only** (spec: reasons are internal Support View material, deferred to Plan 2). Task 9 consumes this payload.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_console_ops_depth.py`:

```python
class JobsEndpointTests(ConsoleOpsDepthHarness):
    def _seed_jobs(self, project_id: str) -> None:
        for job_id, status, error_type in (
            ("job1", "running", None),
            ("job1", "ok", None),
            ("job2", "running", None),
            ("job2", "error", "HostPortNotConfigured"),
        ):
            self.catalog.record_job_event(
                HostedJobEvent(
                    job_id=job_id,
                    operation="run_dream_phase",
                    tenant_id=self.tenant_id,
                    principal_id="shared",
                    status=status,
                    recorded_at="2026-07-01T00:00:00Z",
                    phase="light",
                    error_type=error_type,
                    project_id=project_id,
                )
            )

    def _provisioned_project(self) -> dict:
        project = self._create_project()
        tenant = self.client.post(
            "/control/v1/clerk-orgs/org_123/tenant",
            headers=self._control_auth(),
        ).json()["tenant"]
        self.tenant_id = tenant["tenantId"]
        return project

    def test_jobs_endpoint_returns_project_events_without_error_detail(self) -> None:
        project = self._provisioned_project()
        self._seed_jobs(project["id"])

        response = self.client.get(
            f"/control/v1/clerk-orgs/org_123/projects/{project['id']}/jobs?limit=50",
            headers=self._control_auth(),
        )

        self.assertEqual(response.status_code, 200)
        jobs = response.json()["jobs"]
        self.assertEqual(len(jobs), 4)
        statuses = {job["status"] for job in jobs}
        self.assertIn("error", statuses)
        for job in jobs:
            self.assertNotIn("errorType", job)
            self.assertNotIn("error_type", job)
            self.assertEqual(
                set(job), {"jobId", "operation", "phase", "status", "recordedAt"}
            )

    def test_jobs_endpoint_requires_credential_and_known_project(self) -> None:
        project = self._provisioned_project()

        unauthorized = self.client.get(
            f"/control/v1/clerk-orgs/org_123/projects/{project['id']}/jobs",
        )
        self.assertEqual(unauthorized.status_code, 401)

        missing = self.client.get(
            "/control/v1/clerk-orgs/org_123/projects/proj_missing/jobs",
            headers=self._control_auth(),
        )
        self.assertEqual(missing.status_code, 404)

    def test_jobs_endpoint_is_tenant_isolated(self) -> None:
        project = self._provisioned_project()
        self._seed_jobs(project["id"])
        other_project = self._create_project(org="org_other", name="Other")

        response = self.client.get(
            f"/control/v1/clerk-orgs/org_other/projects/{other_project['id']}/jobs",
            headers=self._control_auth(),
        )

        self.assertEqual(response.json()["jobs"], [])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_console_ops_depth.py -k JobsEndpoint -v`
Expected: FAIL — 404, route missing.

- [ ] **Step 3: Implement**

In `src/vexic/hosted_control_plane_http.py`, add inside `register_control_plane_routes`:

```python
    @app.get("/control/v1/clerk-orgs/{clerk_org_id}/projects/{project_id}/jobs")
    @_control_plane_storage_boundary
    async def list_control_plane_jobs(
        clerk_org_id: str,
        project_id: str,
        request: Request,
    ) -> JSONResponse:
        if not _has_control_plane_credential(request, control_plane_tokens):
            return _error_response(401, "unauthorized", "Invalid control-plane credential.")
        tenant_id = _resolve_control_tenant(service, clerk_org_id)
        if tenant_id is None:
            return _error_response(404, "not_found", "Project not found.")
        try:
            service.catalog.get_control_project(tenant_id, project_id)
        except PermissionError:
            return _error_response(404, "not_found", "Project not found.")
        raw_limit = request.query_params.get("limit", "50")
        try:
            limit = max(1, min(200, int(raw_limit)))
        except ValueError:
            limit = 50
        events = service.catalog.job_events(tenant_id, project_id=project_id, limit=limit)
        return JSONResponse({"jobs": [_job_payload(event) for event in events]})
```

And the module-level payload helper (deliberately omits `error_type` — the
customer surface is status-only; do not add it back without the Plan 2 support
context flag):

```python
def _job_payload(event) -> dict[str, object]:
    return {
        "jobId": event.job_id,
        "operation": event.operation,
        "phase": event.phase,
        "status": event.status,
        "recordedAt": event.recorded_at,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_console_ops_depth.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add src/vexic/hosted_control_plane_http.py tests/test_console_ops_depth.py
git commit -m "feat(control-plane): status-only project jobs endpoint"
```

---

### Task 9: Console Jobs tab

**Files:**
- Modify: `console/lib/control-plane-client.mjs`
- Modify: `console/lib/control-plane-store.mjs`
- Modify: `console/lib/control-plane-api.mjs`
- Modify: `console/lib/console-ui-state.mjs`
- Create: `console/app/api/control-plane/projects/[projectId]/jobs/route.ts`
- Create: `console/app/console/projects/[projectId]/jobs-tab.tsx`
- Modify: `console/app/console/projects/[projectId]/project-workspace.tsx`
- Test: `console/tests/console-ui-state.test.mjs`, `console/tests/console-ops.test.mjs`

**Interfaces:**
- Consumes: Task 8's `{"jobs": [...]}` payload (newest-first events).
- Produces: `jobRuns(events)` in `console-ui-state.mjs` grouping events into runs: `[{ jobId, phase, status, startedAt, finishedAt }]` where `status` is the latest event's status per job and runs sort newest-first; `listJobs(orgId, projectId)` client fn; `listJobsResponse` API fn; `<JobsTab />` component.

- [ ] **Step 1: Write the failing UI-state test**

Append to `console/tests/console-ui-state.test.mjs`:

```js
import { jobRuns } from "../lib/console-ui-state.mjs";

test("jobRuns groups events per job with latest status and time range", () => {
  const runs = jobRuns([
    { jobId: "job2", phase: "rem", status: "error", recordedAt: "2026-07-02T01:05:00Z" },
    { jobId: "job2", phase: "rem", status: "running", recordedAt: "2026-07-02T01:00:00Z" },
    { jobId: "job1", phase: "light", status: "ok", recordedAt: "2026-07-01T00:05:00Z" },
    { jobId: "job1", phase: "light", status: "running", recordedAt: "2026-07-01T00:00:00Z" }
  ]);

  assert.equal(runs.length, 2);
  assert.deepEqual(runs[0], {
    jobId: "job2",
    phase: "rem",
    status: "error",
    startedAt: "2026-07-02T01:00:00Z",
    finishedAt: "2026-07-02T01:05:00Z"
  });
  assert.equal(runs[1].status, "ok");
});

test("jobRuns leaves running jobs without finishedAt", () => {
  const runs = jobRuns([
    { jobId: "job3", phase: "deep", status: "running", recordedAt: "2026-07-02T02:00:00Z" }
  ]);

  assert.equal(runs[0].status, "running");
  assert.equal(runs[0].finishedAt, null);
});
```

- [ ] **Step 2: Write the failing API test**

Append to `console/tests/console-ops.test.mjs`:

```js
import { listJobsResponse } from "../lib/control-plane-api.mjs";

test("jobs endpoint responds with a status-only job list for a stub project", async () => {
  const { project } = await json(
    await createProjectResponse(request("POST", "/api/control-plane/projects", { name: "Alpha" }), authedOrg)
  );

  const jobs = await json(
    await listJobsResponse(
      request("GET", `/api/control-plane/projects/${project.id}/jobs`),
      authedOrg,
      project.id
    )
  );

  assert.ok(Array.isArray(jobs.jobs));
  for (const job of jobs.jobs) {
    assert.deepEqual(Object.keys(job).sort(), ["jobId", "operation", "phase", "recordedAt", "status"]);
  }
});
```

- [ ] **Step 3: Run console tests to verify they fail**

Run (from `console/`): `npm test`
Expected: FAIL — missing exports.

- [ ] **Step 4: Implement the lib layers**

1. `console/lib/console-ui-state.mjs`:

```js
const TERMINAL_JOB_STATUSES = new Set(["ok", "error"]);

export function jobRuns(events) {
  const byJob = new Map();
  for (const event of events) {
    const entries = byJob.get(event.jobId) ?? [];
    entries.push(event);
    byJob.set(event.jobId, entries);
  }
  const runs = [];
  for (const [jobId, entries] of byJob) {
    const ordered = [...entries].sort((a, b) => a.recordedAt.localeCompare(b.recordedAt));
    const latest = ordered[ordered.length - 1];
    runs.push({
      jobId,
      phase: latest.phase,
      status: latest.status,
      startedAt: ordered[0].recordedAt,
      finishedAt: TERMINAL_JOB_STATUSES.has(latest.status) ? latest.recordedAt : null
    });
  }
  return runs.sort((a, b) => b.startedAt.localeCompare(a.startedAt));
}
```

2. `console/lib/control-plane-client.mjs`:

```js
export async function listJobs(orgId, projectId) {
  const data = await request(orgId, "GET", `/projects/${encodeURIComponent(projectId)}/jobs?limit=50`);
  return data.jobs ?? [];
}
```

3. `console/lib/control-plane-store.mjs` — dispatch + stub + fail-closed entry:

```js
export function listJobs(orgId, projectId) {
  return selectedStore().listJobs(orgId, projectId);
}

function stubListJobs(orgId, projectId) {
  if (!stubGetProject(orgId, projectId)) {
    return null;
  }
  const base = Date.now() - 3 * 3_600_000;
  return [
    { jobId: "job_stub_3", operation: "run_dream_phase", phase: "deep", status: "running", recordedAt: new Date(base + 2 * 3_600_000).toISOString() },
    { jobId: "job_stub_2", operation: "run_dream_phase", phase: "rem", status: "ok", recordedAt: new Date(base + 3_600_000).toISOString() },
    { jobId: "job_stub_2", operation: "run_dream_phase", phase: "rem", status: "running", recordedAt: new Date(base + 3_540_000).toISOString() },
    { jobId: "job_stub_1", operation: "run_dream_phase", phase: "light", status: "error", recordedAt: new Date(base).toISOString() }
  ];
}
```

4. `console/lib/control-plane-api.mjs`:

```js
export async function listJobsResponse(_request, auth, projectId) {
  const denied = requireOrg(auth);
  if (denied) return denied;
  return storeResponse(
    () => listJobs(auth.orgId, projectId),
    (jobs) => (jobs ? json({ jobs }) : json({ error: "not_found" }, 404))
  );
}
```

5. Route file `console/app/api/control-plane/projects/[projectId]/jobs/route.ts` — same shape as the usage route, calling `listJobsResponse`.

- [ ] **Step 5: Run console tests to verify they pass**

Run (from `console/`): `npm test`
Expected: all PASS.

- [ ] **Step 6: Build the Jobs tab component**

Create `console/app/console/projects/[projectId]/jobs-tab.tsx`:

```tsx
"use client";

import { useEffect, useState } from "react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { jobRuns } from "@/lib/console-ui-state.mjs";

type JobEvent = {
  jobId: string;
  operation: string;
  phase: string | null;
  status: string;
  recordedAt: string;
};

type LoadState = "loading" | "ready" | "error";

const timeFormatter = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" });

function statusBadge(status: string) {
  if (status === "ok") return <Badge variant="secondary">Succeeded</Badge>;
  if (status === "running") return <Badge variant="outline">Running</Badge>;
  return <Badge variant="destructive">Failed</Badge>;
}

export default function JobsTab({ projectId }: { projectId: string }) {
  const [events, setEvents] = useState<JobEvent[]>([]);
  const [loadState, setLoadState] = useState<LoadState>("loading");

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        setLoadState("loading");
        const response = await fetch(`/api/control-plane/projects/${projectId}/jobs`, { cache: "no-store" });
        if (!response.ok) throw new Error(`Jobs load failed with ${response.status}`);
        const data = (await response.json()) as { jobs: JobEvent[] };
        if (cancelled) return;
        setEvents(data.jobs);
        setLoadState("ready");
      } catch {
        if (cancelled) return;
        setLoadState("error");
        toast.error("Background jobs failed to load.");
      }
    }
    load();
    return () => {
      cancelled = true;
    };
  }, [projectId]);

  const runs = jobRuns(events);

  return (
    <Card>
      <CardHeader>
        <CardTitle>Background jobs</CardTitle>
        <CardDescription>
          Vexic reviews recent conversations in the background and promotes durable facts. Recent runs appear
          here; runs from before project attribution was added are not shown.
        </CardDescription>
      </CardHeader>
      <CardContent>
        {loadState === "ready" && runs.length > 0 ? (
          <div className="mb-4 flex flex-wrap gap-4 text-sm text-muted-foreground">
            {["light", "rem", "deep"].map((phase) => {
              const lastOk = runs.find((run) => run.phase === phase && run.status === "ok");
              return (
                <span key={phase}>
                  <span className="capitalize">{phase}</span> last succeeded:{" "}
                  {lastOk?.finishedAt ? timeFormatter.format(new Date(lastOk.finishedAt)) : "never"}
                </span>
              );
            })}
          </div>
        ) : null}
        {loadState === "loading" ? (
          <Skeleton className="h-32 w-full" />
        ) : loadState === "error" ? (
          <p className="text-sm text-muted-foreground">Jobs could not be loaded. Refresh to retry.</p>
        ) : runs.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            No background runs recorded for this project yet.
          </p>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Phase</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Started</TableHead>
                <TableHead>Finished</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {runs.map((run) => (
                <TableRow key={run.jobId}>
                  <TableCell className="capitalize">{run.phase ?? "—"}</TableCell>
                  <TableCell>
                    {statusBadge(run.status)}
                    {run.status === "error" ? (
                      <span className="ml-2 text-xs text-muted-foreground">
                        We&apos;re looking into it — contact support if this persists.
                      </span>
                    ) : null}
                  </TableCell>
                  <TableCell>{timeFormatter.format(new Date(run.startedAt))}</TableCell>
                  <TableCell>{run.finishedAt ? timeFormatter.format(new Date(run.finishedAt)) : "—"}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </CardContent>
    </Card>
  );
}
```

- [ ] **Step 7: Wire the tab into the workspace**

In `project-workspace.tsx`:

1. `type Tab = "keys" | "usage" | "jobs" | "settings";`
2. Add to the `TabsList`: `<TabsTrigger value="jobs">Jobs</TabsTrigger>` between Usage and Settings.
3. Add `<TabsContent value="jobs"><JobsTab projectId={projectId} /></TabsContent>`.
4. `import JobsTab from "./jobs-tab";`

- [ ] **Step 8: Build and verify**

Run (from `console/`): `npm test && npm run build`
Expected: pass.

- [ ] **Step 9: Full-repo verification**

Run from repo root: `uv run pytest`
Expected: all pass.

- [ ] **Step 10: Commit**

```bash
git add console/
git commit -m "feat(console): background jobs tab with status-only run history"
```

---

## Post-plan checklist

- All 9 tasks landed → slices 1–3 of the spec are complete.
- Next: Plan 2 (billing scaffold + event history) — write it via superpowers:writing-plans against the same spec; event history includes the Support View failure-reason surface deferred from Task 8.
- The spec's remaining slices (retention, async job substrate, export, deletes) each require their flagged design check before their plan is written.
