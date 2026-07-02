import type { MetadataRoute } from "next";

const siteUrl = process.env.NEXT_PUBLIC_SITE_URL ?? "https://vexic.dev";

export default function sitemap(): MetadataRoute.Sitemap {
  return ["/", "/pricing", "/docs", "/blog"].map((path) => ({
    url: `${siteUrl}${path === "/" ? "" : path}`,
    changeFrequency: "weekly",
    priority: path === "/" ? 1 : 0.6
  }));
}
