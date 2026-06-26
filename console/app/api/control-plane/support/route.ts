import { supportMetadataResponse } from "@/lib/control-plane-api.mjs";
import { readAuthContext } from "@/lib/auth";

export const dynamic = "force-dynamic";

export async function GET(request: Request) {
  return supportMetadataResponse(request, await readAuthContext());
}
