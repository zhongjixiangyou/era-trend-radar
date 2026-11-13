#!/usr/bin/env node
// scripts/ci/analyze.mjs — 分析层：候选 → 小模型筛选 → 深研事件 → 合并进 content/data.json。
// 无密钥：显式「分析未执行」，不产出、不提交、退出 0；绝不伪造内容。
// 合并前后各校验一次，校验失败回滚备份并退出 1；首次评分/判断永不覆盖。
import fs from "node:fs";
import path from "node:path";
import {
  ROOT,
  SCRIPTS,
  loadConfig,
  parseArgs,
  readJson,
  writeJson,
  resolvePath,
  ensureDir,
  relative,
  run,
  notice,
  warning,
  appendSummary,
  Budget,
} from "./lib/common.mjs";
import { emitStatus, exitFor } from "./lib/status.mjs";
import { entryOf } from "./lib/entry.mjs";

const TOOL = "analyze";

function usage(msg, cfg) {
  console.error(`[analyze] 用法错误：${msg}`);
  console.error("  node scripts/ci/analyze.mjs [--fixture fixtures/model] [--data content/data.json] [--report reports/analyze.json] [--dry-run]");
  process.exit(Number(cfg?.exitCodes?.usage_error ?? 2));
}

function skeleton(cfg) {
  return {
    schemaVersion: cfg.schemaVersion,
    scoringVersion: cfg.scoringVersion,
    modelVersion: cfg.modelVersion,
    generatedAt: new Date().toISOString(),
    events: [],
    trends: [],
  };
}

async function callModel(cfg, body) {
  const key = process.env[cfg.model.apiKeyEnv];
  const base = process.env[cfg.model.baseUrlEnv] || cfg.model.endpoint;
  const res = await fetch(base, {
    method: "POST",
    headers: { "content-type": "application/json", authorization: `Bearer ${key}` },
    body: JSON.stringify({
      model: cfg.model.id,
      temperature: cfg.model.temperature ?? 0.2,
      response_format: { type: "json_object" },
      ...body,
    }),
    signal: AbortSignal.timeout(cfg.model.timeoutMs || 90000),
  });
  if (!res.ok) throw new Error(`模型接口 HTTP ${res.status}: ${(await res.text()).slice(0, 300)}`);
  const json = await res.json();
  const text = json.choices?.[0]?.message?.content;
  if (!text) throw new Error("模型返回为空");
  return JSON.parse(text);
}

function assertEventShape(ev, cfg) {
  const need = ["id", "title", "dedupeKey", "dates", "sources", "trendLinks", "score", "firstScore", "firstJudgment", "scoreHistory", "judgmentHistory"];
  const missing = need.filter((k) => ev[k] === undefined || ev[k] === null);
  if (missing.length) throw new Error(`事件 ${ev.id || "?"} 缺字段: ${missing.join(",")}`);
  return ev;
}

function mergeEvents(doc, incoming, cfg) {
  const incomingEvents = Array.isArray(incoming) ? incoming : incoming?.events || [];
  const byId = new Set(doc.events.map((e) => e.id));
  const byKey = new Set(doc.events.map((e) => e.dedupeKey));
  const added = [];
  const skipped = [];
  for (const raw of incomingEvents) {
    const ev = assertEventShape(raw, cfg);
    if (byId.has(ev.id) || byKey.has(ev.dedupeKey)) {
      skipped.push({ id: ev.id, reason: byId.has(ev.id) ? "duplicate_id" : "duplicate_dedupe_key" });
      continue;
    }
    // 首次记录不可覆盖：以 history 首条为准，防止模型给的 firstScore 漂移。
    ev.firstScore = ev.scoreHistory[0]?.score ?? ev.firstScore;
    ev.firstJudgment = ev.judgmentHistory[0]?.judgment ?? ev.firstJudgment;
    doc.events.push(ev);
    byId.add(ev.id);
    byKey.add(ev.dedupeKey);
    added.push(ev.id);
    // 回链：新事件进趋势 eventIds，避免 BACKLINK_MISMATCH。
    for (const link of ev.trendLinks || []) {
      const tr = doc.trends.find((t) => t.id === link.trendId);
      if (tr && Array.isArray(tr.eventIds) && !tr.eventIds.includes(ev.id)) tr.eventIds.push(ev.id);
    }
  }
  const trendIds = new Set(doc.trends.map((t) => t.id));
  for (const tr of (Array.isArray(incoming) ? [] : incoming?.trends) || []) {
    if (!tr.id || trendIds.has(tr.id)) continue;
    tr.eventIds = Array.isArray(tr.eventIds) ? tr.eventIds.filter((id) => byId.has(id)) : undefined;
    if (tr.eventIds && tr.eventIds.length === 0) delete tr.eventIds;
    doc.trends.push(tr);
    trendIds.add(tr.id);
  }
  return { added, skipped };
}

