import {
  createAgentKey,
  createProject,
  getProject,
  listAgentKeys,
  listProjects,
  revokeAgentKey,
  supportMetadata,
  usageSummary
} from "./control-plane-store.mjs";

function json(body, status = 200) {
  return Response.json(body, { status });
}

function requireUser(auth) {
  if (!auth?.userId) {
    return json({ error: "sign_in_required" }, 401);
  }
  return null;
}

function requireOrg(auth) {
  const denied = requireUser(auth);
  if (denied) {
    return denied;
  }
  return auth.orgId ? null : json({ error: "active_org_required" }, 403);
}

async function body(request) {
  try {
    return await request.json();
  } catch {
    return {};
  }
}

export async function listProjectsResponse(_request, auth) {
  const denied = requireOrg(auth);
  if (denied) return denied;
  return json({ projects: listProjects(auth.orgId) });
}

export async function createProjectResponse(request, auth) {
  const denied = requireOrg(auth);
  if (denied) return denied;
  return json({ project: createProject(auth.orgId, await body(request)) }, 201);
}

export async function getProjectResponse(_request, auth, projectId) {
  const denied = requireOrg(auth);
  if (denied) return denied;
  const project = getProject(auth.orgId, projectId);
  return project ? json({ project }) : json({ error: "not_found" }, 404);
}

export async function listAgentKeysResponse(_request, auth, projectId) {
  const denied = requireOrg(auth);
  if (denied) return denied;
  const keys = listAgentKeys(auth.orgId, projectId);
  return keys ? json({ keys }) : json({ error: "not_found" }, 404);
}

export async function createAgentKeyResponse(request, auth, projectId) {
  const denied = requireOrg(auth);
  if (denied) return denied;
  const result = createAgentKey(auth.orgId, projectId, await body(request));
  return result ? json(result, 201) : json({ error: "not_found" }, 404);
}

export async function revokeAgentKeyResponse(_request, auth, projectId, keyId) {
  const denied = requireOrg(auth);
  if (denied) return denied;
  return revokeAgentKey(auth.orgId, projectId, keyId) ? new Response(null, { status: 204 }) : json({ error: "not_found" }, 404);
}

export async function usageSummaryResponse(_request, auth, projectId) {
  const denied = requireOrg(auth);
  if (denied) return denied;
  const summary = usageSummary(auth.orgId, projectId);
  return summary ? json({ usage: summary }) : json({ error: "not_found" }, 404);
}

export async function supportMetadataResponse(_request, auth) {
  const denied = requireOrg(auth);
  if (denied) return denied;
  if (!auth.isInternalSupport) {
    return json({ error: "internal_support_required" }, 403);
  }
  return json({ records: supportMetadata(auth.orgId) });
}
