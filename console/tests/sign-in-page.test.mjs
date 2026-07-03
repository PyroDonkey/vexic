import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const root = fileURLToPath(new URL("..", import.meta.url));
const pageSource = () =>
  readFileSync(path.join(root, "app/sign-in/[[...sign-in]]/page.tsx"), "utf8");
const cssSource = () => readFileSync(path.join(root, "app/globals.css"), "utf8");

test("sign-in appearance hides the Clerk sign-up footer action", () => {
  assert.match(pageSource(), /footerAction:\s*\{\s*display:\s*"none"\s*\}/);
});

test("sign-in page links to the marketing waitlist for access requests", () => {
  const source = pageSource();
  assert.match(
    source,
    /const SITE_URL = process\.env\.NEXT_PUBLIC_SITE_URL \?\? "https:\/\/vexic\.dev";/
  );
  assert.match(source, /\$\{SITE_URL\}\/#waitlist/);
  assert.match(source, /Get notified when access opens/);
});

test("auth notify styles use the literal marketing palette", () => {
  const css = cssSource();
  assert.match(css, /\.auth-notify\s*\{[^}]*color:\s*#9aa89e/s);
  assert.match(css, /\.auth-notify-link\s*\{[^}]*color:\s*#e5e2e1/s);
});

test("sign-in page renders the ambient canvas behind a layered content wrapper", () => {
  const source = pageSource();
  assert.match(source, /import \{ AmbientCanvas \} from "@\/components\/ambient-canvas";/);
  assert.match(source, /<AmbientCanvas[^>]*color="#10b981"/s);
  assert.match(source, /fadeDirection="to-bottom"/);
  assert.match(source, /className="auth-content"/);
});

test("auth page positions content above the canvas", () => {
  const css = cssSource();
  assert.match(css, /\.auth-page\s*\{[^}]*position:\s*relative/s);
  assert.match(css, /\.auth-page\s*\{[^}]*overflow:\s*hidden/s);
  assert.match(css, /\.auth-content\s*\{[^}]*z-index:\s*1/s);
});
