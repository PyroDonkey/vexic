import Link from "next/link";
import { OrganizationSwitcher, UserButton } from "@clerk/nextjs";

import { readAuthContext } from "@/lib/auth";
import { isClerkConfigured } from "@/lib/clerk-config";

// Vexic Console is repo-local control-plane UI, not memory-core runtime under src/vexic.
export const dynamic = "force-dynamic";

export default async function ConsoleLayout({ children }: { children: React.ReactNode }) {
  if (!isClerkConfigured()) {
    return (
      <ConsoleChrome controls={<span className="muted">Clerk not configured</span>}>
        <section className="notice">
          <h1>Auth configuration required</h1>
          <p>Configure Clerk before creating projects or agent keys.</p>
        </section>
      </ConsoleChrome>
    );
  }

  const auth = await readAuthContext();

  return (
    <ConsoleChrome
      controls={
        <div className="clerk-controls">
          <OrganizationSwitcher hidePersonal />
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
    <div className="console-shell">
      <aside className="sidebar">
        <div className="brand">
          <img src="/vexic-logo-reversed.svg" alt="Vexic" />
          <span>Console</span>
        </div>
        <nav aria-label="Console">
          <Link href="/console">Projects</Link>
          <Link href="/console/settings">Settings</Link>
          <Link href="/console/support">Support</Link>
        </nav>
      </aside>
      <main className="console-main">
        <header className="topbar">{controls}</header>
        <div className="content">{children}</div>
      </main>
    </div>
  );
}

function ActiveOrgRequired() {
  return (
    <section className="modal">
      <div className="eyebrow">Customer Account</div>
      <h1>Select an organization</h1>
      <p className="muted">Projects and Agent API Keys require an active Clerk Organization.</p>
      <OrganizationSwitcher hidePersonal />
    </section>
  );
}
