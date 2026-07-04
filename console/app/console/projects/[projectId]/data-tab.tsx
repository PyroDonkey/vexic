import { Download, Trash2 } from "lucide-react";

import { EventHistoryCard } from "@/components/event-history-card";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";

const retentionPresets = ["30 days", "90 days", "365 days", "Forever"];

export default function DataTab() {
  return (
    <div className="grid gap-4">
      <Card>
        <CardHeader>
          <CardTitle>Retention</CardTitle>
          <CardDescription>
            Transcript retention is set to <strong>Forever</strong> by default. Configurable retention windows and
            the purge confirmation flow are coming soon.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <div className="flex flex-wrap gap-2">
            {retentionPresets.map((preset) => (
              <Button
                key={preset}
                aria-pressed={preset === "Forever"}
                className="font-mono"
                disabled
                type="button"
                variant={preset === "Forever" ? "secondary" : "outline"}
              >
                {preset}
              </Button>
            ))}
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Export</CardTitle>
          <CardDescription>
            Request an archive of this project&apos;s facts, transcripts, and metadata. Once export jobs ship,
            completed requests will expose a time-limited signed download link here.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <Button disabled type="button" variant="outline">
            <Download />
            Request export
          </Button>
        </CardContent>
      </Card>

      <Card className="ring-destructive/20">
        <CardHeader>
          <CardTitle className="text-destructive">Delete project memory</CardTitle>
          <CardDescription>
            Permanently erases this project&apos;s facts and transcripts. Irreversible once the purge runs; a
            type-the-project-name confirmation step is coming to gate this action.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <Button disabled type="button" variant="destructive">
            <Trash2 />
            Delete project memory
          </Button>
        </CardContent>
      </Card>

      <EventHistoryCard description="Data-control and key events for this project." />
    </div>
  );
}
