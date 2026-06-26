"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";

type Project = {
  id: string;
  name: string;
  environment: string;
  createdAt: string;
};

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
    const response = await fetch("/api/control-plane/projects", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ name })
    });

    if (!response.ok) {
      setStatus("Project creation requires an active organization.");
      return;
    }

    const data = (await response.json()) as { project: Project };
    setName("");
    setProjects((current) => [data.project, ...current.filter((project) => project.id !== data.project.id)]);
    router.push(`/console/projects/${data.project.id}`);
  }

  return (
    <>
      <header className="page-title">
        <div>
          <div className="eyebrow">Projects</div>
          <h1>Project list</h1>
          <p className="muted">Vexic-owned control-plane records under the active organization.</p>
        </div>
      </header>

      <section className="grid">
        <div className="panel">
          <h2>Create project</h2>
          <div className="form-row">
            <input value={name} onChange={(event) => setName(event.target.value)} placeholder="Project name" />
            <button className="button" type="button" onClick={createProject}>
              Create
            </button>
          </div>
          {status ? <p className="muted">{status}</p> : null}
        </div>
        <div className="empty-state">
          <h2>{projects.length === 0 ? "No projects yet" : `${projects.length} project${projects.length === 1 ? "" : "s"}`}</h2>
          <p>Open a project workspace to manage agent keys, usage, and settings.</p>
        </div>
      </section>

      <section className="panel" style={{ marginTop: 16 }}>
        {projects.map((project) => (
          <div className="project-row" key={project.id}>
            <div>
              <strong>{project.name}</strong>
              <div className="muted">{project.environment}</div>
            </div>
            <button className="button secondary" type="button" onClick={() => router.push(`/console/projects/${project.id}`)}>
              Open
            </button>
          </div>
        ))}
      </section>
    </>
  );
}
