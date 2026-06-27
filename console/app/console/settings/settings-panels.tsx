"use client";

import { Building2, UserRound } from "lucide-react";
import { OrganizationProfile, UserProfile } from "@clerk/nextjs";

import { Badge } from "@/components/ui/badge";
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
    </div>
  );
}
