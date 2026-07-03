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
    default: "Vexic · Agent memory you can trust",
    template: "%s · Vexic"
  },
  description:
    "Hosted, provenance-first memory for long-running AI agents. Lossless transcripts, staged extraction, and durable facts behind one endpoint, each one traceable to the messages it came from.",
  icons: {
    icon: "/favicon.svg"
  },
  openGraph: {
    title: "Vexic · Agent memory you can trust",
    description:
      "Hosted, provenance-first memory engine for long-running AI agents.",
    url: siteUrl,
    siteName: "Vexic",
    type: "website"
  },
  twitter: {
    card: "summary_large_image",
    title: "Vexic · Agent memory you can trust",
    description:
      "Hosted, provenance-first memory engine for long-running AI agents."
  }
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={`${geistSans.variable} ${geistMono.variable}`}>
      <body className="flex min-h-screen flex-col">
        <a
          href="#main"
          className="sr-only z-[60] rounded-md bg-primary px-3 py-2 text-sm font-semibold text-primary-foreground focus:not-sr-only focus:absolute focus:top-2 focus:left-2"
        >
          Skip to content
        </a>
        <SiteNav />
        <main id="main" className="flex-1">
          {children}
        </main>
        <SiteFooter />
      </body>
    </html>
  );
}
