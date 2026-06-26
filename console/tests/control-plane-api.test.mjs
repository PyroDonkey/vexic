import assert from "node:assert/strict";
import test from "node:test";

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
