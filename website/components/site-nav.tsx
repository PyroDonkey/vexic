import Image from "next/image";
import Link from "next/link";

import { CONSOLE_URL, GITHUB_URL, NAV_LINKS } from "@/lib/links";

export function SiteNav() {
  return (
    <header className="sticky top-0 z-50 border-b border-border bg-background/80 backdrop-blur-md">
      <nav className="mx-auto flex h-16 w-full max-w-6xl items-center justify-between px-5">
        <Link href="/" aria-label="Vexic home" className="flex items-center gap-2">
          <Image
            src="/vexic-logo-reversed.svg"
            alt="Vexic"
            width={96}
            height={28}
            priority
            className="h-7 w-auto"
          />
        </Link>

        <div className="hidden items-center gap-6 text-sm text-muted-foreground sm:flex">
          {NAV_LINKS.map((link) => (
            <Link
              key={link.href}
              href={link.href}
              className="transition-colors hover:text-foreground"
            >
              {link.label}
            </Link>
          ))}
          <a
            href={GITHUB_URL}
            target="_blank"
            rel="noreferrer"
            className="transition-colors hover:text-foreground"
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
            href="#waitlist"
            className="rounded-md bg-primary px-3.5 py-2 text-sm font-semibold text-primary-foreground transition-opacity hover:opacity-90"
          >
            Get early access
          </a>
        </div>
      </nav>
    </header>
  );
}
