import Link from "next/link";

import { Section } from "@/components/section";
import { GITHUB_URL } from "@/lib/links";

export function ComingSoon({ title, lede }: { title: string; lede: string }) {
  return (
    <Section title={title} lede={lede} headingLevel="h1" className="min-h-[60vh]">
      <div className="flex flex-col items-center gap-4">
        <a
          href={GITHUB_URL}
          target="_blank"
          rel="noreferrer"
          className="rounded-md bg-primary px-4 py-2.5 font-mono text-sm font-semibold text-primary-foreground transition-[filter,translate] hover:brightness-110 active:translate-y-px active:brightness-95"
        >
          Read the source on GitHub
        </a>
        <Link href="/" className="text-sm text-muted-foreground transition-colors hover:text-foreground">
          ← Back home
        </Link>
      </div>
    </Section>
  );
}
