import { auth } from "@clerk/nextjs/server";

import { isClerkConfigured } from "./clerk-config";

export type ConsoleAuthContext = {
  userId: string | null;
  orgId: string | null;
  isInternalSupport: boolean;
};

export async function readAuthContext(): Promise<ConsoleAuthContext> {
  if (!isClerkConfigured()) {
    return { userId: null, orgId: null, isInternalSupport: false };
  }

  const session = await auth();
  const claims = session.sessionClaims as Record<string, unknown> | null;
  // Only publicMetadata is trusted: it is writable solely via Clerk's backend
  // API. A bare `metadata` claim could be template-mapped from user-writable
  // unsafeMetadata, which would let a customer self-grant support access.
  const metadata = (claims?.publicMetadata ?? {}) as Record<string, unknown>;
  const isInternalSupport =
    metadata.vexicInternal === true ||
    metadata.vexicRole === "internal" ||
    (Boolean(process.env.VEXIC_INTERNAL_ORG_ID) && session.orgId === process.env.VEXIC_INTERNAL_ORG_ID);

  return {
    userId: session.userId ?? null,
    orgId: session.orgId ?? null,
    isInternalSupport
  };
}
