import { NextResponse } from "next/server";

import { createWaitlistRateLimiter, validateWaitlistPayload } from "@/lib/waitlist.mjs";
import { saveWaitlistSignup } from "@/lib/waitlist-store";

const allowSignup = createWaitlistRateLimiter();

export async function POST(request: Request) {
  const callerIp = (request.headers.get("x-forwarded-for") ?? "unknown").split(",")[0].trim();
  if (!allowSignup(callerIp)) {
    return NextResponse.json(
      { ok: false, error: "Too many attempts. Please try again in a minute." },
      { status: 429 }
    );
  }

  let payload: unknown;
  try {
    payload = await request.json();
  } catch {
    return NextResponse.json(
      { ok: false, error: "Request body must be valid JSON." },
      { status: 400 }
    );
  }

  const result = validateWaitlistPayload(payload);
  if (!result.ok) {
    return NextResponse.json({ ok: false, error: result.error }, { status: 400 });
  }

  const saved = await saveWaitlistSignup(result.email, result.source);

  if (saved.ok) {
    return NextResponse.json({ ok: true });
  }

  if (saved.reason === "unconfigured" && process.env.NODE_ENV === "development") {
    // Local dev without Turso credentials: accept and log so the form can be
    // exercised. Gated on an explicit "development" so a production server
    // with a missing/unset NODE_ENV fails loud (503) instead of silently
    // dropping real signups.
    console.warn(`[waitlist] TURSO_DATABASE_URL not set; dev-only acknowledgement for ${result.email}`);
    return NextResponse.json({ ok: true });
  }

  return NextResponse.json(
    { ok: false, error: "Signups are temporarily unavailable. Please try again shortly." },
    { status: 503 }
  );
}
