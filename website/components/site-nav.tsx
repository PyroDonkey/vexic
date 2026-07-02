"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useState } from "react";

import { CONSOLE_URL, GITHUB_URL, NAV_LINKS } from "@/lib/links";

export function SiteNav() {
  const [open, setOpen] = useState(false);
  const pathname = usePathname();
  // Pricing renders its own waitlist form; keep the CTA on-page there instead
  // of bouncing the user back to the homepage hero.
  const waitlistHref = pathname === "/pricing" ? "/pricing#waitlist" : "/#waitlist";

  return (
    <header className="sticky top-0 z-50 border-b border-border bg-background/80 backdrop-blur-md">
      <nav className="mx-auto flex h-16 w-full max-w-6xl items-center justify-between px-5">
        <Link href="/" aria-label="Vexic home" className="text-lg font-semibold tracking-tight">
          Vexic
        </Link>

        <div className="hidden items-center gap-9 text-muted-foreground sm:flex">
          {NAV_LINKS.map((link) => (
            <Link
              key={link.href}
              href={link.href}
              className="font-mono text-xs tracking-wide uppercase transition-colors hover:text-foreground"
            >
              {link.label}
            </Link>
          ))}
          <a
            href={GITHUB_URL}
            target="_blank"
            rel="noreferrer"
            className="font-mono text-xs tracking-wide uppercase transition-colors hover:text-foreground"
          >
            GitHub
          </a>
        </div>

        <div className="flex items-center gap-3">
          <a
            href={CONSOLE_URL}
            className="hidden text-sm text-muted-foreground transition-colors hover:text-foreground sm:block"
          >
            Sign in
          </a>
          <a
            href={waitlistHref}
            onClick={() => setOpen(false)}
            className="rounded-md bg-primary px-3.5 py-2 font-mono text-sm font-semibold text-primary-foreground transition-[filter,translate] hover:brightness-110 active:translate-y-px active:brightness-95"
          >
            Get early access
          </a>
          <button
            type="button"
            aria-expanded={open}
            aria-controls="mobile-nav"
            aria-label={open ? "Close menu" : "Open menu"}
            onClick={() => setOpen((value) => !value)}
            className="-mr-1 flex h-11 w-11 items-center justify-center rounded-md text-muted-foreground transition-colors hover:text-foreground sm:hidden"
          >
            <svg viewBox="0 0 20 20" className="h-5 w-5" fill="none" aria-hidden>
              {open ? (
                <path d="M5 5 L15 15 M15 5 L5 15" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
              ) : (
                <path d="M3 5.5 H17 M3 10 H17 M3 14.5 H17" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
              )}
            </svg>
          </button>
        </div>
      </nav>

      {/* Always mounted so aria-controls points at a real element when closed. */}
      <div id="mobile-nav" hidden={!open} className="border-t border-border bg-background sm:hidden">
          <div className="mx-auto flex w-full max-w-6xl flex-col px-5 py-2 text-sm">
            {NAV_LINKS.map((link) => (
              <Link
                key={link.href}
                href={link.href}
                onClick={() => setOpen(false)}
                className="flex min-h-11 items-center font-mono text-xs tracking-wide uppercase text-muted-foreground transition-colors hover:text-foreground"
              >
                {link.label}
              </Link>
            ))}
            <a
              href={GITHUB_URL}
              target="_blank"
              rel="noreferrer"
              onClick={() => setOpen(false)}
              className="flex min-h-11 items-center font-mono text-xs tracking-wide uppercase text-muted-foreground transition-colors hover:text-foreground"
            >
              GitHub
            </a>
            <a
              href={CONSOLE_URL}
              onClick={() => setOpen(false)}
              className="flex min-h-11 items-center text-muted-foreground transition-colors hover:text-foreground"
            >
              Sign in
            </a>
          </div>
        </div>
    </header>
  );
}
