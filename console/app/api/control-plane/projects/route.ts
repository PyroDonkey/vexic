import { createProjectResponse, listProjectsResponse } from "@/lib/control-plane-api.mjs";
import { readAuthContext } from "@/lib/auth";

export const dynamic = "force-dynamic";

export async function GET(request: Request) {
  return listProjectsResponse(request, await readAuthContext());
}

export async function POST(request: Request) {
  return createProjectResponse(request, await readAuthContext());
}
