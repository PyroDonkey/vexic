import { createAgentKeyResponse, listAgentKeysResponse } from "@/lib/control-plane-api.mjs";
import { readAuthContext } from "@/lib/auth";

export const dynamic = "force-dynamic";

export async function GET(request: Request, { params }: { params: Promise<{ projectId: string }> }) {
  const { projectId } = await params;
  return listAgentKeysResponse(request, await readAuthContext(), projectId);
}

export async function POST(request: Request, { params }: { params: Promise<{ projectId: string }> }) {
  const { projectId } = await params;
  return createAgentKeyResponse(request, await readAuthContext(), projectId);
}
