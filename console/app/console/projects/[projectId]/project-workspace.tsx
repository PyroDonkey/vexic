"use client";

import { KeyRound, ShieldCheck, SlidersHorizontal, Trash2 } from "lucide-react";
import { useEffect, useState } from "react";

import { UsageMeter } from "@/components/tremor/usage-meter";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Separator } from "@/components/ui/separator";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";

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
  periodStart: string;
  periodEnd: string;
  totals: Record<string, number>;
  caps: Record<string, number>;
};

type Tab = "keys" | "usage" | "settings";

const dateFormatter = new Intl.DateTimeFormat(undefined, { dateStyle: "medium" });

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
    const trimmedName = name.trim();
    if (!trimmedName) return;

    const response = await fetch(`/api/control-plane/projects/${projectId}/keys`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ name: trimmedName, agentScope: agentScope.trim() || "shared" })
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
    <div className="grid gap-6">
      <header className="flex flex-col gap-3 md:flex-row md:items-end md:justify-between">
        <div className="grid gap-1">
          <Badge className="w-fit" variant="outline">
            Project workspace
          </Badge>
          <h1 className="text-2xl font-semibold text-foreground md:text-3xl">{project?.name ?? "Project"}</h1>
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
                  <code>{rawKey}</code>
                  <Button className="w-fit" type="button" variant="outline" onClick={() => setRawKey("")}>
                    Dismiss
                  </Button>
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
              {keys.length === 0 ? (
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
                          <Button type="button" variant="destructive" onClick={() => revokeKey(key.id)}>
                            <Trash2 />
                            Revoke
                          </Button>
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
              {usage
                ? Object.entries(usage.totals).map(([label, value]) => (
                    <UsageMeter key={label} label={formatLabel(label)} max={usage.caps[label] ?? 0} value={value} />
                  ))
                : null}
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
                <span className="text-sm text-muted-foreground">Environment</span>
                <strong>{project?.environment ?? "production"}</strong>
              </div>
            </CardContent>
          </Card>
        </TabsContent>
      </Tabs>
    </div>
  );
}

function formatLabel(value: string) {
  return value.replace(/([A-Z])/g, " $1").replace(/^./, (match) => match.toUpperCase());
}
