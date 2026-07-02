export function validateWaitlistPayload(
  payload: unknown
): { ok: true; email: string; source: string } | { ok: false; error: string };

export function normalizeWaitlistSource(value: unknown): string;

export function createWaitlistRateLimiter(options?: {
  windowMs?: number;
  max?: number;
  maxTracked?: number;
}): (key: string, now?: number) => boolean;
