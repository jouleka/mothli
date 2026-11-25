// After `vite build`: the public pages written out as plain HTML (src/public/prerender.tsx), into
// prerendered/public-pages.json, which the backend image carries and fills in at each request
// (backend/app/hosted/public_pages.py). Run by `npm run build`; nothing here runs on the server.
//
// The app's components are drawn in a browser-like setting (jsdom, as the tests use) at the
// address "/", signed out, online, with nothing remembered on the device: what a first visitor's
// browser draws.
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { JSDOM } from "jsdom";
import { createServer } from "vite";

const here = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const dom = new JSDOM("<!doctype html><html><head></head><body></body></html>", { url: "https://mothli.invalid/" });
for (const name of ["window", "document", "navigator", "location", "history", "sessionStorage", "localStorage"]) {
  Object.defineProperty(globalThis, name, { value: dom.window[name], configurable: true, writable: true });
}

const server = await createServer({
  root: here,
  logLevel: "error",
  appType: "custom",
  server: { middlewareMode: true, hmr: false },
  optimizeDeps: { noDiscovery: true, include: [] },
});
try {
  const { prerender } = await server.ssrLoadModule("/src/public/prerender.tsx");
  const shell = readFileSync(resolve(here, "dist/index.html"), "utf8");
  const out = resolve(here, "prerendered/public-pages.json");
  mkdirSync(dirname(out), { recursive: true });
  const written = prerender(shell);
  writeFileSync(out, JSON.stringify(written));
  const documents = Object.values(written.pages).reduce((sum, versions) => sum + Object.keys(versions).length, 0);
  console.log(`public pages: ${documents} documents, ${Object.keys(written.money).length} currencies -> prerendered/public-pages.json`);
} finally {
  await server.close();
}
