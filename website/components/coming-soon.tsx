import Link from "next/link";

import { Section } from "@/components/section";
import { GITHUB_URL } from "@/lib/links";

export function ComingSoon({ eyebrow, title, lede }: { eyebrow: string; title: string; lede: string }) {
  return (
    <Section eyebrow={eyebrow} title={title} lede={lede} className="min-h-[60vh]">
      <div className="flex flex-col items-center gap-4">
        <a
          href={GITHUB_URL}
          target="_blank"
          rel="noreferrer"
          className="rounded-md bg-primary px-4 py-2.5 text-sm font-semibold text-primary-foreground transition-opacity hover:opacity-90"
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
