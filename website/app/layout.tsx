import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";

import "./globals.css";
import { SiteFooter } from "@/components/site-footer";
import { SiteNav } from "@/components/site-nav";

const geistSans = Geist({
  subsets: ["latin"],
  variable: "--font-geist-sans"
});

const geistMono = Geist_Mono({
  subsets: ["latin"],
  variable: "--font-geist-mono"
});

const siteUrl = process.env.NEXT_PUBLIC_SITE_URL ?? "https://vexic.dev";

export const metadata: Metadata = {
  metadataBase: new URL(siteUrl),
  title: {
    default: "Vexic — Memory your agents can trust",
    template: "%s — Vexic"
  },
  description:
    "Provenance-first, replayable memory engine for long-running AI agents. Lossless transcripts, staged extraction, and durable facts that carry their receipts.",
  icons: {
    icon: "/favicon.svg"
  },
  openGraph: {
    title: "Vexic — Memory your agents can trust",
    description:
      "Provenance-first, replayable memory engine for long-running AI agents.",
    url: siteUrl,
    siteName: "Vexic",
    type: "website"
  },
  twitter: {
    card: "summary_large_image",
    title: "Vexic — Memory your agents can trust",
    description:
      "Provenance-first, replayable memory engine for long-running AI agents."
  }
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={`${geistSans.variable} ${geistMono.variable}`}>
      <body className="flex min-h-screen flex-col">
        <SiteNav />
        <main className="flex-1">{children}</main>
        <SiteFooter />
      </body>
    </html>
  );
}
