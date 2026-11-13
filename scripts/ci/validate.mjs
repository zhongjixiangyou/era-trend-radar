#!/usr/bin/env node
// scripts/ci/validate.mjs — 门禁：字段/评分/日期/去重/关联/不可覆盖/来源。
// 只返回 0（通过）或 1（失败），绝不返回 2；任何非 0 都阻断提交与 deploy。
//
// 用法:
//   node scripts/ci/validate.mjs [--data content|content/data.json] [--config pipeline.config.json]
//        [--baseline <file>] [--no-git-baseline] [--report reports/validate.json] [--json]
//        [--delegate <validate-data.mjs>] [--no-delegate]
import fs from "node:fs";
import path from "node:path";
import {
  ROOT,
  loadConfig,
  parseArgs,
  readJson,
  writeJson,
  resolvePath,
  relative,
  run,
  notice,
  warning,
  appendSummary,
} from "./lib/common.mjs";
import { validateData, readGitBaseline } from "./lib/data.mjs";

const TOOL_VERSION = "0.3.0";
const KNOWN_ARGS = new Set([
  "_",
  "data",
  "config",
  "baseline",
  "git-baseline",
  "no-git-baseline",
  "report",
  "json",
  "delegate",
  "no-delegate",
]);

function usageExit(msg) {
  console.error(`[validate] 用法错误：${msg}`);
  console.error("  node scripts/ci/validate.mjs [--data content|content/data.json] [--config pipeline.config.json] [--baseline <file>] [--no-git-baseline] [--report reports/validate.json] [--json] [--delegate <validate-data.mjs>] [--no-delegate]");
  process.exit(2);
}
const CHECK_DEFS = [
  { id: "schema", title: "必填字段与类型" },
  { id: "score", title: "六维评分与求和" },
  { id: "dates", title: "事件日期窗口" },
  { id: "dedupe", title: "去重键唯一" },
  { id: "links", title: "趋势关联不断链" },
  { id: "directions", title: "关联方向合法" },
  { id: "immutability", title: "首次记录不可覆盖" },
  { id: "sources", title: "来源 URL 齐全" },
];
const CODE_GROUP = {
  MISSING_FIELD: "schema",
  WRONG_TYPE: "schema",
  SCHEMA_VERSION_MISMATCH: "schema",
  DUPLICATE_ID: "schema",
  DIMENSION_OVER_CAP: "score",
  SCORE_SUM_MISMATCH: "score",
  DISPLAYED_MISMATCH: "score",
  SCORE_VERSION_MISSING: "score",
  SCORE_BASIS_MISSING: "score",
  FIRST_RECORD_MISMATCH: "score",
  EVENT_DATE_INVALID: "dates",
  EVENT_DATE_OUT_OF_RANGE: "dates",
  DATE_ORDER: "dates",
  DUPLICATE_DEDUPE_KEY: "dedupe",
  TREND_LINK_MISSING: "links",
  BACKLINK_MISMATCH: "links",
  TREND_LINK_DIRECTION: "directions",
  DIRECTION_INVALID: "directions",
  FIRST_RECORD_OVERWRITTEN: "immutability",
  HISTORY_SHRUNK: "immutability",
  HISTORY_REVISION_GAP: "immutability",
  SOURCE_MISSING: "sources",
  SOURCE_URL_INVALID: "sources",
};

function groupOf(code) {
  return CODE_GROUP[code] || "schema";
}

function resolveDataFile(cfg, dataArg) {
  const abs = resolvePath(dataArg || cfg.data.dir || "content");
  if (!fs.existsSync(abs)) return { abs, missing: true };
  const stat = fs.statSync(abs);
  if (stat.isDirectory()) return { abs: path.join(abs, cfg.data.file || "data.json"), missing: false };
  return { abs, missing: false };
}

