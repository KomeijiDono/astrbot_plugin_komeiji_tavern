import { env } from "cloudflare:workers";

export const SERVICE_VERSION = "1.0.0";
export const MAX_BODY_BYTES = 2 * 1024 * 1024;
export const MIN_TTL_SECONDS = 30;
export const MAX_TTL_SECONDS = 3600;
export const DEFAULT_TTL_SECONDS = 600;

export type HostedMessage = {
  role: "system" | "user" | "assistant" | "tool";
  content: string;
};

type RelayEnv = {
  DB?: D1Database;
  URL_REQUEST_API_KEY?: string;
};

export type StoredRequest = {
  request_id: string;
  read_token_hash: string;
  messages_json: string;
  created_at: number;
  updated_at: number;
  expires_at: number;
};

export const SECURITY_HEADERS: Record<string, string> = {
  "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
  Pragma: "no-cache",
  "X-Content-Type-Options": "nosniff",
  "X-Frame-Options": "DENY",
  "X-Robots-Tag": "noindex, nofollow, noarchive",
  "Content-Security-Policy":
    "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
  "Referrer-Policy": "no-referrer",
};

export function relayEnv(): RelayEnv {
  return env as unknown as RelayEnv;
}

export function db(): D1Database {
  const database = relayEnv().DB;
  if (!database) {
    throw new Error("D1 binding DB is unavailable.");
  }
  return database;
}

export function jsonResponse(
  payload: unknown,
  init: ResponseInit = {},
): Response {
  const headers = new Headers(init.headers);
  for (const [name, value] of Object.entries(SECURITY_HEADERS)) {
    headers.set(name, value);
  }
  headers.set("Content-Type", "application/json; charset=utf-8");
  return new Response(JSON.stringify(payload), { ...init, headers });
}

export function textResponse(
  text: string,
  init: ResponseInit & { contentType?: string } = {},
): Response {
  const { contentType = "text/plain", ...responseInit } = init;
  const headers = new Headers(init.headers);
  for (const [name, value] of Object.entries(SECURITY_HEADERS)) {
    headers.set(name, value);
  }
  headers.set(
    "Content-Type",
    `${contentType}; charset=utf-8`,
  );
  return new Response(text, { ...responseInit, headers });
}

export async function sha256Hex(value: string): Promise<string> {
  const bytes = new TextEncoder().encode(value);
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest))
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
}

export function randomToken(byteLength = 32): string {
  const bytes = new Uint8Array(byteLength);
  crypto.getRandomValues(bytes);
  let binary = "";
  for (const byte of bytes) {
    binary += String.fromCharCode(byte);
  }
  return btoa(binary)
    .replaceAll("+", "-")
    .replaceAll("/", "_")
    .replace(/=+$/u, "");
}

export async function isAuthorized(request: Request): Promise<boolean> {
  const configured = relayEnv().URL_REQUEST_API_KEY?.trim() ?? "";
  const header = request.headers.get("Authorization")?.trim() ?? "";
  if (!configured || !header.startsWith("Bearer ")) {
    return false;
  }
  const supplied = header.slice("Bearer ".length).trim();
  if (!supplied) {
    return false;
  }
  const [expectedHash, suppliedHash] = await Promise.all([
    sha256Hex(configured),
    sha256Hex(supplied),
  ]);
  return expectedHash === suppliedHash;
}

export async function requireAuthorization(
  request: Request,
): Promise<Response | null> {
  if (await isAuthorized(request)) {
    return null;
  }
  return jsonResponse({ error: "unauthorized" }, { status: 401 });
}

export async function readJsonBody(request: Request): Promise<unknown> {
  const declared = Number(request.headers.get("Content-Length") ?? "0");
  if (Number.isFinite(declared) && declared > MAX_BODY_BYTES) {
    throw new RequestValidationError("request body exceeds 2 MiB", 413);
  }
  const raw = await request.text();
  if (new TextEncoder().encode(raw).byteLength > MAX_BODY_BYTES) {
    throw new RequestValidationError("request body exceeds 2 MiB", 413);
  }
  try {
    return JSON.parse(raw);
  } catch {
    throw new RequestValidationError("request body must be valid JSON", 400);
  }
}

export class RequestValidationError extends Error {
  constructor(
    message: string,
    public readonly status: number,
  ) {
    super(message);
  }
}

