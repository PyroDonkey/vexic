"use client";

import { Building2, ShieldAlert, UserRound } from "lucide-react";
import { OrganizationProfile, UserProfile } from "@clerk/nextjs";

import { EventHistoryCard } from "@/components/event-history-card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";

export default function SettingsPanels() {
  return (
    <div className="grid gap-6">
      <header className="grid gap-1">
        <Badge className="w-fit" variant="outline">
          Settings
        </Badge>
        <h1 className="text-2xl font-semibold text-foreground md:text-3xl">Account and organization</h1>
        <p className="text-sm text-muted-foreground">Clerk owns human identity and organization membership.</p>
      </header>

      <Tabs defaultValue="user">
        <TabsList>
          <TabsTrigger value="user">
            <UserRound />
            User
          </TabsTrigger>
          <TabsTrigger value="organization">
            <Building2 />
            Organization
          </TabsTrigger>
        </TabsList>
        <TabsContent value="user">
          <UserProfile routing="hash" />
        </TabsContent>
        <TabsContent value="organization">
          <OrganizationProfile routing="hash" />
        </TabsContent>
      </Tabs>

      <EventHistoryCard description="Data-control and key events across every project in this organization." />

      <Card className="ring-destructive/20">
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-destructive">
            <ShieldAlert className="size-4" />
            Danger Zone
          </CardTitle>
          <CardDescription>
            Deleting organization data will require typing the organization name plus a separate
            &quot;I understand this is irreversible&quot; confirmation before it runs. Purge removes data from the
            live database immediately; point-in-time-recovery history and operator backups age out on their own
            retention schedule afterward. This confirmation flow is coming soon.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <Button disabled type="button" variant="destructive">
            Delete organization data
          </Button>
        </CardContent>
      </Card>
    </div>
  );
}
