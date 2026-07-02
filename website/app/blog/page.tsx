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
      lede="We're writing deep dives on agent memory, provenance, and replayable systems. Until they land, watch the GitHub repository for updates."
    />
  );
}
