#!/usr/bin/env node
// scripts/ci/gate.mjs — 统一放行闸：把各步骤写的 artifacts/status/<tool>.json 汇总成一张表。
// 规则：failed / 缺失 → 退出 1（阻断 commit 与 deploy）；healthy / no_change / degraded → 放行。
// 用法：node scripts/ci/gate.mjs --require collect,validate [--report reports/gate.json]
import fs from "node:fs";
import path from "node:path";
import { loadConfig, parseArgs, readJson, writeJson, resolvePath, relative } from "./lib/common.mjs";
import { STATUSES, exitFor } from "./lib/status.mjs";

const TOOL = "gate";
const PASS = new Set(["healthy", "no_change", "degraded"]);

function main() {
  const args = parseArgs();
  const known = new Set(["require", "config", "report", "strict"]);
  const unknown = Object.keys(args).filter((k) => k !== "_" && !known.has(k));
  if (unknown.length) {
    console.error(`[gate] 不认识的参数：${unknown.map((k) => `--${k}`).join(" ")}`);
    console.error("[gate] 可用参数：--require <a,b> --strict <a,b> --report <path> --config <path>");
    process.exit(exitSafe("usage_error", 2));
  }
  if (Array.isArray(args._) && args._.length) {
    console.error(`[gate] 多余的位置参数：${args._.join(" ")}（步骤用 --require 指定）`);
    process.exit(exitSafe("usage_error", 2));
  }
  const raw = args.require;
  if (!raw || raw === true) {
    console.error("[gate] 缺少 --require（逗号分隔，如 collect,validate）");
    process.exit(exitSafe("usage_error", 2));
  }
  const cfg = loadConfig(args.config || undefined);
  const tools = String(raw).split(",").map((s) => s.trim()).filter(Boolean);
  const strict = new Set(String(args.strict === true ? "" : args.strict || "").split(",").map((s) => s.trim()).filter(Boolean));
  // --strict 的步骤必须 healthy：用于 deploy 的 build，degraded（fallback 桩）不放行，避免覆盖线上旧版。
  const statusDir = resolvePath("artifacts/status");
  const rows = [];
  for (const tool of tools) {
    const file = path.join(statusDir, `${tool}.json`);
    if (!fs.existsSync(file)) {
      rows.push({ tool, status: "missing", ok: false, message: "没有状态文件（步骤未执行或未写状态）" });
      continue;
    }
    let st;
    try {
      st = readJson(file);
    } catch (err) {
      rows.push({ tool, status: "unreadable", ok: false, message: `状态文件无法解析：${err.message}` });
      continue;
    }
    const status = st.status;
    const needStrict = strict.has(tool);
    const ok = needStrict ? status === "healthy" : PASS.has(status);
    rows.push({ tool, status, ok, message: st.message || st.detail?.message || "", at: st.at });
  }

  const failed = rows.filter((r) => !r.ok);
  const valid = rows.filter((r) => STATUSES.includes(r.status)).length;
  const report = {
    tool: TOOL,
    status: failed.length ? "failed" : "healthy",
    required: tools,
    strict: [...strict],
    rows,
    note: `四态校验：${valid}/${rows.length} 个状态值合法；allowed=${[...PASS].join("|")}`,
    finishedAt: new Date().toISOString(),
  };
  const out = args.report || "reports/gate.json";
  writeJson(out, report);

  const lines = ["| 步骤 | 状态 | 通过 | 说明 |", "|---|---|---|---|", ...rows.map((r) => `| ${r.tool} | ${r.status} | ${r.ok ? "✅" : "❌"} | ${String(r.message).replace(/\|/g, "／").slice(0, 120)} |`)];
  const table = lines.join("\n");
  console.log(`[gate] ${report.status}\n\n${table}`);
  const summary = process.env.GITHUB_STEP_SUMMARY;
  if (summary) {
    try {
      fs.appendFileSync(summary, `\n### ${failed.length ? "❌" : "✅"} gate: ${report.status}\n\n${table}\n\n${report.note}\n`);
    } catch {
      /* summary 写不进去不影响结果 */
    }
  }
  process.exit(failed.length ? exitFor(cfg, "validation_failed") : 0);
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
  console.error(`[gate] ${err.stack || err.message}`);
  process.exit(1);
}
