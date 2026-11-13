#!/usr/bin/env node
// scripts/ci/entry-run.mjs — 按 config.entries 调用外部入口，不硬编码命令。
// 用法：node scripts/ci/entry-run.mjs --name validate [--report reports/x.json]
// 退出码沿用 config.exitCodes：入口 0 → healthy；非 0 一律 failed（含 2，不做豁免）。
import fs from "node:fs";
import path from "node:path";
import { loadConfig, parseArgs, writeJson, run, ROOT, relative } from "./lib/common.mjs";
import { emitStatus, exitFor } from "./lib/status.mjs";

const TOOL = "entry-run";
// 状态文件名用 entry-<name>，否则 validate 与 scoreCheck 会互相覆盖。
const statusTool = (name) => `entry-${name}`;

function main() {
  const args = parseArgs();
  const name = args.name;
  if (!name || name === true) {
    console.error(`[entry-run] 缺少 --name（可用：validate / scoreCheck / analyze / collect）`);
    process.exit(exitSafe("usage_error", 2));
  }
  const cfg = loadConfig(args.config || undefined);
  const entry = cfg.entries?.[name];
  if (!entry || !Array.isArray(entry.cmd) || entry.cmd.length === 0) {
    const message = `入口未配置：entries.${name}.cmd 为空`;
    writeJson(args.report || `reports/entry-${name}.json`, { tool: TOOL, name, status: "failed", reason: "entry_missing", message });
    emitStatus(cfg, statusTool(name), "failed", { name, message });
    console.error(`[entry-run] ${message}`);
    process.exit(exitFor(cfg, "source_failed"));
  }

  const retries = Number(cfg.budgets?.retry?.retries ?? 2);
  const started = Date.now();
  let attempts = 0;
  let res;
  for (; attempts <= retries; attempts += 1) {
    res = run(entry.cmd[0], entry.cmd.slice(1), { cwd: ROOT, timeout: 15 * 60 * 1000 });
    if (res.status === 0) break;
    if (attempts < retries) console.log(`[entry-run] ${name} 第 ${attempts + 1} 次失败（exit ${res.status}），重试`);
  }
  const ms = Date.now() - started;
  const ok = res.status === 0;
  const status = ok ? "healthy" : "failed";
  const report = {
    tool: TOOL,
    name,
    status,
    cmd: entry.cmd,
    exit: res.status,
    attempts: attempts + 1,
    ms,
    stdoutTail: (res.stdout || "").trim().split("\n").slice(-5).join("\n"),
    stderrTail: (res.stderr || "").trim().split("\n").slice(-5).join("\n"),
    finishedAt: new Date().toISOString(),
  };
  const out = args.report || entry.report || `reports/entry-${name}.json`;
  writeJson(out, report);

  const detail = ok
    ? `entries.${name} 通过（exit 0，${ms}ms）`
    : `entries.${name} 失败（exit ${res.status}，${attempts + 1} 次尝试）：${(report.stderrTail || report.stdoutTail).slice(-200)}`;
  emitStatus(cfg, statusTool(name), status, { name, message: detail, report: relative(out) });
  console.log(`[entry-run] ${status} · ${detail}`);
  if (ok) process.exit(0);
  // 入口自报的用法错误也一律算失败：工作流不做 2 的豁免。
  process.exit(exitFor(cfg, "validation_failed"));
}

function exitSafe(key, dflt) {
  try {
    return Number(loadConfig()?.exitCodes?.[key] ?? dflt);
  } catch {
    return dflt;
  }
}

try {
  main();
} catch (err) {
  console.error(`[entry-run] ${err.stack || err.message}`);
  process.exit(1);
}
