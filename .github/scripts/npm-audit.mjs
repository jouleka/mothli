// The dependency audit for one npm package folder (run from that folder).
//
// 1. What ships to readers (production dependencies): any advisory fails the check. No exceptions.
// 2. Build and test tooling (development dependencies): any advisory fails the check, except the
//    ones named in ALLOWED below. An exception is for an advisory that has no fixed version to
//    move to and that cannot be reached by a reader; it names the advisory, says why, and is
//    removed as soon as a fixed version exists (the check prints a reminder when it is no longer
//    needed).
import { spawnSync } from "node:child_process";

const ALLOWED = {
  // picomatch <= 4.0.7, the newest published version on 2026-11-17: a slow pattern match
  // (ReDoS) on crafted glob patterns. It comes in only through vite and vitest, which run on
  // the build machine with our own patterns; it is not in what ships (`npm audit --omit=dev`
  // is clean). The only "fix" offered is a breaking downgrade of vite and vitest.
  "GHSA-fc5q-39xc-vjpf": "picomatch ReDoS, tooling only, no fixed version published",
};

function audit(...args) {
  const run = spawnSync("npm", ["audit", "--json", ...args], { encoding: "utf8", shell: true });
  try {
    return JSON.parse(run.stdout);
  } catch {
    console.error(run.stdout, run.stderr);
    throw new Error("npm audit did not answer with a report");
  }
}

function advisories(report) {
  const found = new Map();
  for (const entry of Object.values(report.vulnerabilities ?? {}))
    for (const via of entry.via)
      if (typeof via === "object") found.set(String(via.url).split("/").pop(), `${via.name}: ${via.title}`);
  return found;
}

const shipped = advisories(audit("--omit=dev"));
const everything = advisories(audit());
const unexpected = [...everything].filter(([id]) => shipped.has(id) || !(id in ALLOWED));

for (const [id, title] of everything)
  if (!shipped.has(id) && id in ALLOWED) console.log(`allowed for now: ${id} (${title}); ${ALLOWED[id]}`);
for (const id of Object.keys(ALLOWED))
  if (!everything.has(id)) console.log(`no longer reported: ${id}; remove it from ALLOWED`);

if (unexpected.length) {
  for (const [id, title] of unexpected)
    console.error(`${shipped.has(id) ? "in what ships" : "in tooling"}: ${id} (${title})`);
  process.exit(1);
}
console.log(`dependency audit passed: ${shipped.size} advisories in what ships, ${everything.size} in all`);
