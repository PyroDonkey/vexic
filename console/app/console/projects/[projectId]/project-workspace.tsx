"use client";

import { Copy, KeyRound, ShieldCheck, SlidersHorizontal, Trash2 } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { toast } from "sonner";

import { BarList } from "@/components/tremor/bar-list";
import { UsageMeter } from "@/components/tremor/usage-meter";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Separator } from "@/components/ui/separator";
import { Skeleton } from "@/components/ui/skeleton";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { usageRows } from "@/lib/console-ui-state.mjs";

type Project = {
  id: string;
  tenantId: string;
  name: string;
  environment: string;
  createdAt: string;
};

type ScopeTemplate = {
  tenant_id: string;
  project_id: string;
  session_id?: string;
  agent_id: string | null;
  principal: {
    principal_id: string;
    principal_type: string;
  };
  trust_boundary: string;
  capabilities: string[];
};

type AgentKey = {
  id: string;
  tenantId: string;
  name: string;
  capability: string;
  agentScope: string;
  scopeTemplate: ScopeTemplate;
  display: string;
  createdAt: string;
};

type Usage = {
  periodStart: string;
  periodEnd: string;
  totals: Record<string, number>;
  caps: Record<string, number>;
};

type Tab = "keys" | "usage" | "settings";
type LoadState = "loading" | "ready" | "error";

const dateFormatter = new Intl.DateTimeFormat(undefined, { dateStyle: "medium" });

