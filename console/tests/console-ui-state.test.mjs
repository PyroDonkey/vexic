import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { dark } from "@clerk/themes";

import { clerkBaseThemeFor } from "../lib/clerk-theme.mjs";
import { projectCreateFailureMessage, usageMeterDisplay, usageRows } from "../lib/console-ui-state.mjs";

const root = fileURLToPath(new URL("..", import.meta.url));

test("project creation distinguishes missing organization from generic failures", () => {
  assert.equal(projectCreateFailureMessage(403), "Project creation requires an active organization.");
  assert.equal(projectCreateFailureMessage(500), "Project creation failed. Try again.");
});

test("usage rows include totals, caps, and readable labels", () => {
  assert.deepEqual(
    usageRows({
      totals: { retrievalEvents: 12, dreamRuns: 2 },
      caps: { retrievalEvents: 100 }
    }),
    [
      { key: "retrievalEvents", label: "Retrieval Events", value: 12, max: 100 },
      { key: "dreamRuns", label: "Dream Runs", value: 2, max: 0 }
    ]
  );
});

test("usage meter display avoids invalid aria values", () => {
  assert.deepEqual(usageMeterDisplay(2, 0), {
    hasCap: false,
    percentage: 0,
    valueLabel: "2 / No cap",
    statusLabel: "No cap",
    ariaNow: null,
    ariaText: "No cap"
  });

  assert.deepEqual(usageMeterDisplay(12, 10), {
    hasCap: true,
    percentage: 100,
    valueLabel: "12 / 10",
    statusLabel: "100.0% used",
    ariaNow: 10,
    ariaText: "12 of 10 (over cap)"
  });
});

test("Clerk base theme follows the resolved app theme", () => {
  assert.equal(clerkBaseThemeFor("dark"), dark);
  assert.equal(clerkBaseThemeFor("light"), undefined);
  assert.equal(clerkBaseThemeFor(undefined), undefined);
});

test("public Clerk theme APIs declare explicit types", () => {
  const clerkThemeSource = readFileSync(path.join(root, "lib/clerk-theme.mjs"), "utf8");
  const providerSource = readFileSync(path.join(root, "components/clerk-theme-provider.tsx"), "utf8");

  assert.match(clerkThemeSource, /@param \{string \| undefined\} resolvedTheme/);
  assert.match(clerkThemeSource, /@returns \{typeof dark \| undefined\}/);
  assert.match(providerSource, /export function ClerkThemeProvider\(\{ children \}: \{ children: ReactNode \}\): ReactNode/);
});

test("Clerk theme provider keeps children rendered while theme resolves", () => {
  const providerSource = readFileSync(path.join(root, "components/clerk-theme-provider.tsx"), "utf8");

  assert.doesNotMatch(providerSource, /return null;/);
  assert.match(providerSource, /<ClerkProvider[\s\S]*\{children\}[\s\S]*<\/ClerkProvider>/);
});
