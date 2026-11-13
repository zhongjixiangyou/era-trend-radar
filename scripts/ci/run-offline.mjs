#!/usr/bin/env node
// scripts/ci/run-offline.mjs — 非联网全链路：采集(fixture) → 分析(fixture) → 校验 → 构建 → 打包 → 反证检索 → 月报。
// 只在 artifacts/offline 沙盒里跑，不碰仓库自己的 content/；任何一步非 0 立即中止并回报。
import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { ROOT, SCRIPTS, relative, writeJson, ensureDir, resolvePath } from "./lib/common.mjs";

const stage = resolvePath("artifacts/offline");
if (!stage.startsWith(path.join(ROOT, "artifacts") + path.sep)) {
  console.error(`[offline] 拒绝写入沙盒外的路径：${stage}`);
  process.exit(1);
}
fs.rmSync(stage, { recursive: true, force: true });
ensureDir(stage);
fs.mkdirSync(path.join(stage, "content"), { recursive: true });
fs.mkdirSync(path.join(stage, "reports"), { recursive: true });
fs.mkdirSync(path.join(stage, "history"), { recursive: true });

function copyDir(src, dst) {
  fs.mkdirSync(dst, { recursive: true });
  for (const name of fs.readdirSync(src)) {
    const s = path.join(src, name);
    const d = path.join(dst, name);
    if (fs.statSync(s).isDirectory()) copyDir(s, d);
    else fs.copyFileSync(s, d);
  }
}
fs.copyFileSync(path.join(ROOT, "pipeline.config.json"), path.join(stage, "pipeline.config.json"));
copyDir(path.join(ROOT, "fixtures/site-data/content"), path.join(stage, "content"));
copyDir(path.join(ROOT, "fixtures/feeds"), path.join(stage, "fixtures/feeds"));
copyDir(path.join(ROOT, "fixtures/model"), path.join(stage, "fixtures/model"));
copyDir(path.join(ROOT, "fixtures/search"), path.join(stage, "fixtures/search"));

const steps = [
  ["collect", ["scripts/ci/collect.mjs", "--fixture", "fixtures/feeds", "--report", "reports/collect.json"]],
  ["analyze", ["scripts/ci/analyze.mjs", "--fixture", "fixtures/model", "--data", "content/data.json", "--report", "reports/analyze.json"]],
  ["validate", ["scripts/ci/validate.mjs", "--data", "content/data.json", "--no-delegate", "--no-git-baseline", "--report", "reports/validate.json"]],
  ["build", ["scripts/ci/build.mjs", "--content", "content", "--out", "artifacts/site", "--report", "reports/build.json"]],
  ["package", ["scripts/ci/package.mjs", "--content", "content", "--out", "artifacts/era-content.tar.gz", "--report", "reports/package.json"]],
  ["factcheck", ["scripts/ci/factcheck.mjs", "--fixture", "fixtures/search", "--data", "content/data.json", "--report", "reports/factcheck.json"]],
  ["monthly-review", ["scripts/ci/monthly-review.mjs", "--data", "content/data.json"]],
  ["validate-after", ["scripts/ci/validate.mjs", "--data", "content/data.json", "--no-delegate", "--no-git-baseline", "--report", "reports/validate-final.json"]],
];

const log = [];
console.log(`[offline] 沙盒 ${relative(stage)}\n`);
for (const [name, argv] of steps) {
  const started = Date.now();
  const res = spawnSync(process.execPath, argv.map((a) => (a.startsWith("scripts/ci/") ? path.join(SCRIPTS, a.slice("scripts/ci/".length)) : a)), {
    cwd: stage,
    env: { ...process.env, ERA_ROOT: stage },
    encoding: "utf8",
    timeout: 5 * 60 * 1000,
  });
  const tail = `${res.stdout || ""}${res.stderr || ""}`.trim().split("\n").filter(Boolean).slice(-3).join(" | ");
  log.push({ step: name, exit: res.status, ms: Date.now() - started, tail });
  console.log(`[${res.status === 0 ? "✓" : "✗"}] ${name.padEnd(14)} exit=${res.status} ${Date.now() - started}ms  ${res.status === 0 ? tail.slice(0, 160) : tail.slice(-400)}`);
  if (res.status !== 0) {
    writeJson(path.join(relative(stage), "RUNLOG.json"), { ok: false, failedAt: name, steps: log });
    console.error(`\n[offline] 在 ${name} 中止，沙盒保留：${relative(stage)}`);
    process.exit(res.status);
  }
}

const data = JSON.parse(fs.readFileSync(path.join(stage, "content/data.json"), "utf8"));
const site = JSON.parse(fs.readFileSync(path.join(stage, "reports/build.json"), "utf8"));
const pack = JSON.parse(fs.readFileSync(path.join(stage, "reports/package.json"), "utf8"));
const fact = JSON.parse(fs.readFileSync(path.join(stage, "reports/factcheck.json"), "utf8"));
const summary = {
  ok: true,
  steps: log,
  data: { events: data.events.length, trends: data.trends.length },
  build: { mode: site.mode, parity: site.parity, files: site.files?.length ?? site.files },
  package: { archive: pack.archive, bytes: pack.bytes, sha256: pack.sha256 },
  factcheck: fact.totals,
  finishedAt: new Date().toISOString(),
};
writeJson(path.join(relative(stage), "RUNLOG.json"), summary);
console.log(`\n[offline] PASS · 事件 ${data.events.length} / 趋势 ${data.trends.length} · 构建 ${site.mode} parity=${site.parity} · 包 ${(pack.bytes / 1024).toFixed(1)}KB`);
console.log(`[offline] 产物：${relative(stage)}/artifacts/site, ${relative(stage)}/${pack.archive}`);
