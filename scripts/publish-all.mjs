#!/usr/bin/env node
// Publish the pi-package under all three names by swapping the `name` field
// in package.json. Each name is an independent, self-contained package with
// identical content (bridge + skills); only the name differs — no aliasing
// indirection, so `pi install npm:<name>` always resolves the real files.
//
//   node scripts/publish-all.mjs            # real publish (CI: OIDC, no token)
//   node scripts/publish-all.mjs --dry-run  # npm publish --dry-run for each
//
// Names:
//   pi-mnemos                 — PRIMARY (pi-* convention, matches pi-subagents/pi-mcp-adapter)
//   mnemos-pi                 — alias (mnemos-* convention)
//   @korrlabs/mnemospi        — alias (short scoped, korrlabs org)
//   @korrlabs/mnemos-pi        — alias (scoped, korrlabs org)
//
// NOTE: the bare unscoped `mnemospi` is unpublishable by npm policy —
// the registry rejects names too similar to the existing `mnemos-pi`
// (403 "Package name too similar"). That same rule blocks squatters for us,
// and the scoped variant covers the short-name use case.
//
// FIRST PUBLISH of each name must be done manually (npm requires a package to
// exist before you can configure a Trusted Publisher for it). After the first
// manual publish of each name, configure an npm Trusted Publisher pointing at
// repo `vesmaro/vesma` + workflow `publish-npm.yml` + environment `npm`,
// then this script (via the workflow) publishes all three over OIDC with no
// long-lived token.
import fs from "node:fs";
import path from "node:path";
import { execSync } from "node:child_process";

const root = path.resolve(import.meta.dirname, "..");
const pjPath = path.join(root, "package.json");
const original = fs.readFileSync(pjPath, "utf8");
const pj = JSON.parse(original);

const dryRun = process.argv.includes("--dry-run");
const inCI = !!process.env.GITHUB_ACTIONS;
const provenance = inCI && !dryRun; // --provenance needs OIDC (id-token: write)
const names = ["pi-mnemos", "mnemos-pi", "@korrlabs/mnemospi", "@korrlabs/mnemos-pi"];

// Auth pre-check: npm session tokens expire after 2h (npm Dec 2025 policy).
// Fail fast with a clear message instead of 3× confusing E404.
if (!dryRun && !inCI) {
  try {
    const who = execSync("npm whoami", { stdio: "pipe", cwd: root, timeout: 15_000 }).toString().trim();
    console.log(`✓ npm auth: ${who}`);
  } catch {
    console.error("❌ npm auth expired — run `npm login` first, then re-run this script.");
    process.exit(1);
  }
}

const results = []; // {name, status: "published"|"already"|"failed"}

// Idempotency pre-check: is this name@version already on the registry?
// `npm view <pkg>@<ver> version` exits 0 if it exists, non-zero (E404) if not.
// With stdio:"inherit" the publish error text is NOT capturable via execSync,
// so we check BEFORE publishing instead of parsing npm's error output.
function isAlreadyPublished(name, version) {
  try {
    const out = execSync(`npm view ${name}@${version} version`, { cwd: root, timeout: 60_000 })
      .toString()
      .trim();
    return out === version || out.includes(version);
  } catch {
    return false; // 404 or network hiccup — let the publish itself decide
  }
}

try {
  for (const name of names) {
    pj.name = name;
    fs.writeFileSync(pjPath, JSON.stringify(pj, null, 2) + "\n");
    if (isAlreadyPublished(name, pj.version)) {
      console.log(`\n=== ${name} ===\n→ v${pj.version} already on npm — skipped (idempotent re-run)`);
      results.push({ name, status: "already" });
      continue;
    }
    const cmd = `npm publish --access public${provenance ? " --provenance" : ""}${dryRun ? " --dry-run" : ""}`;
    console.log(`\n=== ${name} ===\n$ ${cmd}`);
    try {
      execSync(cmd, { stdio: "inherit", cwd: root });
      results.push({ name, status: "published" });
    } catch {
      // npm's own error output went straight to the terminal above.
      console.error(`→ ${name}: PUBLISH FAILED — continuing with the other names`);
      results.push({ name, status: "failed" });
    }
  }
} finally {
  fs.writeFileSync(pjPath, original);
  console.log("\nrestored package.json");
}

console.log("\n=== RESULT ===");
for (const r of results) {
  const icon = r.status === "published" ? "✅ published" : r.status === "already" ? "☑️  already on npm (skip)" : "❌ failed";
  console.log(`${icon}  ${r.name}`);
}
const failed = results.filter(r => r.status === "failed");
if (failed.length > 0) {
  console.error(`\n${failed.length} name(s) failed — fix the errors above and re-run (safe: already-published versions are skipped)`);
  process.exit(1);
}