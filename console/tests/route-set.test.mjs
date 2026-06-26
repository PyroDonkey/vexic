import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const root = fileURLToPath(new URL("..", import.meta.url));

const routes = [
  ["public home", "app/page.tsx"],
  ["sign in", "app/sign-in/[[...sign-in]]/page.tsx"],
  ["sign up", "app/sign-up/[[...sign-up]]/page.tsx"],
  ["project list", "app/console/page.tsx"],
  ["project workspace", "app/console/projects/[projectId]/page.tsx"],
  ["settings", "app/console/settings/page.tsx"],
  ["support", "app/console/support/page.tsx"]
];

test("COA-230 route set exists", () => {
  for (const [name, file] of routes) {
    assert.ok(existsSync(path.join(root, file)), `${name} route missing: ${file}`);
  }
});
