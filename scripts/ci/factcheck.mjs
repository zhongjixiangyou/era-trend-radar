#!/usr/bin/env node
// scripts/ci/factcheck.mjs — 反证检索留痕：每条已发布趋势当轮必须有记录。
// 有检索端点 → 真查；没有/失败 → 写 unavailable + 原因；查到 0 条写「未发现」。绝不允许空字段。
import fs from "node:fs";
import path from "node:path";
import { loadConfig, parseArgs, readJson, writeJson, resolvePath, ensureDir, relative, withRetry, mustRun } from "./lib/common.mjs";
import { emitStatus, exitFor } from "./lib/status.mjs";
import { currentWindow } from "./lib/window.mjs";

const TOOL = "factcheck";

async function search(cfg, query, window) {
  const endpoint = process.env[cfg.search.endpointEnv];
  const key = process.env[cfg.search.apiKeyEnv];
  const res = await withRetry(
    async () => {
      const r = await fetch(endpoint, {
        method: "POST",
        headers: { "content-type": "application/json", ...(key ? { authorization: `Bearer ${key}` } : {}) },
        body: JSON.stringify({ query, window, limit: 10 }),
        signal: AbortSignal.timeout(20000),
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      return await r.json();
    },
    { retries: Number(cfg.budgets?.retry?.retries ?? 2), label: `search ${query.slice(0, 30)}` },
  );
  const hits = (res.hits || res.results || []).map((h) => ({ title: h.title, url: h.url, snippet: (h.snippet || h.content || "").slice(0, 200) }));
  return hits;
}

async function main() {
  const args = parseArgs();
  for (const k of ["data", "fixture", "report"]) if (args[k] === true) process.exit(exitForSafe("usage_error", 2));
  const cfg = loadConfig(args.config || undefined);
  const WIN = currentWindow(cfg);
  const dataFile = resolvePath(args.data || path.join(cfg.data.dir, cfg.data.file));
  if (!fs.existsSync(dataFile)) {
    const message = `反证检索失败：缺少 ${relative(dataFile)}`;
    writeJson(args.report || "reports/factcheck.json", { tool: TOOL, status: "failed", message });
    emitStatus(cfg, TOOL, "failed", { message });
    process.exit(exitFor(cfg, "validation_failed"));
  }
  const doc = readJson(dataFile);
  const fixtureDir = args.fixture ? resolvePath(args.fixture) : null;
  const endpoint = process.env[cfg.search.endpointEnv];
  const configured = Boolean(endpoint || fixtureDir);

  const records = [];
  for (const tr of doc.trends || []) {
    const query = `${tr.name} 反证 质疑 失败 数据`;
    const base = {
      trendId: tr.id,
      name: tr.name,
      query,
      window: { start: WIN.from, end: WIN.to, mode: WIN.mode },
      searchedAt: new Date().toISOString(),
    };
    if (!configured) {
      records.push({ ...base, status: "unavailable", hits: [], hitCount: null, note: `unavailable: ${cfg.search.endpointEnv} 未配置` });
      continue;
    }
    try {
      let hits;
      if (fixtureDir) {
        const f = path.join(fixtureDir, `${tr.id}.json`);
        if (!fs.existsSync(f)) throw new Error(`fixture 缺失 ${relative(f)}`);
        hits = readJson(f).hits || [];
      } else {
        hits = await search(cfg, query, base.window);
      }
      records.push({
        ...base,
        status: "searched",
        hits,
        hitCount: hits.length,
        note: hits.length === 0 ? "未发现" : `命中 ${hits.length} 条`,
      });
    } catch (err) {
      records.push({ ...base, status: "unavailable", hits: [], hitCount: null, note: `unavailable: ${err.message}` });
    }
  }

  const unavailable = records.filter((r) => r.status === "unavailable").length;
  const empty = records.filter((r) => r.status === "searched" && r.hitCount === 0).length;
  const report = {
    tool: TOOL,
    status: unavailable === records.length && records.length > 0 ? "failed" : unavailable > 0 ? "degraded" : "healthy",
    configured,
    endpointEnv: cfg.search.endpointEnv,
    totals: { trends: records.length, searched: records.length - unavailable, unavailable, notFound: empty },
    records,
    finishedAt: new Date().toISOString(),
  };
  const jsonPath = args.report || "reports/factcheck.json";
  writeJson(jsonPath, report);

  const md = [
    `# 反证检索留痕 ${new Date().toISOString().slice(0, 10)}`,
    "",
    `窗口 ${WIN.from} ~ ${WIN.to} · 趋势 ${records.length} · 已查 ${records.length - unavailable} · 不可用 ${unavailable} · 未发现 ${empty}`,
    "",
    "| 趋势 | 关键词 | 命中 | 留痕 |",
    "|---|---|---|---|",
    ...records.map((r) => `| ${r.trendId} ${r.name} | ${r.query} | ${r.hitCount ?? "—"} | ${r.note}${r.hits?.length ? `：${r.hits.map((h) => h.url).join("；")}` : ""} |`),
  ].join("\n");
  const mdPath = path.join(path.dirname(resolvePath(jsonPath)), "factcheck.md");
  fs.writeFileSync(mdPath, md + "\n", "utf8");

  emitStatus(cfg, TOOL, report.status, {
    message: `反证检索：${records.length} 条趋势，不可用 ${unavailable}，未发现 ${empty}`,
    report: relative(jsonPath),
  });
  console.log(`[factcheck] ${report.status} · ${records.length} 趋势 · 不可用 ${unavailable}`);
  process.exit(report.status === "failed" ? exitFor(cfg, "source_failed") : 0);
}

function exitForSafe(key, dflt) {
  try {
    return Number(loadConfig()?.exitCodes?.[key] ?? dflt);
  } catch {
    return dflt;
  }
}

main().catch((err) => {
  console.error(`[factcheck] ${err.stack || err.message}`);
  process.exit(1);
});