/** 自动寻找三组查错的独立验收器（存在才跑，不存在不失败）。 */
function findDelegate() {
  const candidates = [
    "scripts/check/validate-data.mjs",
    "check/scripts/ci/validate-data.mjs",
    "vendor/check/validate-data.mjs",
    "../check/scripts/ci/validate-data.mjs",
  ];
  for (const rel of candidates) {
    const abs = resolvePath(rel);
    if (fs.existsSync(abs)) return abs;
  }
  return null;
}

function runDelegate(file, dataFile, cfg, reportPath) {
  const out = writeJson(reportPath, { pending: true });
  const res = run(
    process.execPath,
    [
      file,
      "--data",
      dataFile,
      "--json",
      out,
      "--quiet",
    ],
    { timeoutMs: 300000 },
  );
  const delegated = { path: file, exit: res.status, errors: 0, warnings: 0, report: out, stderr: "" };
  try {
    const rep = JSON.parse(fs.readFileSync(out, "utf8"));
    delegated.errors = rep.summary?.errors ?? rep.errors?.length ?? 0;
    delegated.warnings = rep.summary?.warnings ?? rep.warnings?.length ?? 0;
    delegated.checks = (rep.checks || []).map((c) => ({ id: c.id, status: c.status }));
  } catch {
    /* 报告读不到就以退出码为准 */
  }
  const lines = (res.stderr || res.stdout || "").trim().split("\n").filter(Boolean);
  delegated.stderr = lines.slice(-6).join("\n");
  return delegated;
}

