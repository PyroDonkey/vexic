import Link from "next/link";
import { CreditCard, FolderKanban, LifeBuoy, Menu, Settings } from "lucide-react";
import { CreateOrganization, OrganizationSwitcher, UserButton } from "@clerk/nextjs";

import { ThemeToggle } from "@/components/theme-toggle";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Separator } from "@/components/ui/separator";
import { Sheet, SheetClose, SheetContent, SheetHeader, SheetTitle, SheetTrigger } from "@/components/ui/sheet";
import { readAuthContext } from "@/lib/auth";
import { isClerkConfigured } from "@/lib/clerk-config";
import { activeOrganizationCreateProps } from "@/lib/console-routes.mjs";

// Vexic Console is repo-local control-plane UI, not memory-core runtime under src/vexic.
export const dynamic = "force-dynamic";

const navItems = [
  { href: "/console", label: "Projects", icon: FolderKanban },
  { href: "/console/billing", label: "Billing", icon: CreditCard },
  { href: "/console/settings", label: "Settings", icon: Settings },
  { href: "/console/support", label: "Support", icon: LifeBuoy }
];

export default async function ConsoleLayout({ children }: { children: React.ReactNode }) {
  if (!isClerkConfigured()) {
    return (
      <ConsoleChrome controls={<Badge variant="outline">Clerk not configured</Badge>}>
        <Card className="max-w-2xl">
          <CardHeader>
            <CardTitle>Auth configuration required</CardTitle>
            <CardDescription>Configure Clerk before creating projects or agent keys.</CardDescription>
          </CardHeader>
        </Card>
      </ConsoleChrome>
    );
  }

  const auth = await readAuthContext();

  return (
    <ConsoleChrome
      controls={
        <div className="flex items-center gap-3">
          <OrganizationSwitcher hidePersonal />
          <ThemeToggle />
          <UserButton />
        </div>
      }
    >
      {auth.orgId ? children : <ActiveOrgRequired />}
    </ConsoleChrome>
  );
}

function ConsoleChrome({ children, controls }: { children: React.ReactNode; controls: React.ReactNode }) {
  return (
    <div className="min-h-screen bg-background text-foreground lg:grid lg:grid-cols-[16rem_minmax(0,1fr)]">
      <aside className="hidden border-sidebar-border bg-sidebar text-sidebar-foreground lg:block lg:min-h-screen lg:border-r">
        <SidebarNav />
      </aside>
      <main className="min-w-0">
        <header className="sticky top-0 z-10 flex min-h-14 items-center justify-between border-b bg-background/90 px-4 backdrop-blur md:px-6 lg:justify-end">
          <div className="flex items-center gap-2 lg:hidden">
            <MobileNav />
            <span className="text-sm font-semibold">Console</span>
          </div>
          {controls}
        </header>
        <div className="mx-auto w-full max-w-6xl px-4 py-6 md:px-6 lg:py-8">{children}</div>
      </main>
    </div>
  );
}

function SidebarNav() {
  return (
    <div className="flex h-full flex-col gap-5 px-4 py-4">
      <Link href="/console" className="flex items-center gap-3 rounded-lg px-2 py-1.5">
        <img src="/vexic-logo-reversed.svg" alt="Vexic" className="h-8 w-auto" />
        <span className="text-xs font-semibold uppercase text-sidebar-primary">Console</span>
      </Link>
      <Separator className="bg-sidebar-border" />
      <NavLinks />
    </div>
  );
}

function MobileNav() {
  return (
    <Sheet>
      <SheetTrigger asChild>
        <Button aria-label="Open navigation" size="icon" type="button" variant="ghost">
          <Menu />
        </Button>
      </SheetTrigger>
      <SheetContent side="left">
        <SheetHeader>
          <SheetTitle>
            <Link href="/console" className="flex items-center gap-3">
              <img src="/vexic-logo-reversed.svg" alt="Vexic" className="h-8 w-auto" />
              <span className="text-xs font-semibold uppercase text-sidebar-primary">Console</span>
            </Link>
          </SheetTitle>
        </SheetHeader>
        <Separator className="bg-sidebar-border" />
        <NavLinks closeOnClick />
      </SheetContent>
    </Sheet>
  );
}

function NavLinks({ closeOnClick = false }: { closeOnClick?: boolean }) {
  return (
    <nav aria-label="Console" className="grid gap-1">
      {navItems.map((item) => {
        const link = (
          <Link
            key={item.href}
            href={item.href}
            className="flex h-9 items-center gap-2 rounded-lg px-2.5 text-sm text-sidebar-foreground/80 transition hover:bg-sidebar-accent hover:text-sidebar-accent-foreground"
          >
            <item.icon className="size-4" />
            {item.label}
          </Link>
        );

        return closeOnClick ? (
          <SheetClose key={item.href} asChild>
            {link}
          </SheetClose>
        ) : (
          link
        );
      })}
    </nav>
  );
}

function ActiveOrgRequired() {
  return (
    <Card className="max-w-2xl">
      <CardHeader>
        <Badge className="w-fit" variant="outline">
          Customer Account
        </Badge>
        <CardTitle>Create an organization</CardTitle>
        <CardDescription>Projects and Agent API Keys require an active Clerk Organization.</CardDescription>
      </CardHeader>
      <CardContent>
        <CreateOrganization {...activeOrganizationCreateProps} />
      </CardContent>
    </Card>
  );
}
