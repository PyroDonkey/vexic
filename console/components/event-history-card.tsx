import { History } from "lucide-react";

import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";

export function EventHistoryCard({ description }: { description: string }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Event history</CardTitle>
        <CardDescription>{description}</CardDescription>
      </CardHeader>
      <CardContent>
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Actor</TableHead>
              <TableHead>Event</TableHead>
              <TableHead>Time</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            <TableRow>
              <TableCell colSpan={3}>
                <div className="flex items-center gap-3 rounded-lg border border-dashed p-8 text-center text-sm text-muted-foreground">
                  <History className="mx-auto size-5 shrink-0" />
                  <p className="text-left">Data-control events will be recorded here once auditing lands.</p>
                </div>
              </TableCell>
            </TableRow>
          </TableBody>
        </Table>
      </CardContent>
    </Card>
  );
}
