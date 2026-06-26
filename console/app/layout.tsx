import type { Metadata } from "next";
import { ClerkProvider } from "@clerk/nextjs";

import "./globals.css";
import { isClerkConfigured } from "@/lib/clerk-config";

export const metadata: Metadata = {
  title: "Vexic Console",
  description: "Control plane for Vexic projects, agent keys, usage, and support."
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  const body = isClerkConfigured() ? <ClerkProvider>{children}</ClerkProvider> : children;

  return (
    <html lang="en">
      <body>{body}</body>
    </html>
  );
}
