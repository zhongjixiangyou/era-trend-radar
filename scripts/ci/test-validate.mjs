#!/usr/bin/env node
// scripts/ci/test-validate.mjs — 门禁自测：正例必须过，反例必须被抓到，退出码 0/1/2 分层。
// 用 --no-delegate 保证自测只测本仓实现；外挂验收器单独做集成检查。
// 用法: node scripts/ci/test-validate.mjs [--quiet]
import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { ROOT, readJson, relative, ensureDir } from "./lib/common.mjs";

const args = process.argv.slice(2);
const quiet = args.includes("--quiet");
const outDir = "artifacts/reports/selftest";

function validate({ data, baseline, report, extra = [] }) {
  const argv = [path.join(ROOT, "scripts/ci/validate.mjs")];
  if (data) argv.push("--data", data);
  if (report) argv.push("--report", report);
  argv.push("--json", "--no-delegate", ...extra);
  if (baseline) argv.push("--baseline", baseline);
  const res = spawnSync(process.execPath, argv, { cwd: ROOT, encoding: "utf8", timeout: 180000 });
  let parsed = null;
  try {
    parsed = JSON.parse(fs.readFileSync(path.resolve(ROOT, report), "utf8"));
  } catch {
    /* 报告缺失时用退出码 */
  }
  return { exit: res.status, report: parsed, stdout: res.stdout || "", stderr: res.stderr || "" };
}

const cases = [];
cases.push({
  name: "good",
  want: "pass",
  run: () =>
    validate({
      data: "fixtures/site-data/content",
      baseline: "fixtures/baseline/content/data.json",
      report: `${outDir}/good.json`,
    }),
  expectCode: null,
});
cases.push({
  name: "missing-data",
  want: "fail",
  run: () =>
    validate({
      data: "fixtures/no-such-dir",
      report: `${outDir}/missing.json`,
    }),
  expectCode: "DATA_FILE_MISSING",
});

cases.push({
  name: "bad-flag",
  want: "usage",
  run: () => validate({ extra: ["--nope"] }),
  expectCode: null,
});

const manifest = readJson("fixtures/invalid/manifest.json");
for (const item of manifest) {
  cases.push({
    name: item.name,
    want: "fail",
    expectCode: item.expect,
    run: () =>
      validate({
        data: `fixtures/invalid/${item.name}/content`,
        baseline: item.needsBaseline ? "fixtures/site-data/content/data.json" : undefined,
        report: `${outDir}/${item.name}.json`,
      }),
  });
}

ensureDir(outDir);
let pass = 0;
const rows = [];
for (const c of cases) {
  let res;
  try {
    res = c.run();
  } catch (err) {
    rows.push({ name: c.name, ok: false, detail: `执行异常：${err.message}` });
    continue;
  }
  const codes = res.report?.errors?.map((e) => e.code) || [];
  const isPass = res.exit === 0 && codes.length === 0;
  const isFail = res.exit === 1 && codes.length > 0;
  const exitOk = c.want === "pass" ? isPass : c.want === "usage" ? res.exit === 2 : isFail;
  const codeOk = c.expectCode ? codes.includes(c.expectCode) : true;
  const ok = exitOk && codeOk;
  if (ok) pass += 1;
  rows.push({
    name: c.name,
    ok,
    detail: `exit=${res.exit} 期望=${c.want}${c.expectCode ? ` 命中=${codes.includes(c.expectCode) ? c.expectCode : `缺 ${c.expectCode}（实际 ${codes.slice(0, 4).join(",") || "无"}）`}` : ""}`,
  });
}

const width = Math.max(...rows.map((r) => r.name.length), 4);
console.log(`\n[test-validate] ${pass}/${rows.length} 通过\n`);
for (const r of rows) {
  console.log(`  ${r.ok ? "✓" : "✗"} ${r.name.padEnd(width)}  ${r.detail}`);
}
if (!quiet) console.log(`\n  报告目录: ${relative(outDir)}`);
process.exit(pass === rows.length ? 0 : 1);