function main() {
  const startedAt = Date.now();
  const args = parseArgs();
  for (const key of Object.keys(args)) {
    if (!KNOWN_ARGS.has(key)) usageExit(`未知参数 --${key}`);
  }
  for (const key of ["data", "config", "baseline", "report", "delegate"]) {
    if (args[key] === true) usageExit(`--${key} 需要一个值`);
  }
  let cfg;
  try {
    cfg = loadConfig(args.config || undefined);
  } catch (err) {
    usageExit(`配置读取失败：${err.message}`);
  }
  const { abs: dataFile } = resolveDataFile(cfg, args.data);
  const errors = [];
  const warnings = [];
  const notes = [];

  let doc = null;
  if (!fs.existsSync(dataFile)) {
    errors.push({
      check: "schema",
      code: "DATA_FILE_MISSING",
      path: relative(dataFile),
      message: `数据文件不存在。先跑 collect 生成 ${cfg.data.dir}/${cfg.data.file}，不能拿空目录过门禁。`,
    });
  } else {
    try {
      doc = readJson(dataFile);
    } catch (err) {
      errors.push({ check: "schema", code: "DATA_PARSE_ERROR", path: relative(dataFile), message: err.message });
    }
  }

  const dataRel = path.relative(ROOT, dataFile);
  const inRepo = !dataRel.startsWith("..") && !path.isAbsolute(dataRel);
  let baseline = null;
  let baselineSource = "none";
  if (doc && args.baseline) {
    try {
      baseline = readJson(args.baseline);
      baselineSource = `file:${relatedLabel(args.baseline)}`;
    } catch (err) {
      errors.push({ check: "immutability", code: "BASELINE_UNREADABLE", path: relatedLabel(args.baseline), message: err.message });
    }
  } else if (doc && inRepo && args["git-baseline"] !== false && args["no-git-baseline"] !== true) {
    baseline = readGitBaseline(run, dataRel);
    if (baseline) baselineSource = `git:HEAD:${dataRel}`;
  }

  let result = { errors: [], warnings: [], stats: {} };
  if (doc) {
    result = validateData(doc, cfg, { baseline });
    errors.push(...result.errors.map((e) => ({ check: groupOf(e.code), ...e })));
    warnings.push(...result.warnings.map((e) => ({ check: groupOf(e.code), ...e })));
    if (!baseline) notes.push("未找到基线（新数据或非 git 环境）：本轮只做结构校验，下一轮起启用不可覆盖比对。");
  }

  // 可选外挂：三组查错的独立验收器，双实现互证。
  let delegated = null;
  if (doc && args["no-delegate"] !== true) {
    const file = args.delegate ? resolvePath(args.delegate) : findDelegate();
    if (file && fs.existsSync(file)) {
      delegated = runDelegate(file, dataFile, cfg, "artifacts/reports/validate-checker.json");
      if (delegated.exit !== 0) {
        errors.push({
          check: "external",
          code: delegated.exit === 2 ? "DELEGATE_USAGE" : "DELEGATE_FAILED",
          path: relative(file),
          message: `独立验收器退出 ${delegated.exit}（${delegated.errors} 错误）${delegated.stderr ? `：${delegated.stderr}` : ""}`,
        });
      }
      notes.push(`外挂验收器 ${relative(file)} 退出 ${delegated.exit}。`);
    }
  }

  const checks = CHECK_DEFS.map((c) => {
    const errs = errors.filter((e) => e.check === c.id);
    const warns = warnings.filter((w) => w.check === c.id);
    return {
      id: c.id,
      title: c.title,
      status: errs.length ? "fail" : "pass",
      errorCount: errs.length,
      warningCount: warns.length,
    };
  });
  if (delegated) {
    checks.push({
      id: "external",
      title: "独立验收器（三组查错）",
      status: delegated.exit === 0 ? "pass" : "fail",
      errorCount: delegated.exit === 0 ? 0 : 1,
      warningCount: 0,
    });
  }

  const finishedAt = Date.now();
  const report = {
    tool: "validate",
    toolVersion: TOOL_VERSION,
    startedAt: new Date(startedAt).toISOString(),
    finishedAt: new Date(finishedAt).toISOString(),
    durationMs: finishedAt - startedAt,
    ok: errors.length === 0,
    dataFile: relative(dataFile),
    baseline: { source: baselineSource },
    options: { delegate: delegated?.path ? relative(delegated.path) : null },
    stats: result.stats || {},
    checks,
    errors,
    warnings,
    notes,
  };
  const reportPath = args.report || "reports/validate.json";
  writeJson(reportPath, report);

  if (args.json) {
    console.log(JSON.stringify(report, null, 2));
  } else {
    const mark = report.ok ? "PASS" : "FAIL";
    console.log(
      `[validate] ${mark} · 事件 ${report.stats.events ?? 0} / 趋势 ${report.stats.trends ?? 0} · 错误 ${errors.length} · 警告 ${warnings.length} · ${finishedAt - startedAt}ms`,
    );
    console.log(`  数据: ${report.dataFile}  基线: ${baselineSource}  报告: ${reportPath}`);
    for (const c of checks) console.log(`  ${c.status === "pass" ? "✓" : "✗"} ${c.id.padEnd(12)} ${c.title}${c.errorCount ? `（${c.errorCount}）` : ""}`);
    errors.slice(0, 40).forEach((e, i) => console.log(`  E${i + 1} [${e.code}] ${e.path} — ${e.message}`));
    if (errors.length > 40) console.log(`  … 其余 ${errors.length - 40} 条见报告`);
    warnings.slice(0, 10).forEach((w) => console.log(`  W [${w.code}] ${w.path} — ${w.message}`));
    notes.forEach((n) => console.log(`  · ${n}`));
    if (!report.ok) warning(`数据门禁未通过（${errors.length} 错误），阻断提交与 deploy。`);
  }

  const rows = checks.map((c) => `| ${c.status === "pass" ? "✅" : "❌"} | ${c.id} | ${c.title} | ${c.errorCount} | ${c.warningCount} |`).join("\n");
  appendSummary(
    `### 数据门禁 ${report.ok ? "通过" : "失败"}\n\n数据：\`${report.dataFile}\` · 基线：\`${baselineSource}\`\n\n| 结果 | 检查 | 说明 | 错误 | 警告 |\n|---|---|---|---|---|\n${rows}\n`,
  );
  if (report.ok) notice(`数据门禁通过：${report.stats.events} 事件 / ${report.stats.trends} 趋势`);

  process.exit(report.ok ? 0 : 1);
}

function relatedLabel(p) {
  return p ? relative(p) : String(p);
}

main();
