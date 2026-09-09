import { buildSync } from "esbuild";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { spawnSync } from "node:child_process";

const directory = mkdtempSync(join(tmpdir(), "opentrons-previews-"));
try {
  const outfile = join(directory, "previews.cjs");
  buildSync({ entryPoints: ["tests/previews.test.tsx"], bundle: true, platform: "node", format: "cjs", outfile });
  const result = spawnSync(process.execPath, [outfile], { stdio: "inherit" });
  if (result.error) throw result.error;
  process.exitCode = result.status ?? 1;
} finally {
  rmSync(directory, { recursive: true, force: true });
}