export default function ProjectWorkspace({ projectId }: { projectId: string }) {
  const [tab, setTab] = useState<Tab>("keys");
  const [project, setProject] = useState<Project | null>(null);
  const [keys, setKeys] = useState<AgentKey[]>([]);
  const [usage, setUsage] = useState<Usage | null>(null);
  const [rawKey, setRawKey] = useState("");
  const [createdKey, setCreatedKey] = useState<AgentKey | null>(null);
  const [name, setName] = useState("");
  const [agentScope, setAgentScope] = useState("shared");
  const [projectLoadState, setProjectLoadState] = useState<LoadState>("loading");
  const [keysLoadState, setKeysLoadState] = useState<LoadState>("loading");
  const [usageLoadState, setUsageLoadState] = useState<LoadState>("loading");
  // Bumped by every loadKeys call and by createKey, so an in-flight key-list
  // fetch that resolves after a newer write cannot clobber the fresher state.
  const keysRequestSeq = useRef(0);

  async function loadProject() {
    try {
      setProjectLoadState("loading");
      const response = await fetch(`/api/control-plane/projects/${projectId}`, { cache: "no-store" });
      if (!response.ok) throw new Error(`Project load failed with ${response.status}`);
      const data = (await response.json()) as { project: Project };
      setProject(data.project);
      setProjectLoadState("ready");
    } catch {
      setProjectLoadState("error");
      toast.error("Project details failed to load.");
    }
  }

  async function loadKeys() {
    const seq = ++keysRequestSeq.current;
    try {
      setKeysLoadState("loading");
      const response = await fetch(`/api/control-plane/projects/${projectId}/keys`, { cache: "no-store" });
      if (!response.ok) throw new Error(`Key list failed with ${response.status}`);
      const data = (await response.json()) as { keys: AgentKey[] };
      if (seq !== keysRequestSeq.current) return;
      setKeys(data.keys);
      setKeysLoadState("ready");
    } catch {
      if (seq !== keysRequestSeq.current) return;
      setKeysLoadState("error");
      toast.error("Agent API Keys failed to load.");
    }
  }

  async function loadUsage() {
    try {
      setUsageLoadState("loading");
      const response = await fetch(`/api/control-plane/projects/${projectId}/usage`, { cache: "no-store" });
      if (!response.ok) throw new Error(`Usage load failed with ${response.status}`);
      const data = (await response.json()) as { usage: Usage };
      setUsage(data.usage);
      setUsageLoadState("ready");
    } catch {
      setUsageLoadState("error");
      toast.error("Usage failed to load.");
    }
  }

  useEffect(() => {
    setRawKey("");
    setCreatedKey(null);
    void loadProject();
    void loadKeys();
    void loadUsage();
  }, [projectId]);

  async function createKey() {
    const trimmedName = name.trim();
    if (!trimmedName) return;

    let response: Response;
    try {
      response = await fetch(`/api/control-plane/projects/${projectId}/keys`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ name: trimmedName, agentScope: agentScope.trim() || "shared" })
      });
    } catch {
      toast.error("Agent API Key creation failed.");
      return;
    }

    if (!response.ok) {
      toast.error("Agent API Key creation failed.");
      return;
    }

    const data = (await response.json()) as { rawKey: string; key: AgentKey };
    keysRequestSeq.current += 1;
    setRawKey(data.rawKey);
    setCreatedKey(data.key);
    setKeys((current) => [data.key, ...current.filter((key) => key.id !== data.key.id)]);
    setKeysLoadState("ready");
    setName("");
  }

  async function revokeKey(keyId: string) {
    try {
      const response = await fetch(`/api/control-plane/projects/${projectId}/keys/${keyId}`, { method: "DELETE" });
      if (!response.ok) throw new Error(`Key revoke failed with ${response.status}`);
      setKeys((current) => current.filter((key) => key.id !== keyId));
    } catch {
      toast.error("Agent API Key revocation failed.");
    }
  }

  async function copyRawKey() {
    try {
      await navigator.clipboard.writeText(rawKey);
      toast.success("Raw key copied.");
    } catch {
      toast.error("Raw key could not be copied.");
    }
  }

  async function copyScope(scope: ScopeTemplate) {
    try {
      await navigator.clipboard.writeText(scopeJson(scope));
      toast.success("Scope template copied.");
    } catch {
      toast.error("Scope template could not be copied.");
    }
  }

  const usageRowData = usage ? usageRows(usage) : [];

  return (
    <div className="grid gap-6">
      <header className="flex flex-col gap-3 md:flex-row md:items-end md:justify-between">
        <div className="grid gap-1">
          <Badge className="w-fit" variant="outline">
            Project workspace
          </Badge>
          {projectLoadState === "loading" ? (
            <Skeleton className="h-8 w-48" />
          ) : (
            <h1 className="text-2xl font-semibold text-foreground md:text-3xl">{project?.name ?? "Project"}</h1>
          )}
          <p className="text-sm text-muted-foreground">Manage machine credentials and aggregate operational limits.</p>
        </div>
        <Badge className="w-fit" variant="secondary">
          {project?.environment ?? "production"}
        </Badge>
      </header>

      <Tabs value={tab} onValueChange={(value) => setTab(value as Tab)}>
        <TabsList>
          <TabsTrigger value="keys">
            <KeyRound />
            Agent API Keys
          </TabsTrigger>
          <TabsTrigger value="usage">
            <ShieldCheck />
            Usage & Caps
          </TabsTrigger>
          <TabsTrigger value="settings">
            <SlidersHorizontal />
            Project Settings
          </TabsTrigger>
        </TabsList>

        <TabsContent value="keys" className="grid gap-4">
          <Card>
            <CardHeader>
              <CardTitle>Create Agent API Key</CardTitle>
              <CardDescription>The raw key is shown once and never returned by list responses.</CardDescription>
            </CardHeader>
            <CardContent className="grid gap-4">
              <form
                className="grid gap-3 md:grid-cols-[minmax(0,1fr)_minmax(12rem,16rem)_auto]"
                onSubmit={(event) => {
                  event.preventDefault();
                  void createKey();
                }}
              >
                <Input
                  aria-label="Key name"
                  value={name}
                  onChange={(event) => setName(event.target.value)}
                  placeholder="Key name"
                />
                <Input
                  aria-label="Agent scope"
                  value={agentScope}
                  onChange={(event) => setAgentScope(event.target.value)}
                  placeholder="Agent scope"
                />
                <Button disabled={!name.trim()} type="submit">
                  <KeyRound />
                  Create
                </Button>
              </form>

              {rawKey ? (
                <div className="grid gap-3 rounded-lg border border-primary/30 bg-primary/10 p-4" role="status">
                  <div>
                    <strong className="text-sm">Raw key</strong>
                    <p className="text-sm text-muted-foreground">This value will not appear in the list again.</p>
                  </div>
                  <code className="block overflow-x-auto rounded-md bg-background/80 px-3 py-2 text-xs">{rawKey}</code>
                  {createdKey ? (
                    <div className="grid gap-2">
                      <strong className="text-sm">Scope template</strong>
                      <pre className="max-h-52 overflow-auto rounded-md bg-background/80 p-3 text-xs">{scopeJson(createdKey.scopeTemplate)}</pre>
                    </div>
                  ) : null}
                  <div className="flex flex-wrap gap-2">
                    <Button className="w-fit" type="button" variant="outline" onClick={copyRawKey}>
                      <Copy />
                      Copy
                    </Button>
                    {createdKey ? (
                      <Button
                        className="w-fit"
                        type="button"
                        variant="outline"
                        onClick={() => copyScope(createdKey.scopeTemplate)}
                      >
                        <Copy />
                        Copy scope
                      </Button>
                    ) : null}
                    <Button
                      className="w-fit"
                      type="button"
                      variant="outline"
                      onClick={() => {
                        setRawKey("");
                        setCreatedKey(null);
                      }}
                    >
                      Dismiss
                    </Button>
                  </div>
                </div>
              ) : null}
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Active keys</CardTitle>
              <CardDescription>List and revoke project-scoped credentials.</CardDescription>
            </CardHeader>
            <CardContent>
              {keysLoadState === "loading" ? (
                <div className="grid gap-3">
                  {Array.from({ length: 3 }, (_, index) => (
                    <Skeleton key={index} className="h-10 w-full" />
                  ))}
                </div>
              ) : keysLoadState === "error" ? (
                <div className="rounded-lg border border-destructive/30 bg-destructive/10 p-8 text-center text-sm text-destructive">
                  Agent API Keys could not be loaded. Refresh to try again.
                </div>
              ) : keys.length === 0 ? (
                <div className="rounded-lg border border-dashed p-8 text-center text-sm text-muted-foreground">
                  No active keys for this project.
                </div>
              ) : (
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Name</TableHead>
                      <TableHead>Capability</TableHead>
                      <TableHead>Display</TableHead>
                      <TableHead>Created</TableHead>
                      <TableHead className="text-right">Action</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {keys.map((key) => (
                      <TableRow key={key.id}>
                        <TableCell>
                          <div className="font-medium">{key.name}</div>
                          <div className="text-xs text-muted-foreground">{key.agentScope}</div>
                        </TableCell>
                        <TableCell>
                          <Badge variant="secondary">{key.capability}</Badge>
                        </TableCell>
                        <TableCell>
                          <code>{key.display}</code>
                        </TableCell>
                        <TableCell className="text-muted-foreground">
                          {dateFormatter.format(new Date(key.createdAt))}
                        </TableCell>
                        <TableCell className="text-right">
                          <div className="flex justify-end gap-2">
                            <Button type="button" variant="outline" onClick={() => copyScope(key.scopeTemplate)}>
                              <Copy />
                              Scope
                            </Button>
                            <Button type="button" variant="destructive" onClick={() => revokeKey(key.id)}>
                              <Trash2 />
                              Revoke
                            </Button>
                          </div>
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              )}
            </CardContent>
          </Card>
        </TabsContent>

        <TabsContent value="usage">
          <Card>
            <CardHeader>
              <CardTitle>Usage & Caps</CardTitle>
              <CardDescription>
                {usage
                  ? `${dateFormatter.format(new Date(usage.periodStart))} to ${dateFormatter.format(new Date(usage.periodEnd))}`
                  : "Aggregate telemetry for the current period."}
              </CardDescription>
            </CardHeader>
            <CardContent className="grid gap-5 md:grid-cols-2">
              {usageLoadState === "loading" ? (
                <>
                  <Skeleton className="h-16 w-full" />
                  <Skeleton className="h-16 w-full" />
                </>
              ) : usageLoadState === "error" ? (
                <div className="rounded-lg border border-destructive/30 bg-destructive/10 p-8 text-center text-sm text-destructive md:col-span-2">
                  Usage could not be loaded. Refresh to try again.
                </div>
              ) : usageRowData.length > 0 ? (
                <>
                  <div className="grid gap-3 md:col-span-2">
                    <div>
                      <h3 className="text-sm font-medium">Metric totals</h3>
                      <p className="text-sm text-muted-foreground">Current-period activity by metric.</p>
                    </div>
                    <BarList
                      data={usageRowData.map((row) => ({
                        key: row.key,
                        name: row.label,
                        value: row.value,
                        valueLabel: row.valueLabel
                      }))}
                    />
                  </div>
                  {usageRowData.map((row) => (
                    <UsageMeter
                      key={row.key}
                      label={row.label}
                      max={row.max}
                      value={row.value}
                      valueLabel={row.valueLabel}
                    />
                  ))}
                </>
              ) : (
                <div className="rounded-lg border border-dashed p-8 text-center text-sm text-muted-foreground md:col-span-2">
                  No usage recorded for this period.
                </div>
              )}
            </CardContent>
          </Card>
        </TabsContent>

        <TabsContent value="settings">
          <Card>
            <CardHeader>
              <CardTitle>Project Settings</CardTitle>
              <CardDescription>Read-only project identity for the local control-plane slice.</CardDescription>
            </CardHeader>
            <CardContent className="grid gap-4">
              <div className="grid gap-1">
                <span className="text-sm text-muted-foreground">Project ID</span>
                <code>{projectId}</code>
              </div>
              <Separator />
              <div className="grid gap-1">
                <span className="text-sm text-muted-foreground">Tenant ID</span>
                {projectLoadState === "error" ? (
                  <span className="text-sm text-destructive">Project details could not be loaded.</span>
                ) : (
                  <code>{project?.tenantId ?? "loading"}</code>
                )}
              </div>
              <Separator />
              <div className="grid gap-1">
                <span className="text-sm text-muted-foreground">Environment</span>
                {projectLoadState === "error" ? (
                  <span className="text-sm text-destructive">Project details could not be loaded.</span>
                ) : (
                  <strong>{project?.environment ?? "production"}</strong>
                )}
              </div>
            </CardContent>
          </Card>
        </TabsContent>
      </Tabs>
    </div>
  );
}

function scopeJson(scope: ScopeTemplate) {
  return JSON.stringify(scope, null, 2);
}
