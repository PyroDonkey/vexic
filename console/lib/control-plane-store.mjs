import { createHash, randomBytes, randomUUID } from "node:crypto";

import * as client from "./control-plane-client.mjs";
import { ControlPlaneClientError } from "./control-plane-client.mjs";

const projectsByOrg = new Map();
const keysByProject = new Map();

function now() {
  return new Date().toISOString();
}

function id(prefix) {
  return `${prefix}_${randomUUID().replaceAll("-", "").slice(0, 12)}`;
}

function tenantId(orgId) {
  return `tenant_${createHash("sha256").update(String(orgId)).digest("hex").slice(0, 16)}`;
}

function safeName(value, fallback) {
  const name = String(value ?? "").trim();
  return name.length > 0 ? name.slice(0, 80) : fallback;
}

function orgProjects(orgId) {
  if (!projectsByOrg.has(orgId)) {
    projectsByOrg.set(orgId, []);
  }
  return projectsByOrg.get(orgId);
}

export function resetStoreForTests() {
  projectsByOrg.clear();
  keysByProject.clear();
}

export function createProject(orgId, input = {}) {
  return selectedStore().createProject(orgId, input);
}

export function listProjects(orgId) {
  return selectedStore().listProjects(orgId);
}

export function getProject(orgId, projectId) {
  return selectedStore().getProject(orgId, projectId);
}

export function createAgentKey(orgId, projectId, input = {}) {
  return selectedStore().createAgentKey(orgId, projectId, input);
}

export function listAgentKeys(orgId, projectId) {
  return selectedStore().listAgentKeys(orgId, projectId);
}

export function revokeAgentKey(orgId, projectId, keyId) {
  return selectedStore().revokeAgentKey(orgId, projectId, keyId);
}

export function usageSummary(orgId, projectId) {
  return selectedStore().usageSummary(orgId, projectId);
}

export function supportMetadata(orgId) {
  return selectedStore().supportMetadata(orgId);
}

function stubCreateProject(orgId, input = {}) {
  const timestamp = now();
  const project = {
    id: id("proj"),
    tenantId: tenantId(orgId),
    orgId,
    name: safeName(input.name, "Untitled project"),
    environment: safeName(input.environment, "production"),
    createdAt: timestamp,
    updatedAt: timestamp
  };
  orgProjects(orgId).push(project);
  return project;
}

function stubListProjects(orgId) {
  return [...orgProjects(orgId)];
}

function stubGetProject(orgId, projectId) {
  return orgProjects(orgId).find((project) => project.id === projectId) ?? null;
}

function stubCreateAgentKey(orgId, projectId, input = {}) {
  const project = stubGetProject(orgId, projectId);
  if (!project) {
    return null;
  }

  const rawKey = `vx_live_${randomBytes(24).toString("hex")}`;
  const timestamp = now();
  const key = {
    id: id("key"),
    tenantId: project.tenantId,
    projectId,
    name: safeName(input.name, "Agent key"),
    capability: "v1-memory",
    agentScope: safeName(input.agentScope, "shared"),
    prefix: rawKey.slice(0, 16),
    last4: rawKey.slice(-4),
    display: `${rawKey.slice(0, 16)}...${rawKey.slice(-4)}`,
    keyHash: createHash("sha256").update(rawKey).digest("hex"),
    createdAt: timestamp,
    revokedAt: null
  };

  const keys = keysByProject.get(projectId) ?? [];
  keys.push(key);
  keysByProject.set(projectId, keys);

  return {
    rawKey,
    key: publicKey(key)
  };
}

function stubListAgentKeys(orgId, projectId) {
  if (!stubGetProject(orgId, projectId)) {
    return null;
  }

  return (keysByProject.get(projectId) ?? []).filter((key) => !key.revokedAt).map(publicKey);
}

function stubRevokeAgentKey(orgId, projectId, keyId) {
  if (!stubGetProject(orgId, projectId)) {
    return false;
  }

  const key = (keysByProject.get(projectId) ?? []).find((item) => item.id === keyId);
  if (!key || key.revokedAt) {
    return false;
  }

  key.revokedAt = now();
  return true;
}

function stubUsageSummary(orgId, projectId) {
  const project = stubGetProject(orgId, projectId);
  if (!project) {
    return null;
  }

  const current = new Date();
  const periodStart = new Date(Date.UTC(current.getUTCFullYear(), current.getUTCMonth(), 1));
  const periodEnd = new Date(Date.UTC(current.getUTCFullYear(), current.getUTCMonth() + 1, 0, 23, 59, 59, 999));
  const keyCount = stubListAgentKeys(orgId, projectId)?.length ?? 0;
  return {
    projectId,
    periodStart: periodStart.toISOString(),
    periodEnd: periodEnd.toISOString(),
    totals: {
      requests: 1240,
      writes: 94,
      retrievals: 876,
      agentKeys: keyCount,
      storageMb: 18
    },
    caps: {
      requests: 100000,
      writes: 10000,
      retrievals: 100000,
      agentKeys: 20,
      storageMb: 1024
    }
  };
}

function stubSupportMetadata(orgId) {
  const projects = stubListProjects(orgId);
  const timestamp = now();
  return [
    {
      ticketId: "support_stub_001",
      orgId,
      projectIds: projects.map((project) => project.id),
      status: "metadata-only",
      createdAt: timestamp,
      updatedAt: timestamp
    }
  ];
}

function publicKey(key) {
  return {
    id: key.id,
    tenantId: key.tenantId,
    projectId: key.projectId,
    name: key.name,
    capability: key.capability,
    agentScope: key.agentScope,
    scopeTemplate: scopeTemplate(key),
    prefix: key.prefix,
    last4: key.last4,
    display: key.display,
    createdAt: key.createdAt,
    revokedAt: key.revokedAt
  };
}

function scopeTemplate(key) {
  return {
    tenant_id: key.tenantId,
    project_id: key.projectId,
    agent_id: key.agentScope === "shared" ? null : key.agentScope,
    principal: {
      principal_id: key.agentScope,
      principal_type: "agent"
    },
    trust_boundary: "networked",
    capabilities: ["memory:write", "memory:search", "memory:expand"]
  };
}

function selectedStore() {
  if (controlPlaneUrlConfigured()) {
    return client;
  }
  if (process.env.NODE_ENV === "production") {
    return failClosedStore;
  }
  return stubStore;
}

function controlPlaneUrlConfigured() {
  return String(process.env.VEXIC_CONTROL_PLANE_URL ?? "").trim().length > 0;
}

const stubStore = {
  createProject: stubCreateProject,
  listProjects: stubListProjects,
  getProject: stubGetProject,
  createAgentKey: stubCreateAgentKey,
  listAgentKeys: stubListAgentKeys,
  revokeAgentKey: stubRevokeAgentKey,
  usageSummary: stubUsageSummary,
  supportMetadata: stubSupportMetadata
};

const failClosedStore = {
  createProject: notConfigured,
  listProjects: notConfigured,
  getProject: notConfigured,
  createAgentKey: notConfigured,
  listAgentKeys: notConfigured,
  revokeAgentKey: notConfigured,
  usageSummary: notConfigured,
  supportMetadata: notConfigured
};

function notConfigured() {
  throw new ControlPlaneClientError(500, "control_plane_unavailable", "Control plane is not configured.");
}
