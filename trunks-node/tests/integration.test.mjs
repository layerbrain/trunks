import { strict as assert } from "node:assert";
import { execFileSync } from "node:child_process";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";

import { Trunks } from "../dist/index.js";

let trunksAvailable = false;
try {
  execFileSync("trunks", ["--help"], { stdio: "pipe" });
  trunksAvailable = true;
} catch {
  trunksAvailable = false;
}

test(
  "Trunks methods round-trip against the real CLI",
  { skip: trunksAvailable ? false : "trunks binary not on PATH" },
  async () => {
    const dir = mkdtempSync(join(tmpdir(), "trunks-int-"));

    execFileSync("trunks", ["init", "--name", "demo"], { cwd: dir });
    writeFileSync(join(dir, "a.md"), "a\n", "utf8");

    const trunks = new Trunks({ cwd: dir });

    await trunks.checkpoint("first");
    await trunks.branches.create({ name: "feat", from: "main" });

    const branches = await trunks.branches.list();
    assert.equal(branches.object, "list");
    assert.ok(branches.data.some((branch) => branch.name === "main"));
    assert.ok(branches.data.some((branch) => branch.name === "feat"));

    await trunks.branches.switch({ name: "feat" });
    writeFileSync(join(dir, "b.md"), "b\n", "utf8");
    await trunks.checkpoint("on feat");

    await trunks.tags.create({ name: "v1", at: "main" });
    const tags = await trunks.tags.list();
    assert.equal(tags.object, "list");
    assert.ok(tags.data.some((tag) => tag.name === "v1"));

    const diff = await trunks.diff({ vs: "main" });
    assert.ok(Array.isArray(diff));
    assert.ok(diff.some((entry) => entry.path === "b.md"));

    const commits = await trunks.log();
    assert.ok(commits.length >= 2);
    assert.ok(commits.every((commit) => typeof commit.id === "string"));

    await trunks.rollback({ to: "main" });
    await trunks.branches.switch({ name: "main" });
    await trunks.branches.delete({ name: "feat" });
    await trunks.tags.delete({ name: "v1" });

    const finalBranches = await trunks.branches.list();
    assert.ok(!finalBranches.data.some((branch) => branch.name === "feat"));
  },
);
