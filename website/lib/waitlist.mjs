// Shared by the waitlist API route and node --test suites, so it stays plain JS.
// Domain labels may not contain dots (rejects "a@b..com", "user@example.com."),
// and the TLD must be alphabetic (rejects pasted trailing punctuation like
// "user@example.com,").
const EMAIL_PATTERN = /^[^\s@]+@[^\s@.]+(?:\.[^\s@.]+)*\.[A-Za-z]{2,}$/;
const MAX_EMAIL_LENGTH = 254;
const MAX_SOURCE_LENGTH = 40;
const SOURCE_PATTERN = /^[a-z0-9-]+$/;

/**
 * Validate a waitlist signup payload.
 * Returns { ok: true, email, source } with the normalized email and
 * attribution source, or { ok: false, error }.
 */
export function validateWaitlistPayload(payload) {
  if (payload === null || typeof payload !== "object") {
    return { ok: false, error: "Request body must be a JSON object." };
  }

  const raw = payload.email;
  if (typeof raw !== "string") {
    return { ok: false, error: "Email is required." };
  }

  const email = raw.trim().toLowerCase();
  if (email.length === 0) {
    return { ok: false, error: "Email is required." };
  }
  if (email.length > MAX_EMAIL_LENGTH) {
    return { ok: false, error: "Email is too long." };
  }
  if (!EMAIL_PATTERN.test(email)) {
    return { ok: false, error: "Enter a valid email address." };
  }

  return { ok: true, email, source: normalizeWaitlistSource(payload.source) };
}

/**
 * Normalize the attribution source for a signup. Unknown or malformed values
 * collapse to "unknown" rather than rejecting the signup — attribution is
 * best-effort, the email is the thing that matters.
 */
export function normalizeWaitlistSource(value) {
  if (typeof value !== "string") return "unknown";
  const source = value.trim().toLowerCase();
  if (source.length === 0 || source.length > MAX_SOURCE_LENGTH) return "unknown";
  if (!SOURCE_PATTERN.test(source)) return "unknown";
  return source;
}

/**
 * Sliding-window rate limiter for signup attempts, keyed by caller (IP).
 * In-memory and per-instance, so it is best-effort on serverless platforms —
 * enough to blunt naive scripted abuse without external state.
 * Returns a function allow(key, now?) -> boolean.
 */
export function createWaitlistRateLimiter({ windowMs = 60_000, max = 5, maxTracked = 5000 } = {}) {
  const hits = new Map();
  return function allow(key, now = Date.now()) {
    const cutoff = now - windowMs;
    const stamps = (hits.get(key) ?? []).filter((stamp) => stamp > cutoff);
    if (stamps.length >= max) {
      hits.set(key, stamps);
      return false;
    }
    stamps.push(now);
    hits.set(key, stamps);
    if (hits.size > maxTracked) {
      for (const [trackedKey, trackedStamps] of hits) {
        if (hits.size <= maxTracked) break;
        if (trackedKey !== key && trackedStamps.every((stamp) => stamp <= cutoff)) {
          hits.delete(trackedKey);
        }
      }
    }
    return true;
  };
}
