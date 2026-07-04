"use client";

import { useEffect, useState } from "react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { jobRuns } from "@/lib/console-ui-state.mjs";

type JobEvent = {
  jobId: string;
  operation: string;
  phase: string | null;
  status: string;
  recordedAt: string;
};

type LoadState = "loading" | "ready" | "error";

const timeFormatter = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" });

function statusBadge(status: string) {
  if (status === "ok") return <Badge variant="secondary">Succeeded</Badge>;
  if (status === "running") return <Badge variant="outline">Running</Badge>;
  return <Badge variant="destructive">Failed</Badge>;
}

export default function JobsTab({ projectId }: { projectId: string }) {
  const [events, setEvents] = useState<JobEvent[]>([]);
  const [loadState, setLoadState] = useState<LoadState>("loading");

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        setLoadState("loading");
        const response = await fetch(`/api/control-plane/projects/${projectId}/jobs`, { cache: "no-store" });
        if (!response.ok) throw new Error(`Jobs load failed with ${response.status}`);
        const data = (await response.json()) as { jobs: JobEvent[] };
        if (cancelled) return;
        setEvents(data.jobs);
        setLoadState("ready");
      } catch {
        if (cancelled) return;
        setLoadState("error");
        toast.error("Background jobs failed to load.");
      }
    }
    load();
    return () => {
      cancelled = true;
    };
  }, [projectId]);

  const runs = jobRuns(events);

  return (
    <Card>
      <CardHeader>
        <CardTitle>Background jobs</CardTitle>
        <CardDescription>
          Vexic reviews recent conversations in the background and promotes durable facts. Recent runs appear
          here; runs from before project attribution was added are not shown.
        </CardDescription>
      </CardHeader>
      <CardContent>
        {loadState === "ready" && runs.length > 0 ? (
          <div className="mb-4 flex flex-wrap gap-4 text-sm text-muted-foreground">
            {["light", "rem", "deep", "summarize"].map((phase) => {
              const lastOk = runs.find((run) => run.phase === phase && run.status === "ok");
              return (
                <span key={phase}>
                  <span className="capitalize">{phase}</span> last succeeded:{" "}
                  {lastOk?.finishedAt ? timeFormatter.format(new Date(lastOk.finishedAt)) : "never"}
                </span>
              );
            })}
          </div>
        ) : null}
        {loadState === "loading" ? (
          <Skeleton className="h-32 w-full" />
        ) : loadState === "error" ? (
          <p className="text-sm text-muted-foreground">Jobs could not be loaded. Refresh to retry.</p>
        ) : runs.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            No background runs recorded for this project yet.
          </p>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Phase</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Started</TableHead>
                <TableHead>Finished</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {runs.map((run) => (
                <TableRow key={run.jobId}>
                  <TableCell className="capitalize">{run.phase ?? "—"}</TableCell>
                  <TableCell>
                    {statusBadge(run.status)}
                    {run.status === "error" ? (
                      <span className="ml-2 text-xs text-muted-foreground">
                        We&apos;re looking into it — contact support if this persists.
                      </span>
                    ) : null}
                  </TableCell>
                  <TableCell>{timeFormatter.format(new Date(run.startedAt))}</TableCell>
                  <TableCell>{run.finishedAt ? timeFormatter.format(new Date(run.finishedAt)) : "—"}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </CardContent>
    </Card>
  );
}
