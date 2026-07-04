import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { dark } from "@clerk/themes";

import { clerkBaseThemeFor } from "../lib/clerk-theme.mjs";
import {
  capStatus,
  jobRuns,
  keyFreshness,
  projectCreateFailureMessage,
  usageMeterDisplay,
  usageRows
} from "../lib/console-ui-state.mjs";

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

test("usage rows format cost values as dollars", () => {
  assert.deepEqual(usageRows({ totals: { cost: 0.123456 }, caps: {} }), [
    { key: "cost", label: "Cost", value: 0.123456, max: 0, valueLabel: "$0.12" }
  ]);
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

  assert.deepEqual(usageMeterDisplay(0.123456, 0, "$0.12"), {
    hasCap: false,
    percentage: 0,
    valueLabel: "$0.12 / No cap",
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

test("keyFreshness labels never-used keys", () => {
  assert.deepEqual(keyFreshness(null, "2026-07-03T00:00:00Z"), {
    label: "Never used",
    stale: false
  });
});

test("keyFreshness flags keys unused for 30+ days as stale", () => {
  const fresh = keyFreshness("2026-06-20T00:00:00Z", "2026-07-03T00:00:00Z");
  assert.equal(fresh.stale, false);

  const stale = keyFreshness("2026-05-01T00:00:00Z", "2026-07-03T00:00:00Z");
  assert.equal(stale.stale, true);
  assert.match(stale.label, /May 1|2026/);

  const boundary = keyFreshness("2026-06-03T00:00:00Z", "2026-07-03T00:00:00Z");
  assert.equal(boundary.stale, true);
});

test("capStatus thresholds: ok below 80, warn at 80, alert at 95, none without cap", () => {
  assert.equal(capStatus(50, 100).level, "ok");
  assert.equal(capStatus(80, 100).level, "warn");
  assert.equal(capStatus(95, 100).level, "alert");
  assert.equal(capStatus(120, 100).level, "alert");
  assert.equal(capStatus(50, 0).level, "none");
});

test("jobRuns groups events per job with latest status and time range", () => {
  const runs = jobRuns([
    { jobId: "job2", phase: "rem", status: "error", recordedAt: "2026-07-02T01:05:00Z" },
    { jobId: "job2", phase: "rem", status: "running", recordedAt: "2026-07-02T01:00:00Z" },
    { jobId: "job1", phase: "light", status: "ok", recordedAt: "2026-07-01T00:05:00Z" },
    { jobId: "job1", phase: "light", status: "running", recordedAt: "2026-07-01T00:00:00Z" }
  ]);

  assert.equal(runs.length, 2);
  assert.deepEqual(runs[0], {
    jobId: "job2",
    phase: "rem",
    status: "error",
    startedAt: "2026-07-02T01:00:00Z",
    finishedAt: "2026-07-02T01:05:00Z"
  });
  assert.equal(runs[1].status, "ok");
});

test("jobRuns leaves running jobs without finishedAt", () => {
  const runs = jobRuns([
    { jobId: "job3", phase: "deep", status: "running", recordedAt: "2026-07-02T02:00:00Z" }
  ]);

  assert.equal(runs[0].status, "running");
  assert.equal(runs[0].finishedAt, null);
});

test("jobRuns groups a summarize phase run like any other dream phase", () => {
  const runs = jobRuns([
    { jobId: "job4", phase: "summarize", status: "ok", recordedAt: "2026-07-02T03:05:00Z" },
    { jobId: "job4", phase: "summarize", status: "running", recordedAt: "2026-07-02T03:00:00Z" }
  ]);

  assert.equal(runs[0].phase, "summarize");
  assert.equal(runs[0].status, "ok");
});

test("jobs tab renders a last-succeeded row for the summarize phase", () => {
  const jobsTabSource = readFileSync(
    path.join(root, "app/console/projects/[projectId]/jobs-tab.tsx"),
    "utf8"
  );

  assert.match(jobsTabSource, /\["light", "rem", "deep", "summarize"\]/);
});
