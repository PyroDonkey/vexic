import { usageDailyResponse } from "@/lib/control-plane-api.mjs";
import { readAuthContext } from "@/lib/auth";

export const dynamic = "force-dynamic";

export async function GET(request: Request, { params }: { params: Promise<{ projectId: string }> }) {
  const { projectId } = await params;
  return usageDailyResponse(request, await readAuthContext(), projectId);
}
