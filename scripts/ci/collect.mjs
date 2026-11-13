#!/usr/bin/env node
// scripts/ci/collect.mjs — 采集层：拉 RSS/Atom 或跑二组外部入口，只写候选 content/candidates/，
// 绝不写 content/data.json、绝不打分。全源失败 → 状态 failed + 退出 3(SOURCE_FAILED)。
import fs from "node:fs";
import path from "node:path";
import {
  ROOT,
  loadConfig,
  parseArgs,
  readJson,
  writeJson,
  resolvePath,
  ensureDir,
  relative,
  sha256,
  withRetry,
  run,
  notice,
  warning,
} from "./lib/common.mjs";
import { emitStatus, exitFor } from "./lib/status.mjs";
import { entryOf } from "./lib/entry.mjs";

const TOOL = "collect";

function usage(msg) {
  console.error(`[collect] 用法错误：${msg}`);
  console.error("  node scripts/ci/collect.mjs [--fixture fixtures/feeds] [--report reports/collect.json] [--dry-run]");
  process.exit(exitForSafe("usage_error", 2));
}
function exitForSafe(key, dflt) {
  try {
    return Number(loadConfig()?.exitCodes?.[key] ?? dflt);
  } catch {
    return dflt;
  }
}

function stripTags(s) {
  return String(s || "")
    .replace(/<!\[CDATA\[([\s\S]*?)\]\]>/g, "$1")
    .replace(/<[^>]+>/g, "")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&amp;/g, "&")
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&nbsp;/g, " ")
    .trim();
}

function pick(block, tag) {
  const re = new RegExp(`<${tag}[^>]*>([\\s\\S]*?)</${tag}>`, "i");
  const m = block.match(re);
  return m ? stripTags(m[1]) : "";
}

function pickLink(block) {
  const atom = block.match(/<link[^>]*href=["']([^"']+)["'][^>]*\/?>/i);
  if (atom) return atom[1];
  return pick(block, "link");
}

