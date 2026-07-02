export const GITHUB_URL = "https://github.com/PyroDonkey/vexic";

export const CONSOLE_URL =
  process.env.NEXT_PUBLIC_CONSOLE_URL ?? "https://console.vexic.dev";

export const NAV_LINKS = [
  { href: "/docs", label: "Docs" },
  { href: "/pricing", label: "Pricing" },
  { href: "/blog", label: "Blog" }
] as const;
