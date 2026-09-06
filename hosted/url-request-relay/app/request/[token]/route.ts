import {
  SECURITY_HEADERS,
  cleanupExpired,
  db,
  jsonResponse,
  renderRequestHtml,
  sha256Hex,
  textResponse,
} from "@/lib/relay";
import type { HostedMessage } from "@/lib/relay";

type RouteContext = {
  params: Promise<{ token: string }>;
};

async function loadMessages(token: string): Promise<HostedMessage[] | null> {
  if (!/^[A-Za-z0-9_-]{43}$/u.test(token)) {
    return null;
  }
  await cleanupExpired();
  const tokenHash = await sha256Hex(token);
  const row = await db()
    .prepare(
      "SELECT messages_json FROM temporary_requests " +
        "WHERE read_token_hash = ?1 AND expires_at > ?2 LIMIT 1",
    )
    .bind(tokenHash, Date.now())
    .first<{ messages_json: string }>();
  if (!row) {
    return null;
  }
  try {
    return JSON.parse(row.messages_json) as HostedMessage[];
  } catch {
    return null;
  }
}

export async function GET(request: Request, context: RouteContext) {
  const { token } = await context.params;
  const messages = await loadMessages(token);
  if (!messages) {
    return textResponse("Not found", { status: 404 });
  }
  if (
    request.headers.get("Accept")?.toLowerCase().includes("application/json")
  ) {
    return jsonResponse({
      instruction: "Preserve message roles and order, then answer the request.",
      messages,
    });
  }
  return textResponse(renderRequestHtml(messages), {
    contentType: "text/html",
  });
}

export async function HEAD(_request: Request, context: RouteContext) {
  const { token } = await context.params;
  const messages = await loadMessages(token);
  return new Response(null, {
    status: messages ? 200 : 404,
    headers: SECURITY_HEADERS,
  });
}
