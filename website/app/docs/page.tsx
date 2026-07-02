import type { Metadata } from "next";

import { ComingSoon } from "@/components/coming-soon";

export const metadata: Metadata = {
  title: "Docs",
  description: "Vexic documentation is coming soon."
};

export default function DocsPage() {
  return (
    <ComingSoon
      eyebrow="Docs"
      title="Documentation is coming soon"
      lede="Full guides for the memory core, MCP integrations, and the hosted API are on the way. Until then, the repository README and architecture docs are the best reference."
    />
  );
}
