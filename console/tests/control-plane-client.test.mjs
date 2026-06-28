import assert from "node:assert/strict";
import test, { mock } from "node:test";

import {
  createAgentKey,
  createProject,
  getProject,
  listAgentKeys,
  listProjects,
  revokeAgentKey,
  supportMetadata,
  usageSummary
} from "../lib/control-plane-client.mjs";

const originalEnv = {
  NODE_ENV: process.env.NODE_ENV,
  VEXIC_CONTROL_PLANE_TOKEN: process.env.VEXIC_CONTROL_PLANE_TOKEN,
  VEXIC_CONTROL_PLANE_URL: process.env.VEXIC_CONTROL_PLANE_URL
};

test.afterEach(() => {
  restoreEnv();
  mock.restoreAll();
});

test("createProject posts to the hosted org project endpoint", async () => {
  useClientEnv();
  const calls = [];
  mock.method(globalThis, "fetch", async (url, options) => {
    calls.push({ url: String(url), options });
    return Response.json(
      {
        project: {
          id: "proj_123",
          name: "Alpha",
          environment: "staging",
          createdAt: "2026-06-28T00:00:00Z"
        }
      },
      { status: 201 }
    );
  });

  const project = await createProject("org_123", { name: "Alpha", environment: "staging" });

  assert.equal(project.id, "proj_123");
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "https://api.example.test/control/v1/clerk-orgs/org_123/projects");
  assert.equal(calls[0].options.method, "POST");
  assert.equal(new Headers(calls[0].options.headers).get("authorization"), "Bearer secret-token");
  assert.equal(new Headers(calls[0].options.headers).get("content-type"), "application/json");
  assert.deepEqual(JSON.parse(calls[0].options.body), { name: "Alpha", environment: "staging" });
});

test("listProjects gets the hosted org project list", async () => {
  useClientEnv();
  const calls = [];
  mock.method(globalThis, "fetch", async (url, options) => {
    calls.push({ url: String(url), options });
    return Response.json({ projects: [{ id: "proj_123", name: "Alpha" }] });
  });

  const projects = await listProjects("org_123");

  assert.deepEqual(projects, [{ id: "proj_123", name: "Alpha" }]);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "https://api.example.test/control/v1/clerk-orgs/org_123/projects");
  assert.equal(calls[0].options.method, "GET");
  assert.equal(new Headers(calls[0].options.headers).get("authorization"), "Bearer secret-token");
  assert.equal(new Headers(calls[0].options.headers).get("content-type"), null);
  assert.equal(calls[0].options.body, undefined);
});

test("control-plane URL is trimmed before hosted requests", async () => {
  useClientEnv();
  process.env.VEXIC_CONTROL_PLANE_URL = " \nhttps://api.example.test/// \t";
  const calls = [];
  mock.method(globalThis, "fetch", async (url, options) => {
    calls.push({ url: String(url), options });
    return Response.json({ projects: [] });
  });

  await listProjects("org_123");

  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "https://api.example.test/control/v1/clerk-orgs/org_123/projects");
});

test("project and key operations map to hosted control-plane endpoints", async () => {
  useClientEnv();
  const calls = [];
  const responses = [
    Response.json({ project: { id: "proj_123" } }),
    Response.json({ keys: [{ id: "key_123" }] }),
    Response.json({ rawKey: "vx_abcd_secret", key: { id: "key_456" } }, { status: 201 }),
    new Response(null, { status: 204 })
  ];
  mock.method(globalThis, "fetch", async (url, options) => {
    calls.push({ url: String(url), options });
    return responses.shift();
  });

  assert.deepEqual(await getProject("org_123", "proj_123"), { id: "proj_123" });
  assert.deepEqual(await listAgentKeys("org_123", "proj_123"), [{ id: "key_123" }]);
  assert.deepEqual(await createAgentKey("org_123", "proj_123", { name: "local", agentScope: "writer" }), {
    rawKey: "vx_abcd_secret",
    key: { id: "key_456" }
  });
  assert.equal(await revokeAgentKey("org_123", "proj_123", "key_123"), true);

  assert.deepEqual(
    calls.map((call) => [call.options.method, call.url]),
    [
      ["GET", "https://api.example.test/control/v1/clerk-orgs/org_123/projects/proj_123"],
      ["GET", "https://api.example.test/control/v1/clerk-orgs/org_123/projects/proj_123/keys"],
      ["POST", "https://api.example.test/control/v1/clerk-orgs/org_123/projects/proj_123/keys"],
      ["POST", "https://api.example.test/control/v1/clerk-orgs/org_123/projects/proj_123/keys/key_123/revoke"]
    ]
  );
  assert.deepEqual(JSON.parse(calls[2].options.body), { name: "local", agentScope: "writer" });
});

