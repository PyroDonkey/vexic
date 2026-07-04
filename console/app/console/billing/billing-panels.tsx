import { Gauge, Mail } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";

export default function BillingPanels() {
  return (
    <div className="grid gap-6">
      <header className="grid gap-1">
        <Badge className="w-fit" variant="outline">
          Billing
        </Badge>
        <h1 className="text-2xl font-semibold text-foreground md:text-3xl">Plan and limits</h1>
        <p className="text-sm text-muted-foreground">
          Vexic is in alpha; plan tiers and self-serve billing are being built out.
        </p>
      </header>

      <Card>
        <CardHeader>
          <CardTitle>Current plan</CardTitle>
          <CardDescription>Set by the Vexic team while the service is in alpha.</CardDescription>
        </CardHeader>
        <CardContent className="flex items-center gap-3">
          <Badge className="w-fit font-mono" variant="secondary">
            Alpha
          </Badge>
          <p className="text-sm text-muted-foreground">
            Plan limits and upgrade options will appear here once billing tiers ship.
          </p>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Included limits</CardTitle>
          <CardDescription>Where this plan&apos;s quotas will render alongside current usage.</CardDescription>
        </CardHeader>
        <CardContent>
          <div className="flex items-center gap-3 rounded-lg border border-dashed p-8 text-center text-sm text-muted-foreground">
            <Gauge className="mx-auto size-5 shrink-0" />
            <p className="text-left">
              Limits-versus-usage meters (operations per day, projects, keys) land here once the plan model ships.
              Nothing is capped or displayed yet.
            </p>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Upgrade</CardTitle>
          <CardDescription>No self-serve plan changes during alpha.</CardDescription>
        </CardHeader>
        <CardContent>
          <Button disabled type="button" variant="outline">
            <Mail />
            Plan changes are handled by the Vexic team during alpha.
          </Button>
        </CardContent>
      </Card>
    </div>
  );
}
