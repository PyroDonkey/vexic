import assert from "node:assert/strict";
import { test } from "node:test";

import {
  createWaitlistRateLimiter,
  normalizeWaitlistSource,
  validateWaitlistPayload
} from "../lib/waitlist.mjs";

test("accepts a valid email and normalizes it", () => {
  const result = validateWaitlistPayload({ email: "  Dev@Example.COM " });
  assert.deepEqual(result, { ok: true, email: "dev@example.com", source: "unknown" });
});

test("returns the normalized source alongside the email", () => {
  const result = validateWaitlistPayload({ email: "dev@example.com", source: "  Footer-CTA " });
  assert.deepEqual(result, { ok: true, email: "dev@example.com", source: "footer-cta" });
});

test("accepts subdomain and plus addresses", () => {
  assert.equal(validateWaitlistPayload({ email: "a+tag@mail.example.co" }).ok, true);
});

test("rejects a missing body", () => {
  const result = validateWaitlistPayload(null);
  assert.equal(result.ok, false);
});

test("rejects a non-object body", () => {
  assert.equal(validateWaitlistPayload("dev@example.com").ok, false);
});

test("rejects a missing email", () => {
  const result = validateWaitlistPayload({});
  assert.equal(result.ok, false);
  assert.equal(result.error, "Email is required.");
});

test("rejects a non-string email", () => {
  assert.equal(validateWaitlistPayload({ email: 42 }).ok, false);
});

test("rejects an empty or whitespace email", () => {
  assert.equal(validateWaitlistPayload({ email: "   " }).ok, false);
});

test("rejects malformed emails", () => {
  for (const email of [
    "plainaddress",
    "no@tld",
    "spaces in@example.com",
    "@example.com",
    "a@b.c",
    "user@example.com,",
    "user@example.com.",
    "a@b..com",
    "user@example.co1"
  ]) {
    const result = validateWaitlistPayload({ email });
    assert.equal(result.ok, false, `expected rejection for ${JSON.stringify(email)}`);
  }
});

test("rejects an overlong email", () => {
  const email = `${"a".repeat(250)}@example.com`;
  assert.equal(validateWaitlistPayload({ email }).ok, false);
});

test("normalizes known sources", () => {
  assert.equal(normalizeWaitlistSource("hero"), "hero");
  assert.equal(normalizeWaitlistSource("  Footer-CTA "), "footer-cta");
});

test("collapses malformed sources to unknown", () => {
  for (const value of [undefined, null, 42, "", "   ", "a".repeat(41), "bad source!", "<script>"]) {
    assert.equal(normalizeWaitlistSource(value), "unknown", `expected unknown for ${JSON.stringify(value)}`);
  }
});

test("rate limiter allows up to max hits per window and recovers after it", () => {
  const allow = createWaitlistRateLimiter({ windowMs: 60_000, max: 3 });
  const start = 1_000_000;
  assert.equal(allow("ip-a", start), true);
  assert.equal(allow("ip-a", start + 1), true);
  assert.equal(allow("ip-a", start + 2), true);
  assert.equal(allow("ip-a", start + 3), false);
  // Other callers are unaffected.
  assert.equal(allow("ip-b", start + 3), true);
  // The window slides: old hits expire.
  assert.equal(allow("ip-a", start + 60_004), true);
});
