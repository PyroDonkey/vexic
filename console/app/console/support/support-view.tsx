"use client";

import { LifeBuoy } from "lucide-react";
import { useEffect, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
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

export default function SupportView() {
  const [records, setRecords] = useState<SupportRecord[]>([]);
  const [restricted, setRestricted] = useState(false);

  useEffect(() => {
    async function load() {
      const response = await fetch("/api/control-plane/support", { cache: "no-store" });
      if (response.status === 403) {
        setRestricted(true);
        return;
      }
      if (response.ok) {
        const data = (await response.json()) as { records: SupportRecord[] };
        setRecords(data.records);
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
            {records.length === 0 ? (
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
