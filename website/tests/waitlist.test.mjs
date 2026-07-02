import assert from "node:assert/strict";
import { test } from "node:test";

import { validateWaitlistPayload } from "../lib/waitlist.mjs";

test("accepts a valid email and normalizes it", () => {
  const result = validateWaitlistPayload({ email: "  Dev@Example.COM " });
  assert.deepEqual(result, { ok: true, email: "dev@example.com" });
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
  for (const email of ["plainaddress", "no@tld", "spaces in@example.com", "@example.com", "a@b.c"]) {
    const result = validateWaitlistPayload({ email });
    assert.equal(result.ok, false, `expected rejection for ${JSON.stringify(email)}`);
  }
});

test("rejects an overlong email", () => {
  const email = `${"a".repeat(250)}@example.com`;
  assert.equal(validateWaitlistPayload({ email }).ok, false);
});