async function main() {
  const startedAt = Date.now();
  const args = parseArgs();
  for (const k of ["fixture", "data", "report"]) if (args[k] === true) usage(`--${k} 需要值`, null);

  let cfg;
  try {
    cfg = loadConfig(args.config || undefined);
  } catch (err) {
    usage(`配置读取失败：${err.message}`, null);
  }

  const dataFile = resolvePath(args.data || path.join(cfg.data.dir, cfg.data.file));
  const reportPath = args.report || cfg.entries?.analyze?.report || "reports/analyze.json";
  const keyName = cfg.model.apiKeyEnv;
  const key = process.env[keyName];
  const fixtureDir = args.fixture ? resolvePath(args.fixture) : null;

  // 外部入口优先。
  const entry = entryOf(cfg, "analyze");
  if (entry && !fixtureDir) {
    const res = run(entry.argv[0], entry.argv.slice(1), { timeoutMs: 18 * 60 * 1000 });
    const entryReport = res.status === 0 ? readJson(cfg.entries.analyze.report, null) : null;
    if (res.status === 0) {
      emitStatus(cfg, TOOL, entryReport?.status || "healthy", { mode: "entry", message: "外部入口通过" });
      writeJson(reportPath, { tool: TOOL, mode: "entry", argv: entry.argv, exit: 0, report: entryReport });
      process.exit(0);
    }
    // 二组 CLI 约定：exit 4 = 无密钥 skipped_no_key。CI 统一映射为 failed + no_api_key + executed=false，
    // 不新增第五态；脚本 exit 0 让工作流能落到 gate（gate 判失败，本轮不提交、不发布）。
    if (res.status === 4) {
      const message = `分析未执行：外部入口报无密钥（${keyName}），executed=false`;
      writeJson(reportPath, { tool: TOOL, status: "failed", mode: "entry", argv: entry.argv, exit: 4, executed: false, reason: "no_api_key", message });
      appendSummary(`### ⚠️ analyze 未执行\n\n${message}\n\n未写入 content/，本轮不提交、不发布。\n`);
      notice(message);
      emitStatus(cfg, TOOL, "failed", { executed: false, reason: "no_api_key", message });
      process.exit(0);
    }
    emitStatus(cfg, TOOL, "failed", { mode: "entry", message: `外部入口退出 ${res.status}` });
    writeJson(reportPath, { tool: TOOL, mode: "entry", argv: entry.argv, exit: res.status, stderr: (res.stderr || "").slice(-2000) });
    process.exit(exitFor(cfg, "validation_failed"));
  }

  // 无密钥：显式「分析未执行」。
  if (!key && !fixtureDir) {
    const message = `分析未执行：${keyName} 未配置（entry=${entry ? "configured" : "none"}）`;
    const report = {
      tool: TOOL,
      status: "failed",
      executed: false,
      reason: "no_api_key",
      keyEnv: keyName,
      message,
      startedAt: new Date(startedAt).toISOString(),
      finishedAt: new Date().toISOString(),
    };
    writeJson(reportPath, report);
    appendSummary(`### ⚠️ analyze 未执行\n\n${message}\n\n未读取候选、未调用模型、未写入 content/，本轮不提交。\n`);
    notice(message);
    emitStatus(cfg, TOOL, "failed", { executed: false, reason: "no_api_key", message });
    process.exit(0);
  }

  // 候选读取。
  const candPath = resolvePath(path.join(cfg.data.candidatesDir || "content/candidates", "candidates.json"));
  const candDoc = fs.existsSync(candPath) ? readJson(candPath) : { candidates: [] };
  const candidates = (candDoc.candidates || []).filter((c) => c.title && c.url);
  if (!candidates.length) {
    const message = "无候选可分析（content/candidates/candidates.json 为空或不存在）";
    writeJson(reportPath, { tool: TOOL, executed: true, status: "no_change", message, candidates: 0 });
    emitStatus(cfg, TOOL, "no_change", { message });
    process.exit(0);
  }

  const screenBudget = new Budget(cfg.budgets.analyze.screen.maxModelCalls, "analyze.screen");
  const deepBudget = new Budget(cfg.budgets.analyze.deepDive.maxModelCalls, "analyze.deepDive");
  const totalBudget = new Budget(cfg.budgets.analyze.maxModelCalls, "analyze.total");

  let selected;
  let incoming;
  if (fixtureDir) {
    const screen = readJson(path.join(fixtureDir, "screen.json"));
    const deep = readJson(path.join(fixtureDir, "events.json"));
    screenBudget.spend(1, "fixture screen");
    totalBudget.spend(1, "fixture screen");
    selected = (screen.selected || []).filter((s) => Number(s.preScore) >= 70);
    incoming = deep;
    const use = Math.min(selected.length, cfg.budgets.analyze.deepDive.maxItems, deep.events?.length ?? 0);
    for (let i = 0; i < use; i += 1) {
      deepBudget.spend(1, "fixture deepDive");
      totalBudget.spend(1, "fixture deepDive");
    }
    incoming = { events: (deep.events || []).slice(0, use), trends: (deep.trends || []).slice(0, use) };
  } else {
    const screen = await callModel(cfg, {
      messages: [
        { role: "system", content: "你是趋势研究筛选器。只输出 JSON：{\"selected\":[{\"dedupeKeyCandidate\":\"...\",\"preScore\":0-100}]}。preScore<70 不要进 deepDive。" },
        {
          role: "user",
          content: JSON.stringify(candidates.slice(0, cfg.budgets.analyze.screen.maxItems).map((c) => ({ k: c.dedupeKeyCandidate, t: c.title, s: c.summary, u: c.url }))),
        },
      ],
    });
    screenBudget.spend(1, "screen");
    totalBudget.spend(1, "screen");
    selected = (screen.selected || []).filter((s) => Number(s.preScore) >= 70);
    const picked = selected.slice(0, cfg.budgets.analyze.deepDive.maxItems);
    incoming = { events: [], trends: [] };
    for (const s of picked) {
      const c = candidates.find((x) => x.dedupeKeyCandidate === s.dedupeKeyCandidate);
      if (!c) continue;
      const out = await callModel(cfg, {
        messages: [
          {
            role: "system",
            content: `你是 ERA 趋势研究员。按 schema ${cfg.schemaVersion} 输出单个事件的 JSON（id/title/summary/dedupeKey/dates/sources/trendLinks/score/firstScore/firstJudgment/scoreHistory/judgmentHistory），方向 ∈ ${(cfg.directions || []).join("/")}。只输出 JSON。`,
          },
          { role: "user", content: JSON.stringify(c) },
        ],
      });
      if (out.event) incoming.events.push(out.event);
      if (Array.isArray(out.trends)) incoming.trends.push(...out.trends);
      deepBudget.spend(1, "deepDive");
      totalBudget.spend(1, "deepDive");
    }
  }

  // 合并 + 校验 + 回滚。
  const before = fs.existsSync(dataFile) ? readJson(dataFile) : skeleton(cfg);
  const ts = new Date().toISOString().replace(/[:.]/g, "-");
  const backup = resolvePath(`artifacts/backup/data-${ts}.json`);
  ensureDir(path.dirname(backup));
  fs.writeFileSync(backup, JSON.stringify(before, null, 2) + "\n", "utf8");

  const doc = JSON.parse(JSON.stringify(before));
  doc.events = Array.isArray(doc.events) ? doc.events : [];
  doc.trends = Array.isArray(doc.trends) ? doc.trends : [];
  const { added, skipped } = mergeEvents(doc, incoming, cfg);
  doc.generatedAt = new Date().toISOString();

  if (args["dry-run"]) {
    writeJson(reportPath, { tool: TOOL, executed: true, dryRun: true, wouldAdd: added, skipped, budgets: { screen: screenBudget.report(), deepDive: deepBudget.report(), total: totalBudget.report() } });
    notice(`dry-run：将新增 ${added.length} 条（未写入）`);
    process.exit(0);
  }

  if (added.length > 0) writeJson(dataFile, doc);
  const v = run(process.execPath, [path.join(SCRIPTS, "validate.mjs"), "--data", dataFile, "--no-delegate", "--report", "reports/validate-after-analyze.json"], {
    cwd: ROOT,
    timeoutMs: 120000,
  });
  if (v.status !== 0) {
    fs.copyFileSync(backup, dataFile);
    const message = `合并后校验失败（exit ${v.status}），已回滚 content/data.json；见 reports/validate-after-analyze.json`;
    writeJson(reportPath, { tool: TOOL, executed: true, status: "failed", message, added, skipped, stderr: (v.stderr || "").slice(-2000) });
    warning(message);
    emitStatus(cfg, TOOL, "failed", { message });
    process.exit(exitFor(cfg, "validation_failed"));
  }

  const status = added.length > 0 ? "healthy" : "no_change";
  const report = {
    tool: TOOL,
    executed: true,
    status,
    startedAt: new Date(startedAt).toISOString(),
    finishedAt: new Date().toISOString(),
    durationMs: Date.now() - startedAt,
    mode: fixtureDir ? "fixture" : key ? "model" : "skip",
    candidates: candidates.length,
    selected: selected.length,
    added,
    skipped,
    backup: relative(backup),
    budgets: { screen: screenBudget.report(), deepDive: deepBudget.report(), total: totalBudget.report() },
    message: added.length ? `新增 ${added.length} 条事件` : "无新增（全部重复或未达阈值），不提交",
  };
  writeJson(reportPath, report);
  emitStatus(cfg, TOOL, status, { message: report.message, added: added.length });
  if (added.length) notice(`analyze 新增 ${added.length} 条事件：${added.join(", ")}`);
  process.exit(0);
}

main().catch((err) => {
  console.error(`[analyze] 未捕获错误：${err.stack || err.message}`);
  emitStatus({ exitCodes: {} }, TOOL, "failed", { message: err.message });
  process.exit(1);
});
