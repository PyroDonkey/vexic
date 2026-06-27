"use client";

import { LifeBuoy } from "lucide-react";
import { useEffect, useState } from "react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";

type SupportRecord = {
  ticketId: string;
  orgId: string;
  projectIds: string[];
  status: string;
  createdAt: string;
  updatedAt: string;
};

const dateFormatter = new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" });
type LoadState = "loading" | "ready" | "error";

export default function SupportView() {
  const [records, setRecords] = useState<SupportRecord[]>([]);
  const [restricted, setRestricted] = useState(false);
  const [loadState, setLoadState] = useState<LoadState>("loading");

  useEffect(() => {
    async function load() {
      try {
        setLoadState("loading");
        const response = await fetch("/api/control-plane/support", { cache: "no-store" });
        if (response.status === 403) {
          setRestricted(true);
          setLoadState("ready");
          return;
        }
        if (!response.ok) throw new Error(`Support load failed with ${response.status}`);
        const data = (await response.json()) as { records: SupportRecord[] };
        setRecords(data.records);
        setLoadState("ready");
      } catch {
        setLoadState("error");
        toast.error("Support records failed to load.");
      }
    }
    void load();
  }, []);

  return (
    <div className="grid gap-6">
      <header className="grid gap-1">
        <Badge className="w-fit" variant="outline">
          Internal support
        </Badge>
        <h1 className="text-2xl font-semibold text-foreground md:text-3xl">Support metadata</h1>
        <p className="text-sm text-muted-foreground">Ticket, organization, project, and timestamp metadata only.</p>
      </header>

      {restricted ? (
        <Card>
          <CardHeader>
            <CardTitle>Restricted</CardTitle>
            <CardDescription>Vexic-internal support access is required.</CardDescription>
          </CardHeader>
        </Card>
      ) : (
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <LifeBuoy className="size-4 text-primary" />
              Support records
            </CardTitle>
            <CardDescription>No transcript, fact, search, or raw memory fields are exposed.</CardDescription>
          </CardHeader>
          <CardContent>
            {loadState === "loading" ? (
              <div className="grid gap-3">
                {Array.from({ length: 3 }, (_, index) => (
                  <Skeleton key={index} className="h-10 w-full" />
                ))}
              </div>
            ) : loadState === "error" ? (
              <div className="rounded-lg border border-destructive/30 bg-destructive/10 p-8 text-center text-sm text-destructive">
                Support records could not be loaded. Refresh to try again.
              </div>
            ) : records.length === 0 ? (
              <div className="rounded-lg border border-dashed p-8 text-center text-sm text-muted-foreground">
                No support records available.
              </div>
            ) : (
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Ticket</TableHead>
                    <TableHead>Organization</TableHead>
                    <TableHead>Projects</TableHead>
                    <TableHead>Status</TableHead>
                    <TableHead>Updated</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {records.map((record) => (
                    <TableRow key={record.ticketId}>
                      <TableCell className="font-medium">{record.ticketId}</TableCell>
                      <TableCell>
                        <code>{record.orgId}</code>
                      </TableCell>
                      <TableCell>{record.projectIds.length}</TableCell>
                      <TableCell>
                        <Badge variant="secondary">{record.status}</Badge>
                      </TableCell>
                      <TableCell className="text-muted-foreground">
                        {dateFormatter.format(new Date(record.updatedAt))}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
          </CardContent>
        </Card>
      )}
    </div>
  );
}
