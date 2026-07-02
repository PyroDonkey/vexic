// Shared by the waitlist API route and node --test suites, so it stays plain JS.
const EMAIL_PATTERN = /^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/;
const MAX_EMAIL_LENGTH = 254;

/**
 * Validate a waitlist signup payload.
 * Returns { ok: true, email } with the normalized email, or { ok: false, error }.
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

  return { ok: true, email };
}
