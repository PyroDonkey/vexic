import assert from "node:assert/strict";
import test, { mock } from "node:test";

import {
  createProjectResponse,
  createAgentKeyResponse,
  listAgentKeysResponse,
  listProjectsResponse,
  revokeAgentKeyResponse,
  supportMetadataResponse,
  usageSummaryResponse
} from "../lib/control-plane-api.mjs";
import { resetStoreForTests } from "../lib/control-plane-store.mjs";

const authedOrg = { userId: "user_123", orgId: "org_123", isInternalSupport: false };
const staff = { userId: "user_staff", orgId: "org_vexic", isInternalSupport: true };
const originalEnv = {
  NODE_ENV: process.env.NODE_ENV,
  VEXIC_CONTROL_PLANE_TOKEN: process.env.VEXIC_CONTROL_PLANE_TOKEN,
  VEXIC_CONTROL_PLANE_URL: process.env.VEXIC_CONTROL_PLANE_URL
};

test.afterEach(() => {
  restoreEnv();
  mock.restoreAll();
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

test("project creation requires an active org and appears in the project list", async () => {
  resetStoreForTests();

  const denied = await createProjectResponse(request("POST", "/api/control-plane/projects", { name: "Alpha" }), {
    userId: "user_123",
    orgId: null,
    isInternalSupport: false
  });
  assert.equal(denied.status, 403);

  const created = await json(
    await createProjectResponse(request("POST", "/api/control-plane/projects", { name: "Alpha" }), authedOrg)
  );
  const listed = await json(await listProjectsResponse(request("GET", "/api/control-plane/projects"), authedOrg));

  assert.equal(created.project.name, "Alpha");
  assert.equal(listed.projects.length, 1);
  assert.equal(listed.projects[0].id, created.project.id);
});

test("agent key creation reveals the raw key once and later responses never expose it", async () => {
  resetStoreForTests();

  const { project } = await json(
    await createProjectResponse(request("POST", "/api/control-plane/projects", { name: "Alpha" }), authedOrg)
  );

  const created = await json(
    await createAgentKeyResponse(
      request("POST", `/api/control-plane/projects/${project.id}/keys`, {
        name: "local agent",
        agentScope: "writer"
      }),
      authedOrg,
      project.id
    )
  );
  assert.match(created.rawKey, /^vx_live_/);
  assert.equal(created.key.tenantId, project.tenantId);
  assert.equal(created.key.scopeTemplate.tenant_id, project.tenantId);
  assert.equal(created.key.scopeTemplate.project_id, project.id);
  assert.equal(created.key.scopeTemplate.agent_id, "writer");
  assert.equal(created.key.scopeTemplate.principal.principal_id, "writer");
  assert.deepEqual(created.key.scopeTemplate.capabilities, [
    "memory:write",
    "memory:search",
    "memory:expand"
  ]);

  const listedResponse = await listAgentKeysResponse(
    request("GET", `/api/control-plane/projects/${project.id}/keys`),
    authedOrg,
    project.id
  );
  const listedText = await listedResponse.text();
  assert.equal(listedText.includes(created.rawKey), false);

  const listed = JSON.parse(listedText);
  assert.equal(listed.keys.length, 1);
  assert.equal("rawKey" in listed.keys[0], false);
  assert.match(listed.keys[0].display, /^vx_live_[a-z0-9]+.../);
  assert.equal(listed.keys[0].scopeTemplate.tenant_id, project.tenantId);

  const revoked = await revokeAgentKeyResponse(
    request("DELETE", `/api/control-plane/projects/${project.id}/keys/${created.key.id}`),
    authedOrg,
    project.id,
    created.key.id
  );
  assert.equal(revoked.status, 204);

  const afterRevoke = await json(
    await listAgentKeysResponse(request("GET", `/api/control-plane/projects/${project.id}/keys`), authedOrg, project.id)
  );
  assert.equal(afterRevoke.keys.length, 0);
});

test("usage and support responses expose aggregates and metadata only", async () => {
  resetStoreForTests();

  const { project } = await json(
    await createProjectResponse(request("POST", "/api/control-plane/projects", { name: "Alpha" }), authedOrg)
  );

  const usageText = await (
    await usageSummaryResponse(request("GET", `/api/control-plane/projects/${project.id}/usage`), authedOrg, project.id)
  ).text();
  assert.doesNotMatch(usageText, /transcript|fact|searchQuery|rawMemory|messageText/i);
  const usage = JSON.parse(usageText).usage;
  assert.equal(new Date(usage.periodStart).getUTCDate(), 1);
  assert.ok(new Date(usage.periodEnd) > new Date(usage.periodStart));

  const forbiddenSupport = await supportMetadataResponse(request("GET", "/api/control-plane/support"), authedOrg);
  assert.equal(forbiddenSupport.status, 403);

  const supportText = await (await supportMetadataResponse(request("GET", "/api/control-plane/support"), staff)).text();
  assert.doesNotMatch(supportText, /transcript|fact|searchQuery|rawMemory|messageText/i);
  const support = JSON.parse(supportText);
  assert.equal(support.records[0].orgId, "org_vexic");
});

test("configured control-plane URL uses the hosted client and maps upstream auth failure", async () => {
  resetStoreForTests();
  process.env.NODE_ENV = "test";
  process.env.VEXIC_CONTROL_PLANE_URL = "https://api.example.test";
  delete process.env.VEXIC_CONTROL_PLANE_TOKEN;
  const calls = [];
  mock.method(console, "error", () => {});
  mock.method(globalThis, "fetch", async (url, options) => {
    calls.push({ url: String(url), options });
    return Response.json({ error: { code: "unauthorized", message: "bad token" } }, { status: 401 });
  });

  const response = await listProjectsResponse(request("GET", "/api/control-plane/projects"), authedOrg);
  const body = await json(response);

  assert.equal(response.status, 500);
  assert.deepEqual(body, { error: "control_plane_unavailable" });
  assert.equal(calls.length, 1);
  assert.match(new Headers(calls[0].options.headers).get("authorization"), /^Bearer/);
});

test("production without a control-plane URL fails closed after org guards", async () => {
  resetStoreForTests();
  process.env.NODE_ENV = "production";
  delete process.env.VEXIC_CONTROL_PLANE_URL;

  const denied = await listProjectsResponse(request("GET", "/api/control-plane/projects"), {
    userId: "user_123",
    orgId: null,
    isInternalSupport: false
  });
  assert.equal(denied.status, 403);
  assert.deepEqual(await json(denied), { error: "active_org_required" });

  const response = await listProjectsResponse(request("GET", "/api/control-plane/projects"), authedOrg);
  assert.equal(response.status, 500);
  assert.deepEqual(await json(response), { error: "control_plane_unavailable" });
});

function restoreEnv() {
  for (const [key, value] of Object.entries(originalEnv)) {
    if (value === undefined) {
      delete process.env[key];
    } else {
      process.env[key] = value;
    }
  }
}
