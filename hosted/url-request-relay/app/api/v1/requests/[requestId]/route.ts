import {
  cleanupExpired,
  db,
  decodeRequestMessages,
  jsonResponse,
  readJsonBody,
  requireAuthorization,
  validationErrorResponse,
} from "@/lib/relay";

type RouteContext = {
  params: Promise<{ requestId: string }>;
};

export async function PUT(request: Request, context: RouteContext) {
  const unauthorized = await requireAuthorization(request);
  if (unauthorized) {
    return unauthorized;
  }
  try {
    await cleanupExpired();
    const { requestId } = await context.params;
    const body = (await readJsonBody(request)) as { messages?: unknown };
    const messages = decodeRequestMessages(body);
    const result = await db()
      .prepare(
        "UPDATE temporary_requests SET messages_json = ?1, updated_at = ?2 " +
          "WHERE request_id = ?3 AND expires_at > ?2",
      )
      .bind(JSON.stringify(messages), Date.now(), requestId)
      .run();
    if (!result.meta.changes) {
      return jsonResponse({ error: "not found" }, { status: 404 });
    }
    return jsonResponse({ status: "updated" });
  } catch (error) {
    return validationErrorResponse(error);
  }
}

export async function DELETE(request: Request, context: RouteContext) {
  const unauthorized = await requireAuthorization(request);
  if (unauthorized) {
    return unauthorized;
  }
  try {
    await cleanupExpired();
    const { requestId } = await context.params;
    await db()
      .prepare("DELETE FROM temporary_requests WHERE request_id = ?1")
      .bind(requestId)
      .run();
    return new Response(null, { status: 204 });
  } catch (error) {
    return validationErrorResponse(error);
  }
}