export function validateMessages(value: unknown): HostedMessage[] {
  if (!Array.isArray(value) || value.length === 0 || value.length > 512) {
    throw new RequestValidationError(
      "messages must be a non-empty array with at most 512 items",
      400,
    );
  }
  const roles = new Set(["system", "user", "assistant", "tool"]);
  return value.map((item) => {
    if (!item || typeof item !== "object") {
      throw new RequestValidationError("each message must be an object", 400);
    }
    const role = Reflect.get(item, "role");
    const content = Reflect.get(item, "content");
    if (typeof role !== "string" || !roles.has(role)) {
      throw new RequestValidationError("message role is invalid", 400);
    }
    if (typeof content !== "string") {
      throw new RequestValidationError("message content must be a string", 400);
    }
    return { role, content } as HostedMessage;
  });
}

function decodeBase64Utf8(value: unknown): string {
  if (
    typeof value !== "string" ||
    !/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/u.test(
      value,
    )
  ) {
    throw new RequestValidationError("message content_base64 is invalid", 400);
  }
  try {
    const binary = atob(value);
    const bytes = Uint8Array.from(binary, (character) =>
      character.charCodeAt(0),
    );
    return new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  } catch {
    throw new RequestValidationError(
      "message content_base64 must contain valid UTF-8",
      400,
    );
  }
}

export function decodeRequestMessages(body: unknown): HostedMessage[] {
  if (!body || typeof body !== "object") {
    throw new RequestValidationError("request body must be an object", 400);
  }
  const encoding = Reflect.get(body, "content_encoding");
  const messages = Reflect.get(body, "messages");
  if (encoding === undefined || encoding === null) {
    return validateMessages(messages);
  }
  if (encoding !== "base64_utf8") {
    throw new RequestValidationError("content_encoding is unsupported", 400);
  }
  if (!Array.isArray(messages)) {
    throw new RequestValidationError("messages must be an array", 400);
  }
  return validateMessages(
    messages.map((item) => {
      if (!item || typeof item !== "object") {
        throw new RequestValidationError("each message must be an object", 400);
      }
      return {
        role: Reflect.get(item, "role"),
        content: decodeBase64Utf8(Reflect.get(item, "content_base64")),
      };
    }),
  );
}

export function validateTtl(value: unknown): number {
  if (value === undefined || value === null) {
    return DEFAULT_TTL_SECONDS;
  }
  if (
    typeof value !== "number" ||
    !Number.isInteger(value) ||
    value < MIN_TTL_SECONDS ||
    value > MAX_TTL_SECONDS
  ) {
    throw new RequestValidationError(
      `ttl_seconds must be an integer from ${MIN_TTL_SECONDS} to ${MAX_TTL_SECONDS}`,
      400,
    );
  }
  return value;
}

export async function ensureSchema(): Promise<void> {
  const database = db();
  await database.batch([
    database
      .prepare(
        "CREATE TABLE IF NOT EXISTS temporary_requests (" +
          "request_id TEXT PRIMARY KEY NOT NULL, " +
          "read_token_hash TEXT NOT NULL UNIQUE, " +
          "messages_json TEXT NOT NULL, " +
          "created_at INTEGER NOT NULL, " +
          "updated_at INTEGER NOT NULL, " +
          "expires_at INTEGER NOT NULL)",
      ),
    database.prepare(
      "CREATE INDEX IF NOT EXISTS temporary_requests_expires_at_idx " +
        "ON temporary_requests (expires_at)",
    ),
  ]);
}

export async function cleanupExpired(now = Date.now()): Promise<void> {
  await ensureSchema();
  await db()
    .prepare("DELETE FROM temporary_requests WHERE expires_at <= ?1")
    .bind(now)
    .run();
}

export function escapeHtml(value: string): string {
  return value
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

export function renderRequestHtml(messages: HostedMessage[]): string {
  const sections = messages
    .map(
      (message, index) =>
        `<section data-index="${index + 1}" data-role="${escapeHtml(message.role)}">` +
        `<h2>${index + 1}. ${escapeHtml(message.role)}</h2>` +
        `<pre>${escapeHtml(message.content)}</pre></section>`,
    )
    .join("");
  return (
    '<!doctype html><html><head><meta charset="utf-8">' +
    '<meta name="robots" content="noindex,nofollow,noarchive">' +
    "<title>Temporary model request</title>" +
    "<style>body{font:16px/1.5 system-ui,sans-serif;max-width:960px;margin:2rem auto;padding:0 1rem;color:#17202a}" +
    "section{margin:1.5rem 0}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f4f6f8;padding:1rem;border-radius:.5rem}" +
    "h1,h2{line-height:1.2}</style></head><body>" +
    "<h1>Temporary model request</h1>" +
    "<p>Preserve message roles and order, then answer the request.</p>" +
    sections +
    "</body></html>"
  );
}

export function validationErrorResponse(error: unknown): Response {
  if (error instanceof RequestValidationError) {
    return jsonResponse({ error: error.message }, { status: error.status });
  }
  return jsonResponse({ error: "internal server error" }, { status: 500 });
}
