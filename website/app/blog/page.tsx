import type { Metadata } from "next";

import { ComingSoon } from "@/components/coming-soon";

export const metadata: Metadata = {
  title: "Blog",
  description: "The Vexic blog is coming soon."
};

export default function BlogPage() {
  return (
    <ComingSoon
      title="The blog is coming soon"
      lede="Deep dives on agent memory, provenance, and replayable systems are in the works. Watch the GitHub repository for updates in the meantime."
    />
  );
}
