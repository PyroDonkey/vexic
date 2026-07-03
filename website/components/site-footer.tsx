import Link from "next/link";

import { AmbientCanvas } from "@/components/ambient-canvas";
import { CONSOLE_URL, GITHUB_URL, NAV_LINKS } from "@/lib/links";

export function SiteFooter() {
  return (
    <footer className="relative overflow-hidden border-t border-border">
      {/* Quiet texture: bone-white dots read a step lighter than the canvas,
          fading out toward the top edge so the page bookends. */}
      <AmbientCanvas
        color="var(--foreground)"
        maxOpacity={0.15}
        speed={0.6}
        density={0.75}
        fadeDirection="to-top"
      />
      <div className="relative mx-auto flex w-full max-w-6xl flex-col gap-8 px-5 py-12 sm:flex-row sm:items-start sm:justify-between">
        <div className="max-w-xs space-y-3">
          <p className="text-lg font-semibold tracking-tight">Vexic</p>
          <p className="text-sm text-muted-foreground">
            A persistent memory layer for AI agents.
          </p>
        </div>

        <div className="flex gap-16 text-sm">
          <div className="space-y-3">
            <p className="font-semibold">Product</p>
            <ul className="space-y-2 text-muted-foreground">
              {NAV_LINKS.map((link) => (
                <li key={link.href}>
                  <Link href={link.href} className="transition-colors hover:text-foreground">
                    {link.label}
                  </Link>
                </li>
              ))}
            </ul>
          </div>
          <div className="space-y-3">
            <p className="font-semibold">Resources</p>
            <ul className="space-y-2 text-muted-foreground">
              <li>
                <a
                  href={GITHUB_URL}
                  target="_blank"
                  rel="noreferrer"
                  className="transition-colors hover:text-foreground"
                >
                  GitHub
                </a>
              </li>
              <li>
                <a href={CONSOLE_URL} className="transition-colors hover:text-foreground">
                  Sign in
                </a>
              </li>
            </ul>
          </div>
        </div>
      </div>
      <div className="relative mx-auto w-full max-w-6xl px-5 pb-8 text-xs text-muted-foreground">
        <p>© {new Date().getFullYear()} Vexic</p>
      </div>
    </footer>
  );
}
