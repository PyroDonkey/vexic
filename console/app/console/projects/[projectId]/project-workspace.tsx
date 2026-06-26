"use client";

import { useEffect, useState } from "react";

type Project = {
  id: string;
  name: string;
  environment: string;
  createdAt: string;
};

type AgentKey = {
  id: string;
  name: string;
  capability: string;
  agentScope: string;
  display: string;
  createdAt: string;
};

type Usage = {
  totals: Record<string, number>;
  caps: Record<string, number>;
};

type Tab = "keys" | "usage" | "settings";

export default function ProjectWorkspace({ projectId }: { projectId: string }) {
  const [tab, setTab] = useState<Tab>("keys");
  const [project, setProject] = useState<Project | null>(null);
  const [keys, setKeys] = useState<AgentKey[]>([]);
  const [usage, setUsage] = useState<Usage | null>(null);
  const [rawKey, setRawKey] = useState("");
  const [name, setName] = useState("");
  const [agentScope, setAgentScope] = useState("shared");

  async function loadProject() {
    const response = await fetch(`/api/control-plane/projects/${projectId}`, { cache: "no-store" });
    if (response.ok) {
      const data = (await response.json()) as { project: Project };
      setProject(data.project);
    }
  }

  async function loadKeys() {
    const response = await fetch(`/api/control-plane/projects/${projectId}/keys`, { cache: "no-store" });
    if (response.ok) {
      const data = (await response.json()) as { keys: AgentKey[] };
      setKeys(data.keys);
    }
  }

  async function loadUsage() {
    const response = await fetch(`/api/control-plane/projects/${projectId}/usage`, { cache: "no-store" });
    if (response.ok) {
      const data = (await response.json()) as { usage: Usage };
      setUsage(data.usage);
    }
  }

  useEffect(() => {
    void loadProject();
    void loadKeys();
    void loadUsage();
  }, [projectId]);

  async function createKey() {
    const response = await fetch(`/api/control-plane/projects/${projectId}/keys`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ name, agentScope })
    });

    if (!response.ok) return;

    const data = (await response.json()) as { rawKey: string; key: AgentKey };
    setRawKey(data.rawKey);
    setKeys((current) => [data.key, ...current.filter((key) => key.id !== data.key.id)]);
    setName("");
  }

  async function revokeKey(keyId: string) {
    const response = await fetch(`/api/control-plane/projects/${projectId}/keys/${keyId}`, { method: "DELETE" });
    if (response.ok) {
      setKeys((current) => current.filter((key) => key.id !== keyId));
    }
  }

  return (
    <>
      <header className="page-title">
        <div>
          <div className="eyebrow">Project workspace</div>
          <h1>{project?.name ?? "Project"}</h1>
          <p className="muted">Manage machine credentials and aggregate operational limits.</p>
        </div>
      </header>

      <div className="tabs" role="tablist" aria-label="Project workspace tabs">
        <button className="tab" type="button" aria-selected={tab === "keys"} onClick={() => setTab("keys")}>
          Agent API Keys
        </button>
        <button className="tab" type="button" aria-selected={tab === "usage"} onClick={() => setTab("usage")}>
          Usage & Caps
        </button>
        <button className="tab" type="button" aria-selected={tab === "settings"} onClick={() => setTab("settings")}>
          Project Settings
        </button>
      </div>

      {tab === "keys" ? (
        <section className="panel">
          <h2>Agent API Keys</h2>
          <div className="form-row">
            <input value={name} onChange={(event) => setName(event.target.value)} placeholder="Key name" />
            <input value={agentScope} onChange={(event) => setAgentScope(event.target.value)} placeholder="Agent scope" />
            <button className="button" type="button" onClick={createKey}>
              Create
            </button>
          </div>
          {rawKey ? (
            <div className="raw-key" role="status">
              <strong>Raw key</strong>
              <p>This value will not appear in the list again.</p>
              <code>{rawKey}</code>
              <div className="actions">
                <button className="button secondary" type="button" onClick={() => setRawKey("")}>
                  Dismiss
                </button>
              </div>
            </div>
          ) : null}
          {keys.map((key) => (
            <div className="key-row" key={key.id}>
              <div>
                <strong>{key.name}</strong>
                <div className="muted">
                  {key.capability} · {key.agentScope} · <code>{key.display}</code>
                </div>
              </div>
              <button className="button danger" type="button" onClick={() => revokeKey(key.id)}>
                Revoke
              </button>
            </div>
          ))}
        </section>
      ) : null}

      {tab === "usage" ? (
        <section className="metrics">
          {usage
            ? Object.entries(usage.totals).map(([label, value]) => (
                <div className="metric" key={label}>
                  <span className="muted">{label}</span>
                  <strong>{value}</strong>
                  <span className="muted">cap {usage.caps[label]}</span>
                </div>
              ))
            : null}
        </section>
      ) : null}

      {tab === "settings" ? (
        <section className="panel">
          <h2>Project Settings</h2>
          <div className="metric-row">
            <span className="muted">Project ID</span>
            <code>{projectId}</code>
          </div>
          <div className="metric-row">
            <span className="muted">Environment</span>
            <strong>{project?.environment ?? "production"}</strong>
          </div>
        </section>
      ) : null}
    </>
  );
}