test("usageSummary normalizes hosted totals for console display", async () => {
  useClientEnv();
  const calls = [];
  mock.method(globalThis, "fetch", async (url, options) => {
    calls.push({ url: String(url), options });
    return Response.json({
      usage: {
        projectId: "proj_123",
        periodStart: "2026-06-01T00:00:00Z",
        periodEnd: "2026-06-28T00:00:00Z",
        totals: {
          requests: 10,
          writes: 3,
          retrievals: 4,
          modelRequests: 99,
          inputTokens: 1000,
          outputTokens: 2000,
          totalTokens: 3000,
          estimatedCostMicros: 1234567
        },
        caps: {}
      }
    });
  });

  const usage = await usageSummary("org_123", "proj_123");

  assert.equal(calls[0].url, "https://api.example.test/control/v1/clerk-orgs/org_123/projects/proj_123/usage");
  assert.equal(calls[0].options.method, "GET");
  assert.deepEqual(usage, {
    projectId: "proj_123",
    periodStart: "2026-06-01T00:00:00Z",
    periodEnd: "2026-06-28T00:00:00Z",
    totals: {
      requests: 10,
      writes: 3,
      retrievals: 4,
      cost: 1.234567
    },
    caps: {}
  });
});

test("upstream HTTP statuses map to console-safe errors", async () => {
  useClientEnv();
  const cases = [
    [400, 400, "invalid_request", false],
    [404, 404, "not_found", false],
    [409, 409, "conflict", true],
    [401, 500, "control_plane_unavailable", true],
    [403, 500, "control_plane_unavailable", true],
    [503, 500, "control_plane_unavailable", true]
  ];

  for (const [upstreamStatus, expectedStatus, expectedCode, shouldLog] of cases) {
    mock.restoreAll();
    const logs = [];
    mock.method(console, "error", (...args) => logs.push(args));
    mock.method(globalThis, "fetch", async () =>
      Response.json({ error: { code: "upstream_error", message: "upstream failed" } }, { status: upstreamStatus })
    );

    await assert.rejects(() => listProjects("org_123"), (error) => {
      assert.equal(error.status, expectedStatus);
      assert.equal(error.code, expectedCode);
      return true;
    });
    assert.equal(logs.length > 0, shouldLog);
  }
});

test("network failures map to 502 with a timeout signal and sanitized logging", async () => {
  useClientEnv();
  const logs = [];
  let signal;
  mock.method(console, "error", (...args) => logs.push(args));
  mock.method(globalThis, "fetch", async (_url, options) => {
    signal = options.signal;
    throw new TypeError("connect failed");
  });

  await assert.rejects(() => listProjects("org_123"), (error) => {
    assert.equal(error.status, 502);
    assert.equal(error.code, "control_plane_unavailable");
    return true;
  });

  assert.ok(signal instanceof AbortSignal);
  assert.equal(logs.length, 1);
  assert.equal(JSON.stringify(logs).includes("secret-token"), false);
});

test("malformed control-plane URLs still map fetch failures to 502", async () => {
  useClientEnv();
  process.env.VEXIC_CONTROL_PLANE_URL = "not a url";
  const logs = [];
  mock.method(console, "error", (...args) => logs.push(args));

  await assert.rejects(() => listProjects("org_123"), (error) => {
    assert.equal(error.status, 502);
    assert.equal(error.code, "control_plane_unavailable");
    return true;
  });

  assert.equal(logs.length, 1);
  assert.equal(logs[0][1].path, "<invalid-url>");
});

test("supportMetadata returns empty records without a hosted fetch", async () => {
  useClientEnv();
  mock.method(globalThis, "fetch", async () => {
    throw new Error("support metadata should not fetch");
  });

  assert.deepEqual(await supportMetadata("org_123"), []);
});

function useClientEnv() {
  process.env.NODE_ENV = "test";
  process.env.VEXIC_CONTROL_PLANE_URL = "https://api.example.test";
  process.env.VEXIC_CONTROL_PLANE_TOKEN = "secret-token";
}

function restoreEnv() {
  for (const [key, value] of Object.entries(originalEnv)) {
    if (value === undefined) {
      delete process.env[key];
    } else {
      process.env[key] = value;
    }
  }
}
