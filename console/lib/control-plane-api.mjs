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
import { ControlPlaneClientError } from "./control-plane-client.mjs";

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
  return storeResponse(() => listProjects(auth.orgId), (projects) => json({ projects: projects ?? [] }));
}

export async function createProjectResponse(request, auth) {
  const denied = requireOrg(auth);
  if (denied) return denied;
  const payload = await body(request);
  return storeResponse(
    () => createProject(auth.orgId, payload),
    (project) => json({ project }, 201)
  );
}

export async function getProjectResponse(_request, auth, projectId) {
  const denied = requireOrg(auth);
  if (denied) return denied;
  return storeResponse(
    () => getProject(auth.orgId, projectId),
    (project) => (project ? json({ project }) : json({ error: "not_found" }, 404))
  );
}

export async function listAgentKeysResponse(_request, auth, projectId) {
  const denied = requireOrg(auth);
  if (denied) return denied;
  return storeResponse(
    () => listAgentKeys(auth.orgId, projectId),
    (keys) => (keys ? json({ keys }) : json({ error: "not_found" }, 404))
  );
}

export async function createAgentKeyResponse(request, auth, projectId) {
  const denied = requireOrg(auth);
  if (denied) return denied;
  const payload = await body(request);
  return storeResponse(
    () => createAgentKey(auth.orgId, projectId, payload),
    (result) => (result ? json(result, 201) : json({ error: "not_found" }, 404))
  );
}

export async function revokeAgentKeyResponse(_request, auth, projectId, keyId) {
  const denied = requireOrg(auth);
  if (denied) return denied;
  return storeResponse(
    () => revokeAgentKey(auth.orgId, projectId, keyId),
    (revoked) => (revoked ? new Response(null, { status: 204 }) : json({ error: "not_found" }, 404))
  );
}

export async function usageSummaryResponse(_request, auth, projectId) {
  const denied = requireOrg(auth);
  if (denied) return denied;
  return storeResponse(
    () => usageSummary(auth.orgId, projectId),
    (summary) => (summary ? json({ usage: summary }) : json({ error: "not_found" }, 404))
  );
}

export async function supportMetadataResponse(_request, auth) {
  const denied = requireOrg(auth);
  if (denied) return denied;
  if (!auth.isInternalSupport) {
    return json({ error: "internal_support_required" }, 403);
  }
  return storeResponse(() => supportMetadata(auth.orgId), (records) => json({ records }));
}

async function storeResponse(operation, render) {
  try {
    return render(await operation());
  } catch (error) {
    if (error instanceof ControlPlaneClientError) {
      return json({ error: error.code }, error.status);
    }
    throw error;
  }
}
