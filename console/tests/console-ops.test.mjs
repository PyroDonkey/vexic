import assert from "node:assert/strict";
import test from "node:test";

import {
  createAgentKeyResponse,
  createProjectResponse,
  listAgentKeysResponse,
  revokeAgentKeyResponse,
  usageByKeyResponse,
  usageDailyResponse
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
