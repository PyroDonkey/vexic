import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { test } from "node:test";
import { fileURLToPath } from "node:url";

const appDir = join(dirname(fileURLToPath(import.meta.url)), "..", "app");

const EXPECTED_ROUTES = [
  "page.tsx",
  "layout.tsx",
  "not-found.tsx",
  "robots.ts",
  "sitemap.ts",
  "pricing/page.tsx",
  "docs/page.tsx",
  "blog/page.tsx",
  "api/waitlist/route.ts"
];

test("expected route files exist", () => {
  for (const route of EXPECTED_ROUTES) {
    assert.ok(existsSync(join(appDir, route)), `missing app/${route}`);
  }
});

test("brand assets are present", () => {
  const publicDir = join(appDir, "..", "public");
  for (const asset of ["favicon.svg", "vexic-logo-reversed.svg"]) {
    assert.ok(existsSync(join(publicDir, asset)), `missing public/${asset}`);
  }
});

test("sign-in links target the console sign-in route", () => {
  const root = join(dirname(fileURLToPath(import.meta.url)), "..");
  const links = readFileSync(join(root, "lib/links.ts"), "utf8");
  assert.match(links, /export const SIGN_IN_URL = `\$\{CONSOLE_URL\}\/sign-in`;/);

  for (const file of ["components/site-nav.tsx", "components/site-footer.tsx"]) {
    const source = readFileSync(join(root, file), "utf8");
    assert.ok(source.includes("SIGN_IN_URL"), `${file} should link via SIGN_IN_URL`);
    assert.ok(
      !source.match(/href=\{CONSOLE_URL\}/),
      `${file} should not link to the console root`
    );
  }
});
