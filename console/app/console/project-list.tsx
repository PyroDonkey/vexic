"use client";

import { FolderKanban, Plus, Rocket } from "lucide-react";
import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";

type Project = {
  id: string;
  name: string;
  environment: string;
  createdAt: string;
};

const dateFormatter = new Intl.DateTimeFormat(undefined, { dateStyle: "medium" });

export default function ProjectList() {
  const router = useRouter();
  const [projects, setProjects] = useState<Project[]>([]);
  const [name, setName] = useState("");
  const [status, setStatus] = useState("");

  async function loadProjects() {
    const response = await fetch("/api/control-plane/projects", { cache: "no-store" });
    if (response.ok) {
      const data = (await response.json()) as { projects: Project[] };
      setProjects(data.projects);
    }
  }

  useEffect(() => {
    void loadProjects();
  }, []);

  async function createProject() {
    const trimmedName = name.trim();
    if (!trimmedName) return;

    const response = await fetch("/api/control-plane/projects", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ name: trimmedName })
    });

    if (!response.ok) {
      setStatus("Project creation requires an active organization.");
      return;
    }

    const data = (await response.json()) as { project: Project };
    setStatus("");
    setName("");
    setProjects((current) => [data.project, ...current.filter((project) => project.id !== data.project.id)]);
    router.push(`/console/projects/${data.project.id}`);
  }

  return (
    <div className="grid gap-6">
      <header className="flex flex-col gap-3 md:flex-row md:items-end md:justify-between">
        <div className="grid gap-1">
          <Badge className="w-fit" variant="outline">
            Projects
          </Badge>
          <h1 className="text-2xl font-semibold text-foreground md:text-3xl">Project list</h1>
          <p className="text-sm text-muted-foreground">
            Vexic-owned control-plane records under the active organization.
          </p>
        </div>
      </header>

      <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_22rem]">
        <Card>
          <CardHeader>
            <CardTitle>Create project</CardTitle>
            <CardDescription>Add a project to the active Clerk Organization.</CardDescription>
          </CardHeader>
          <CardContent>
            <form
              className="flex flex-col gap-3 sm:flex-row"
              onSubmit={(event) => {
                event.preventDefault();
                void createProject();
              }}
            >
              <Input
                aria-label="Project name"
                value={name}
                onChange={(event) => setName(event.target.value)}
                placeholder="Project name"
              />
              <Button disabled={!name.trim()} type="submit">
                <Plus />
                Create
              </Button>
            </form>
            {status ? <p className="mt-3 text-sm text-muted-foreground">{status}</p> : null}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <Rocket className="size-4 text-primary" />
              {projects.length === 0 ? "No projects yet" : `${projects.length} project${projects.length === 1 ? "" : "s"}`}
            </CardTitle>
            <CardDescription>Open a workspace to manage keys, usage, and settings.</CardDescription>
          </CardHeader>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <FolderKanban className="size-4 text-primary" />
            Workspaces
          </CardTitle>
          <CardDescription>Project environments and creation dates.</CardDescription>
        </CardHeader>
        <CardContent>
          {projects.length === 0 ? (
            <div className="rounded-lg border border-dashed p-8 text-center text-sm text-muted-foreground">
              Create a project to start managing Agent API Keys.
            </div>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Name</TableHead>
                  <TableHead>Environment</TableHead>
                  <TableHead>Created</TableHead>
                  <TableHead className="text-right">Action</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {projects.map((project) => (
                  <TableRow key={project.id}>
                    <TableCell className="font-medium">{project.name}</TableCell>
                    <TableCell>
                      <Badge variant="secondary">{project.environment}</Badge>
                    </TableCell>
                    <TableCell className="text-muted-foreground">
                      {dateFormatter.format(new Date(project.createdAt))}
                    </TableCell>
                    <TableCell className="text-right">
                      <Button type="button" variant="outline" onClick={() => router.push(`/console/projects/${project.id}`)}>
                        Open
                      </Button>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
