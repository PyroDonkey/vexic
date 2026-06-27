import assert from "node:assert/strict";
import test from "node:test";

import { projectCreateFailureMessage, usageRows } from "../lib/console-ui-state.mjs";

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
