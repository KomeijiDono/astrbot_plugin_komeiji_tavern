import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

test("status page replaces the disposable starter preview", async () => {
  const [page, layout, stylesheet, packageJson] = await Promise.all([
    readFile(new URL("../app/page.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/layout.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/globals.css", import.meta.url), "utf8"),
    readFile(new URL("../package.json", import.meta.url), "utf8"),
  ]);

  assert.match(page, /Komeiji Request Relay/);
  assert.match(page, /Service online/);
  assert.match(page, /unguessable/);
  assert.match(page, /Expired or\s+deleted requests return 404/);
  assert.match(layout, /index:\s*false/);
  assert.match(stylesheet, /\.status-card/);
  assert.doesNotMatch(
    `${page}\n${layout}\n${packageJson}`,
    /codex-preview|react-loading-skeleton|_sites-preview/i,
  );
});

test("worker applies security headers globally", async () => {
  const worker = await readFile(
    new URL("../worker/index.ts", import.meta.url),
    "utf8",
  );
  assert.match(worker, /Cache-Control.+no-store/s);
  assert.match(worker, /X-Robots-Tag.+noindex/s);
  assert.match(worker, /X-Frame-Options.+DENY/s);
  assert.match(worker, /Content-Security-Policy/s);
});
