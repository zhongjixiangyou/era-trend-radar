#!/usr/bin/env node
// scripts/ci/monthly-review.mjs — 月报：只读 content/data.json，写 reports/，不改已发布判断。
import fs from "node:fs";
import path from "node:path";
import { loadConfig, parseArgs, readJson, writeJson, resolvePath, relative } from "./lib/common.mjs";
import { emitStatus, exitFor } from "./lib/status.mjs";

const TOOL = "monthly-review";

function main() {
  const args = parseArgs();
  const cfg = loadConfig(args.config || undefined);
  const dataFile = resolvePath(args.data || path.join(cfg.data.dir, cfg.data.file));
  if (!fs.existsSync(dataFile)) {
    const message = `月报失败：缺少 ${relative(dataFile)}`;
    emitStatus(cfg, TOOL, "failed", { message });
    process.exit(exitFor(cfg, "validation_failed"));
  }
  const doc = readJson(dataFile);
  const month = args.month || (doc.events?.map((e) => e.dates?.occurred?.slice(0, 7)).sort().pop() ?? new Date().toISOString().slice(0, 7));
  const events = doc.events || [];
  const inMonth = events.filter((e) => e.dates?.occurred?.startsWith(month));
  const trends = doc.trends || [];
  const revised = events.filter((e) => (e.judgmentHistory || []).length > 1);
  const rescored = events.filter((e) => (e.scoreHistory || []).length > 1);

  let gaps = [];
  const fact = resolvePath("reports/factcheck.json");
  if (fs.existsSync(fact)) {
    try {
      gaps = (readJson(fact).records || []).filter((r) => r.status !== "searched" || r.hitCount === 0);
    } catch {
      gaps = [{ note: "factcheck.json 无法解析" }];
    }
  }

  const md = [
    `# ERA 月报 ${month}`,
    "",
    `- 数据文件：\`${relative(dataFile)}\`（只读，不改已发布判断）`,
    `- 事件总数：${events.length}，本月新增：${inMonth.length}`,
    `- 趋势总数：${trends.length}`,
    `- 本月评分修订：${rescored.length} 条；判断修订：${revised.length} 条`,
    "",
    "## 本月事件",
    ...(inMonth.length ? inMonth.map((e) => `- ${e.dates.occurred} · ${e.title}（${e.score.displayedTotal} 分）`) : ["- 无"]),
    "",
    "## 需要复核的缺口",
    ...(gaps.length ? gaps.map((g) => `- ${g.trendId || "factcheck"}：${g.note}`) : ["- 无（反证检索均已留痕）"]),
    "",
    "> 月报只提交到 reports/；正式并入数据走 analyze，且首次评分/判断不可覆盖。",
  ].join("\n");

  const outBase = path.join(cfg.artifacts.reportsDir, `monthly-review-${month}`);
  fs.mkdirSync(path.dirname(resolvePath(`${outBase}.md`)), { recursive: true });
  const mdPath = resolvePath(`${outBase}.md`);
  const prev = fs.existsSync(mdPath) ? fs.readFileSync(mdPath, "utf8") : null;
  fs.writeFileSync(mdPath, md + "\n", "utf8");
  writeJson(`${outBase}.json`, {
    tool: TOOL,
    month,
    dataFile: relative(dataFile),
    totals: { events: events.length, inMonth: inMonth.length, trends: trends.length, rescored: rescored.length, revised: revised.length, gaps: gaps.length },
    generatedAt: new Date().toISOString(),
  });

  const changed = prev !== md + "\n";
  emitStatus(cfg, TOOL, changed ? "healthy" : "no_change", {
    message: changed ? `月报写入 ${relative(mdPath)}（${inMonth.length} 条本月事件）` : "月报无变化，no_change",
  });
  process.exit(0);
}

try {
  main();
} catch (err) {
  console.error(`[monthly-review] ${err.stack || err.message}`);
  process.exit(1);
}
