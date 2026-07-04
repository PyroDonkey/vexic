const DEFAULT_TIMEOUT_MS = 10_000;

export class ControlPlaneClientError extends Error {
  constructor(status, code, message) {
    super(message);
    this.name = "ControlPlaneClientError";
    this.status = status;
    this.code = code;
  }
}

export async function listProjects(orgId) {
  const data = await request(orgId, "GET", "/projects");
  return data.projects;
}

export async function createProject(orgId, input = {}) {
  const data = await request(orgId, "POST", "/projects", { body: input });
  return data.project;
}

export async function getProject(orgId, projectId) {
  const data = await request(orgId, "GET", `/projects/${encodeURIComponent(projectId)}`);
  return data.project;
}

export async function listAgentKeys(orgId, projectId, { includeRevoked = false } = {}) {
  const suffix = includeRevoked ? "?include=revoked" : "";
  const data = await request(orgId, "GET", `/projects/${encodeURIComponent(projectId)}/keys${suffix}`);
  return data.keys;
}

export async function createAgentKey(orgId, projectId, input = {}) {
  return request(orgId, "POST", `/projects/${encodeURIComponent(projectId)}/keys`, { body: input });
}

export async function revokeAgentKey(orgId, projectId, keyId) {
  await request(orgId, "POST", `/projects/${encodeURIComponent(projectId)}/keys/${encodeURIComponent(keyId)}/revoke`);
  return true;
}

export async function usageSummary(orgId, projectId) {
  const data = await request(orgId, "GET", `/projects/${encodeURIComponent(projectId)}/usage`);
  return normalizeUsage(data.usage);
}

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

export async function supportMetadata(_orgId) {
  return [];
}

async function request(orgId, method, path, { body } = {}) {
  const url = urlFor(orgId, path);
  const timeout = requestTimeout();
  let response;
  try {
    response = await fetch(url, {
      method,
      headers: headersFor(body),
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: timeout.signal
    });
  } catch (error) {
    logNetworkFailure(error, url);
    throw new ControlPlaneClientError(502, "control_plane_unavailable", "Control plane is unavailable.");
  } finally {
    timeout.clear();
  }
  if (!response.ok) {
    throw await errorForResponse(response, url);
  }
  if (response.status === 204) {
    return null;
  }
  return jsonBody(response);
}

function urlFor(orgId, path) {
  const baseUrl = String(process.env.VEXIC_CONTROL_PLANE_URL ?? "").trim().replace(/\/+$/, "");
  return `${baseUrl}/control/v1/clerk-orgs/${encodeURIComponent(orgId)}${path}`;
}

function headersFor(body) {
  const headers = {
    authorization: `Bearer ${process.env.VEXIC_CONTROL_PLANE_TOKEN ?? ""}`
  };
  if (body !== undefined) {
    headers["content-type"] = "application/json";
  }
  return headers;
}

async function errorForResponse(response, url) {
  const body = await jsonBody(response);
  const upstreamCode = body?.error?.code;
  const upstreamMessage = body?.error?.message;
  if (response.status === 400) {
    return new ControlPlaneClientError(400, "invalid_request", upstreamMessage ?? "Invalid control-plane request.");
  }
  if (response.status === 404) {
    return new ControlPlaneClientError(404, "not_found", upstreamMessage ?? "Control-plane resource not found.");
  }
  if (response.status === 409) {
    logUpstreamFailure(response.status, upstreamCode, url);
    return new ControlPlaneClientError(409, "conflict", upstreamMessage ?? "Control-plane write conflict.");
  }

  if (response.status === 429) {
    logUpstreamFailure(response.status, upstreamCode, url);
    return new ControlPlaneClientError(429, "rate_limited", upstreamMessage ?? "Control plane rate limit exceeded.");
  }

  // Anything else is unexpected; always log so outages, quota denials, and
  // auth failures are distinguishable in server logs.
  logUpstreamFailure(response.status, upstreamCode, url);
  return new ControlPlaneClientError(500, "control_plane_unavailable", "Control plane is unavailable.");
}

async function jsonBody(response) {
  try {
    return await response.json();
  } catch {
    return {};
  }
}

function logUpstreamFailure(status, code, url) {
  console.error("control-plane upstream error", {
    status,
    code: code ?? "unknown",
    path: pathForLog(url)
  });
}

function logNetworkFailure(error, url) {
  console.error("control-plane fetch failed", {
    errorName: error?.name ?? "Error",
    path: pathForLog(url)
  });
}

function requestTimeout() {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), DEFAULT_TIMEOUT_MS);
  timer.unref?.();
  return {
    clear: () => clearTimeout(timer),
    signal: controller.signal
  };
}

function pathForLog(url) {
  try {
    return new URL(url).pathname;
  } catch {
    return "<invalid-url>";
  }
}

function normalizeUsage(usage) {
  const totals = usage?.totals ?? {};
  return {
    ...usage,
    totals: {
      requests: totals.requests ?? 0,
      writes: totals.writes ?? 0,
      retrievals: totals.retrievals ?? 0,
      cost: (totals.estimatedCostMicros ?? 0) / 1_000_000
    },
    caps: usage?.caps ?? {}
  };
}