export function parseFeed(xml, sourceId) {
  const chunks = xml.match(/<(item|entry)\b[\s\S]*?<\/\1>/gi) || [];
  return chunks
    .map((b) => {
      const title = pick(b, "title");
      const url = pickLink(b);
      const published = pick(b, "pubDate") || pick(b, "published") || pick(b, "updated") || pick(b, "dc:date");
      const summary = pick(b, "description") || pick(b, "summary") || pick(b, "content");
      return {
        title,
        url,
        publishedAt: published ? new Date(published).toISOString() : null,
        summary: summary.slice(0, 300),
        sourceId,
      };
    })
    .filter((x) => x.title && /^https?:\/\//i.test(x.url));
}

async function fetchFeed(url, cfg) {
  const ttlHours = Number(cfg.cache?.fetchTtlHours ?? 12);
  const key = sha256(url).slice(0, 16);
  const cacheDir = resolvePath(cfg.cache?.fetchDir || ".cache/era-fetch");
  const bodyPath = path.join(cacheDir, `${key}.xml`);
  const metaPath = path.join(cacheDir, `${key}.json`);
  ensureDir(cacheDir);
  if (fs.existsSync(bodyPath) && fs.existsSync(metaPath)) {
    const meta = readJson(metaPath);
    const ageH = (Date.now() - Date.parse(meta.fetchedAt)) / 3600000;
    if (ageH <= ttlHours) return { xml: fs.readFileSync(bodyPath, "utf8"), cached: true };
  }
  const xml = await withRetry(
    async () => {
      const res = await fetch(url, {
        headers: { "user-agent": "era-trend-radar-ci/0.3 (+https://github.com/zhongjixiangyou/era-trend-radar)" },
        signal: AbortSignal.timeout(15000),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      return await res.text();
    },
    { retries: Number(cfg.budgets?.retry?.retries ?? 2), label: `fetch ${url}` },
  );
  fs.writeFileSync(bodyPath, xml, "utf8");
  writeJson(metaPath, { url, fetchedAt: new Date().toISOString(), bytes: Buffer.byteLength(xml) });
  return { xml, cached: false };
}

async function main() {
  const startedAt = Date.now();
  const args = parseArgs();
  if (args.fixture === true || args.report === true) usage("--fixture/--report 需要一个值");
  let cfg;
  try {
    cfg = loadConfig(args.config || undefined);
  } catch (err) {
    usage(`配置读取失败：${err.message}`);
  }

  // 1) 外部入口优先（二组提供时）。
  const builtinFetch = args["builtin-fetch"] === true || Boolean(args.fixture);
  const entry = entryOf(cfg, "collect");
  if (!builtinFetch && !entry) {
    const message = "采集入口未配置：真实 RSS/API 入口未就绪，本轮按 failed 处理，不写候选、不提交";
    emitStatus(cfg, TOOL, "failed", { reason: "no_fetch_entry", message });
    writeJson(args.report || "reports/collect.json", { tool: TOOL, status: "failed", reason: "no_fetch_entry", message, finishedAt: new Date().toISOString() });
    process.exit(exitFor(cfg, "source_failed"));
  }
  if (entry && !builtinFetch) {
    const res = run(entry.argv[0], entry.argv.slice(1), { timeoutMs: 15 * 60 * 1000 });
    const report = res.status === 0 || res.status === 1 ? readJson(cfg.entries.collect.report, null) : null;
    // 真实入口退出码（二组约定）：0 = 成功（可能 no_change）、1 = 部分源失败 degraded、3 = 全源失败。
    // 其它非 0（含 2 用法错误）一律 failed，不做豁免。
    const exitDegraded = exitFor(cfg, "validation_failed"); // 1
    const exitSource = exitFor(cfg, "source_failed"); // 3
    let status;
    let code;
    let reason;
    if (res.status === 0) {
      status = ["no_change", "degraded", "healthy"].includes(report?.status) ? report.status : "healthy";
      code = 0;
    } else if (res.status === exitDegraded) {
      status = "degraded";
      code = 0; // degraded 仍提交候选，但缺口写进 report
      reason = report?.reason || "some_sources_failed";
    } else if (res.status === exitSource) {
      status = "failed";
      code = exitSource;
      reason = report?.reason || "all_sources_failed";
    } else {
      status = "failed";
      code = exitFor(cfg, "validation_failed");
      reason = `unexpected_exit_${res.status}`;
    }
    const message = `外部入口 ${entry.argv.join(" ")} 退出 ${res.status} → ${status}${reason ? `（${reason}）` : ""}`;
    emitStatus(cfg, TOOL, status, { mode: "entry", exit: res.status, reason, message, argv: entry.argv });
    writeJson(args.report || cfg.entries.collect.report || "reports/collect.json", {
      tool: TOOL,
      status,
      mode: "entry",
      argv: entry.argv,
      exit: res.status,
      reason,
      report,
      stdout: (res.stdout || "").slice(-2000),
      stderr: (res.stderr || "").slice(-2000),
      finishedAt: new Date().toISOString(),
    });
    if (status === "degraded") warning(message);
    if (status === "failed") process.exit(code);
    process.exit(0);
  }

  // 2) 内置 RSS 采集（fixture 模式不联网）。
  const fixtureDir = args.fixture ? resolvePath(args.fixture) : null;
  if (fixtureDir && !fs.existsSync(fixtureDir)) usage(`fixture 目录不存在: ${fixtureDir}`);
  const sources = Array.isArray(cfg.sources) ? cfg.sources.filter((s) => s.kind === "rss") : [];
  if (!sources.length) usage("config.sources 里没有 kind=rss 的来源");

  const results = [];
  const items = [];
  for (const src of sources) {
    try {
      let xml;
      let cached = false;
      if (fixtureDir) {
        const f = path.join(fixtureDir, `${src.id}.xml`);
        if (!fs.existsSync(f)) throw new Error(`fixture 缺失 ${relative(f)}`);
        xml = fs.readFileSync(f, "utf8");
      } else {
        ({ xml, cached } = await fetchFeed(src.url, cfg));
      }
      const parsed = parseFeed(xml, src.id);
      results.push({ id: src.id, url: src.url, status: parsed.length ? "ok" : "empty", count: parsed.length, cached });
      items.push(...parsed);
    } catch (err) {
      results.push({ id: src.id, url: src.url, status: "failed", count: 0, error: err.message });
    }
  }

  const okSources = results.filter((r) => r.status === "ok").length;
  const failedSources = results.filter((r) => r.status === "failed").length;

  // 去重：URL 归一 + 标题归一。
  const seen = new Set();
  const candidates = [];
  for (const it of items) {
    const k = `${it.url.replace(/[?#].*$/, "")}|${it.title.toLowerCase().replace(/\s+/g, "")}`;
    if (seen.has(k)) continue;
    seen.add(k);
    candidates.push({ ...it, dedupeKeyCandidate: k });
  }
  candidates.sort((a, b) => (b.publishedAt || "").localeCompare(a.publishedAt || ""));

  const outPath = resolvePath(path.join(cfg.data.candidatesDir || "content/candidates", "candidates.json"));
  const prev = fs.existsSync(outPath) ? readJson(outPath, { candidates: [] }) : { candidates: [] };
  const prevKeys = new Set((prev.candidates || []).map((c) => c.dedupeKeyCandidate));
  const added = candidates.filter((c) => !prevKeys.has(c.dedupeKeyCandidate));

  const report = {
    tool: TOOL,
    mode: fixtureDir ? "fixture" : "rss",
    startedAt: new Date(startedAt).toISOString(),
    finishedAt: new Date().toISOString(),
    durationMs: Date.now() - startedAt,
    sources: results,
    totals: { fetched: items.length, unique: candidates.length, added: added.length },
    status: "healthy",
  };

  if (okSources === 0) {
    report.status = "failed";
    report.message = "全部来源失败，标记不可用；不写候选、不提交";
    emitStatus(cfg, TOOL, "failed", { message: report.message, sources: results });
    writeJson(args.report || "reports/collect.json", report);
    process.exit(exitFor(cfg, "source_failed"));
  }

  const changed = added.length > 0 || !fs.existsSync(outPath);
  if (changed && !args["dry-run"]) {
    writeJson(outPath, {
      schemaVersion: cfg.schemaVersion,
      generatedAt: new Date().toISOString(),
      candidates,
    });
    notice(`候选写入 ${relative(outPath)}：新增 ${added.length} / 共 ${candidates.length}`);
  }

  report.status = failedSources > 0 ? "degraded" : changed ? "healthy" : "no_change";
  report.message =
    report.status === "degraded"
      ? `${failedSources}/${results.length} 个来源失败，已产出 ${added.length} 条新候选，缺口写报告`
      : report.status === "no_change"
        ? "无新增候选，不提交"
        : `新增 ${added.length} 条候选`;
  if (report.status === "degraded") warning(report.message);
  emitStatus(cfg, TOOL, report.status, { message: report.message, added: added.length, failedSources });
  writeJson(args.report || "reports/collect.json", report);

  const bad = results.filter((r) => r.status !== "ok");
  bad.forEach((r) => warning(`来源 ${r.id} ${r.status}${r.error ? `：${r.error}` : ""}`));
  process.exit(0);
}

main().catch((err) => {
  console.error(`[collect] 未捕获错误：${err.stack || err.message}`);
  process.exit(exitForSafe("validation_failed", 1));
});
