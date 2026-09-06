import {
  SERVICE_VERSION,
  cleanupExpired,
  jsonResponse,
  requireAuthorization,
} from "@/lib/relay";

export async function GET(request: Request) {
  const unauthorized = await requireAuthorization(request);
  if (unauthorized) {
    return unauthorized;
  }
  await cleanupExpired();
  return jsonResponse({
    status: "ok",
    service: "komeiji-request-relay",
    version: SERVICE_VERSION,
    storage: "d1",
  });
}
