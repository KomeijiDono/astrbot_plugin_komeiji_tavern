import {
  SECURITY_HEADERS,
  SERVICE_VERSION,
  jsonResponse,
} from "@/lib/relay";

export function GET() {
  return jsonResponse({
    status: "ok",
    service: "komeiji-request-relay",
    version: SERVICE_VERSION,
  });
}

export function HEAD() {
  return new Response(null, { status: 200, headers: SECURITY_HEADERS });
}
