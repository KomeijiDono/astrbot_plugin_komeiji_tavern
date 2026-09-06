import {
  cleanupExpired,
  db,
  decodeRequestMessages,
  jsonResponse,
  randomToken,
  readJsonBody,
  requireAuthorization,
  sha256Hex,
  validateTtl,
  validationErrorResponse,
} from "@/lib/relay";

export async function POST(request: Request) {
  const unauthorized = await requireAuthorization(request);
  if (unauthorized) {
    return unauthorized;
  }
  try {
    await cleanupExpired();
    const body = (await readJsonBody(request)) as {
      messages?: unknown;
      ttl_seconds?: unknown;
    };
    const messages = decodeRequestMessages(body);
    const ttlSeconds = validateTtl(body?.ttl_seconds);
    const requestId = crypto.randomUUID();
    const readToken = randomToken(32);
    const readTokenHash = await sha256Hex(readToken);
    const now = Date.now();
    const expiresAt = now + ttlSeconds * 1000;
    await db()
      .prepare(
        "INSERT INTO temporary_requests " +
          "(request_id, read_token_hash, messages_json, created_at, updated_at, expires_at) " +
          "VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
      )
      .bind(
        requestId,
        readTokenHash,
        JSON.stringify(messages),
        now,
        now,
        expiresAt,
      )
      .run();
    const origin = new URL(request.url).origin;
    return jsonResponse(
      {
        request_id: requestId,
        read_url: `${origin}/request/${readToken}`,
        expires_at: new Date(expiresAt).toISOString(),
      },
      { status: 201 },
    );
  } catch (error) {
    return validationErrorResponse(error);
  }
}
