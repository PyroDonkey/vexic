import { NextResponse } from "next/server";

import { validateWaitlistPayload } from "@/lib/waitlist.mjs";

// Stub endpoint: validates and acknowledges signups. A real backend (durable
// store + notification) replaces the acknowledgement later; the contract stays.
export async function POST(request: Request) {
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

  return NextResponse.json({ ok: true });
}
