#!/usr/bin/env node
// scripts/ci/package.mjs — 打包 content/ reports/ history/ 为可下载 artifact。
// 边界：_intake/ 不进包；包内清单逐个文件记录哈希，便于部署侧复核。
import fs from "node:fs";
import path from "node:path";
import { loadConfig, parseArgs, writeJson, resolvePath, ensureDir, relative, sha256, run, mustRun } from "./lib/common.mjs";
import { emitStatus, exitFor } from "./lib/status.mjs";

const TOOL = "package";

function walk(dir, base = dir, out = []) {
  if (!fs.existsSync(dir)) return out;
  for (const name of fs.readdirSync(dir)) {
    const abs = path.join(dir, name);
    const st = fs.statSync(abs);
    if (st.isDirectory()) walk(abs, base, out);
    else out.push(path.relative(base, abs).split(path.sep).join("/"));
  }
  return out;
}

function main() {
  const args = parseArgs();
  const cfg = loadConfig(args.config || undefined);
  for (const k of ["content", "out", "report"]) if (args[k] === true) process.exit(exitFor(cfg, "usage_error"));
  const contentDir = resolvePath(args.content || cfg.data.dir);
  const reportsDir = resolvePath(args.reports || cfg.artifacts.reportsDir);
  const historyDir = resolvePath(args.history || cfg.artifacts.historyDir);
  const stamp = (process.env.GITHUB_SHA || "").slice(0, 8) || new Date().toISOString().slice(0, 10);
  const outFile = resolvePath(args.out || path.join(cfg.artifacts.dir || "artifacts", `era-content-${stamp}.tar.gz`));
  const reportPath = args.report || "reports/package.json";
  ensureDir(path.dirname(outFile));

  const dataFile = path.join(contentDir, cfg.data.file);
  if (!fs.existsSync(dataFile)) {
    const message = `打包失败：缺少 ${relative(dataFile)}`;
    writeJson(reportPath, { tool: TOOL, status: "failed", message });
    emitStatus(cfg, TOOL, "failed", { message });
    process.exit(exitFor(cfg, "validation_failed"));
  }

  const inputs = [contentDir, reportsDir, historyDir].filter((p) => fs.existsSync(p));
  const files = inputs.flatMap((dir) => walk(dir, dir).map((rel) => ({ dir, rel })));
  const intake = files.filter((f) => `${relative(f.dir)}/${f.rel}`.includes("_intake/"));
  // macOS bsdtar 不认 `--exclude=pattern`，必须是 `--exclude pattern`，且放在输入之前。
  const tarArgs = ["-czf", outFile, "--exclude", "_intake", ...inputs.map((p) => path.relative(process.cwd(), p) || ".")];
  const res = run("tar", tarArgs, { cwd: process.cwd() });
  if (res.status !== 0) {
    const message = `tar 失败（exit ${res.status}）：${(res.stderr || "").slice(-400)}`;
    writeJson(reportPath, { tool: TOOL, status: "failed", message });
    emitStatus(cfg, TOOL, "failed", { message });
    process.exit(exitFor(cfg, "validation_failed"));
  }

  const manifest = {
    tool: TOOL,
    createdAt: new Date().toISOString(),
    archive: relative(outFile),
    bytes: fs.statSync(outFile).size,
    sha256: sha256(fs.readFileSync(outFile)),
    data: { file: relative(dataFile), sha256: sha256(fs.readFileSync(dataFile)) },
    counts: {
      events: JSON.parse(fs.readFileSync(dataFile, "utf8")).events?.length ?? 0,
      trends: JSON.parse(fs.readFileSync(dataFile, "utf8")).trends?.length ?? 0,
      files: files.length,
    },
    intakeExcluded: intake.map((f) => `${relative(f.dir)}/${f.rel}`),
    files: files.map((f) => `${relative(f.dir)}/${f.rel}`),
  };
  const manifestPath = `${outFile}.manifest.json`;
  writeJson(manifestPath, manifest);
  const report = {
    tool: TOOL,
    status: "healthy",
    archive: relative(outFile),
    manifest: relative(manifestPath),
    bytes: manifest.bytes,
    sha256: manifest.sha256,
    counts: manifest.counts,
    intakeExcluded: manifest.intakeExcluded.length,
    message: `打包 ${manifest.counts.files} 文件 / ${(manifest.bytes / 1024).toFixed(1)} KB；_intake 排除 ${manifest.intakeExcluded.length} 项`,
  };
  writeJson(reportPath, report);
  emitStatus(cfg, TOOL, "healthy", { message: report.message, archive: report.archive });
  console.log(`[package] ${report.archive} (${report.bytes} bytes) sha256=${report.sha256.slice(0, 16)}…`);
  process.exit(0);
}

try {
  main();
} catch (err) {
  console.error(`[package] ${err.stack || err.message}`);
  process.exit(1);
}
