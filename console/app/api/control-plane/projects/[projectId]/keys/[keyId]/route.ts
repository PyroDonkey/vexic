import { revokeAgentKeyResponse } from "@/lib/control-plane-api.mjs";
import { readAuthContext } from "@/lib/auth";

export const dynamic = "force-dynamic";

export async function DELETE(request: Request, { params }: { params: Promise<{ projectId: string; keyId: string }> }) {
  const { projectId, keyId } = await params;
  return revokeAgentKeyResponse(request, await readAuthContext(), projectId, keyId);
}
