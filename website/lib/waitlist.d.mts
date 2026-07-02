export function validateWaitlistPayload(
  payload: unknown
): { ok: true; email: string } | { ok: false; error: string };
