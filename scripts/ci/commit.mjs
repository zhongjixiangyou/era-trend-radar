#!/usr/bin/env node
// scripts/ci/commit.mjs — 幂等提交：只允许白名单路径，无 diff 就 no_change 不提交。
// 白名单来自 config.commit.whitelist，默认 content/ reports/ history/。
import fs from "node:fs";
import path from "node:path";
import { loadConfig, parseArgs, writeJson, run, mustRun, relative, resolvePath } from "./lib/common.mjs";
import { emitStatus, exitFor } from "./lib/status.mjs";

const TOOL = "commit";

function changedPaths() {
  const res = run("git", ["status", "--porcelain"]);
  if (res.status !== 0) throw new Error(`git status 失败：${res.stderr || res.stdout}`);
  const out = [];
  for (const line of res.stdout.split("\n")) {
    if (!line.trim()) continue;
    const body = line.slice(3).trim();
    const target = body.includes(" -> ") ? body.split(" -> ").pop() : body;
    const clean = target.replace(/^"|"$/g, "");
    // CI 本地产物不进提交：状态/报告摘要/缓存/依赖目录。
    if (/^(artifacts|node_modules|\.cache)\//.test(clean)) continue;
    out.push(clean);
  }
  return out;
}

function main() {
  const args = parseArgs();
  const cfg = loadConfig(args.config || undefined);
  const whitelist = cfg.commit?.whitelist || ["content/", "reports/", "history/"];
  let paths = changedPaths();
  const outside = paths.filter((p) => !whitelist.some((w) => p === w.replace(/\/$/, "") || p.startsWith(w)));
  const reportPath = args.report || "reports/commit.json";

  if (outside.length) {
    const message = `拒绝提交：${outside.length} 个路径不在白名单 ${whitelist.join(",")}：${outside.slice(0, 10).join(", ")}`;
    writeJson(reportPath, { tool: TOOL, status: "failed", message, outside });
    emitStatus(cfg, TOOL, "failed", { message });
    process.exit(exitFor(cfg, "validation_failed"));
  }

  if (paths.length === 0) {
    const message = "工作区无变更（git 层面无 diff），no_change 不提交";
    writeJson(reportPath, { tool: TOOL, status: "no_change", message });
    emitStatus(cfg, TOOL, "no_change", { message });
    process.exit(0);
  }

  // 逐轮重写的 CI 报告（gate/validate/commit 等带时间戳）不能进提交，否则每次跑都会产生一条噪声 commit，
  // 幂等就失效了。reports/ 下只提交「内容派生、可审阅」的报告（config.commit.durableReports），
  // 其余逐轮报告由工作流以同名 artifact 上传。
  const durable = cfg.commit?.durableReports || ["factcheck.json", "factcheck.md", "monthly-review-*.md", "monthly-review-*.json"];
  const globToRe = (pat) => new RegExp("^" + pat.replace(/[.+^${}()|[\]\\]/g, "\\$&").replace(/\*/g, ".*") + "$");
  const isDurableReport = (p) => durable.some((pat) => globToRe(pat).test(path.posix.basename(p)));
  const stageable = paths.filter(
    (p) => p.startsWith("content/") || p.startsWith("history/") || (p.startsWith("reports/") && isDurableReport(p)),
  );
  const skipped = paths.filter((p) => !stageable.includes(p));
  if (stageable.length === 0) {
    const message = `只有逐轮重写的 CI 报告有变化（${skipped.length} 个：${skipped.slice(0, 5).join(", ")}），no_change 不提交`;
    writeJson(reportPath, { tool: TOOL, status: "no_change", message, skipped });
    emitStatus(cfg, TOOL, "no_change", { message });
    process.exit(0);
  }
  paths = stageable;

  const statusFile = resolvePath("artifacts/status/analyze.json");
  let runStatus = "healthy";
  try {
    runStatus = JSON.parse(fs.readFileSync(statusFile, "utf8")).status || "healthy";
  } catch {
    /* 没有 analyze 状态就按 healthy */
  }
  const summary = args.message || `${runStatus}: ${paths.length} 个文件（${paths.slice(0, 3).join(", ")}${paths.length > 3 ? " …" : ""}）`;
  const message = `${cfg.commit?.messagePrefix || "chore(era)"}: ${summary}`;

  if (args["dry-run"]) {
    writeJson(reportPath, { tool: TOOL, status: "healthy", dryRun: true, message, paths });
    emitStatus(cfg, TOOL, "healthy", { message: `dry-run：${message}`, paths: paths.length });
    process.exit(0);
  }

  mustRun("git", ["add", "--", ...paths]);
  const commit = run("git", ["commit", "-m", message]);
  if (commit.status !== 0) {
    const out = `${commit.stdout}\n${commit.stderr}`;
    if (/nothing to commit|no changes added/i.test(out)) {
      writeJson(reportPath, { tool: TOOL, status: "no_change", message: "git add 后无差异（可能是忽略规则），no_change" });
      emitStatus(cfg, TOOL, "no_change", { message: "git add 后无差异，no_change" });
      process.exit(0);
    }
    const fail = `git commit 失败（exit ${commit.status}）：${out.split("\n").slice(-6).join("\n")}`;
    writeJson(reportPath, { tool: TOOL, status: "failed", message: fail });
    emitStatus(cfg, TOOL, "failed", { message: fail });
    process.exit(exitFor(cfg, "validation_failed"));
  }
  const sha = run("git", ["rev-parse", "--short", "HEAD"]).stdout.trim();
  let pushed = false;
  if (args.push === true) {
    const push = run("git", ["push"]);
    pushed = push.status === 0;
    if (!pushed) {
      const fail = `git push 失败（exit ${push.status}）：${(push.stderr || push.stdout).slice(-400)}`;
      writeJson(reportPath, { tool: TOOL, status: "failed", message: fail, sha });
      emitStatus(cfg, TOOL, "failed", { message: fail });
      process.exit(exitFor(cfg, "validation_failed"));
    }
  }
  const report = {
    tool: TOOL,
    status: "healthy",
    sha,
    pushed,
    message,
    paths: paths.length,
    files: paths.slice(0, 200),
  };
  writeJson(reportPath, report);
  emitStatus(cfg, TOOL, "healthy", { message: `${message} · ${sha}${pushed ? " pushed" : ""}`, sha });
  console.log(`[commit] ${sha} ${message}`);
  process.exit(0);
}

try {
  main();
} catch (err) {
  const cfg = (() => {
    try {
      return loadConfig();
    } catch {
      return {};
    }
  })();
  console.error(`[commit] ${err.stack || err.message}`);
  emitStatus(cfg, TOOL, "failed", { message: err.message });
  process.exit(1);
}
