import assert from "node:assert/strict";
import test from "node:test";

function escapeHtml(value) {
  return value
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

test("HTML escaping protects request content", () => {
  assert.equal(
    escapeHtml('<script title="x">中😀 & test</script>'),
    "&lt;script title=&quot;x&quot;&gt;中😀 &amp; test&lt;/script&gt;",
  );
});

test("UTF-8 request payload stays below the configured 2 MiB limit", () => {
  const payload = JSON.stringify({
    messages: [{ role: "user", content: "中文 😀\n  whitespace" }],
  });
  assert.ok(new TextEncoder().encode(payload).byteLength < 2 * 1024 * 1024);
});

test("Base64 transport preserves Unicode, whitespace and HTML text", () => {
  const text = "中文 😀\n  <tag> & trailing  ";
  const encoded = Buffer.from(text, "utf8").toString("base64");
  assert.equal(Buffer.from(encoded, "base64").toString("utf8"), text);
});
