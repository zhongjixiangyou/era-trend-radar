#!/usr/bin/env node
/**
 * 把二组 v1 导出（era-work/data/{events,trends,forecasts}.json，content/ 派生）
 * 转成站点投影 src/data/*.json。
 *
 * 规则（Codex 最终规划 §2）：
 * - 唯一权威是 repo/content/，这里只做投影，不新增事实、不猜字段。
 * - 永久 ID 原样保留（ev_* / tr_* / fc_*），不再另造 ev-2026*。
 * - 缺失值一律 null，页面显示「暂无」，不补 0。
 * - 每次运行打印计数，便于源→导出对照。
 *
 * 用法：node scripts/from-export.mjs [--dir ~/.dsh/era-work/data]
 */
import { readFileSync, writeFileSync, existsSync, readdirSync } from 'node:fs';
import { homedir } from 'node:os';
import { basename, dirname, join, resolve } from 'node:path';

const args = process.argv.slice(2);
const dirArg = args.indexOf('--dir');
const outArg = args.indexOf('--out');
const SRC = dirArg >= 0 ? args[dirArg + 1] : join(homedir(), '.dsh/era-work/data');
const OUT = resolve(process.cwd(), outArg >= 0 ? args[outArg + 1] : 'src/data');
// public = 只出已发布（发布门禁默认）；preview = 内部预览，含 pending。
const modeArg = args.indexOf('--mode');
const MODE = modeArg >= 0 ? args[modeArg + 1] : 'public';
if (MODE !== 'public' && MODE !== 'preview') {
  console.error(`--mode 只能是 public|preview，收到 ${MODE}`);
  process.exit(2);
}

// 公开导出在没有 published 时可能整份不写文件（不导出空骨架），按空集合处理。
const read = (name, fallback) => {
  const p = join(SRC, name);
  if (!existsSync(p)) return fallback;
  return JSON.parse(readFileSync(p, 'utf8'));
};
const day = (iso) => (iso ? String(iso).slice(0, 10) : null);

const CATEGORY = { 中国经济与产业: 'industry', 全球宏观: 'economy', 未来趋势: 'technology' };
const REGION = { 中国: 'china', 全球: 'global', 美国: 'us', 中东: 'asia', 欧洲: 'europe', 亚洲: 'asia' };
const CRED = { multi_independent: 'high', single_primary: 'medium', secondary_only: 'low', unverified: 'low' };

// 公开导出没有 pending，所以候选数只能从 content/ 权威源直接数，不能从导出推。
const contentPending = (() => {
  // 优先用二组的 content/MANIFEST.json（它按状态统计过）；没有清单再逐文件数。
  try {
    const mf = JSON.parse(readFileSync(resolve(process.cwd(), 'content/MANIFEST.json'), 'utf8'));
    const ev = mf?.counts?.events;
    if (ev && typeof ev.pending === 'number') return ev.pending;
  } catch (_) {}
  let n = 0;
  for (const kind of ['events', 'trends', 'forecasts']) {
    const dir = resolve(process.cwd(), 'content', kind);
    if (!existsSync(dir)) continue;
    for (const name of readdirSync(dir)) {
      if (!name.endsWith('.json')) continue;
      try {
        const doc = JSON.parse(readFileSync(join(dir, name), 'utf8'));
        if (doc.status && doc.status !== 'published') n++;
      } catch (_) {}
    }
  }
  return n;
})();

const srcEvents = read('events.json', { events: [], generatedAt: null, gates: null });
const srcTrends = read('trends.json', { trends: [] });
const srcForecasts = read('forecasts.json', { forecasts: [] });

const events = srcEvents.events.map((e) => {
  // 发布状态只认导出里的 status，不拿 verification 反推：核验通过 ≠ 已发布。
  const published = e.status === 'published';
  const revisions = Array.isArray(e.revisions) ? e.revisions : [];
  return {
    id: e.id,
    title: e.title,
    date: day(e.occurredAt),
    reportedAt: day(e.firstReportedAt),
    collectedAt: day(e.collectedAt),
    region: REGION[e.region] ?? 'other',
    category: CATEGORY[e.category] ?? 'economy',
    summary: e.summary,
    analysis: e.analysis ?? null,
    facts: e.facts ?? [],
    metrics: e.metrics?.items ?? [],
    scores: {
      structural: e.scores.structural,
      scope: e.scores.scope,
      signal: e.scores.signal,
      novelty: e.scores.delta,
      timeliness: e.scores.timeliness,
      cross: e.scores.crossDomain,
      total: e.scores.total,
      rationale: e.scores.rationale ?? null,
    },
    credibility: CRED[e.evidenceGrade] ?? 'low',
    evidenceGrade: e.evidenceGrade ?? null,
    status: published ? 'confirmed' : 'pending',
    sources: (e.sources ?? []).map((s) => ({
      name: s.publisher ?? s.title ?? s.url,
      url: s.url,
      date: day(s.publishedAt),
      official: s.type === 'primary',
    })),
    trends: (e.trendLinks ?? []).map((l) => ({
      id: l.trendId,
      effect: l.direction,
      reason: l.reason ?? null,
    })),
    timeline: revisions.map((r) => ({ date: day(r.at), title: r.action ?? r.kind ?? '修订', note: r.note ?? null })),
    scoreHistory: [{ date: day(e.collectedAt), total: e.scores.total }],
    watch: e.display?.watch ?? null,
    backfill: Boolean(e.retro?.isRetro),
  };
});

// public 构建必须过滤 pending：HTML、图表、JSON、搜索索引都不许出现候选 ID。
const allEventCount = events.length;
const publishedCount = events.filter((e) => e.status !== 'pending').length;
const publicEvents = MODE === 'public' ? events.filter((e) => e.status !== 'pending') : events;
const publicIds = new Set(publicEvents.map((e) => e.id));

const stripDict = (s) =>
  String(s)
    .replace(/^\{'name':\s*'/, '')
    .replace(/'note':\s*'/, ' — ')
    .replace(/'note':\s*"/, ' — ')
    .replace(/'?\}$/, '')
    .replace(/^\{'text':\s*'/, '')
    .replace(/'\}$/, '')
    .replace(/^"|"$/g, '');

const trends = srcTrends.trends.map((t) => {
  const revisions = Array.isArray(t.revisions) ? t.revisions : [];
  return {
    id: t.id,
    title: String(t.thesis).split(/[，,。；;]/)[0].slice(0, 30),
    thesis: t.thesis,
    domain: 'global',
    firstSeen: day(t.firstSeenAt),
    status: t.stage,
    confidence: null,
    evidenceCount: t.viz?.evidenceCount ?? (t.evidenceRefs ?? []).length,
    supportCount: t.supportCount ?? null,
    counterCount: t.counterCount ?? null,
    indicators: (t.indicators ?? []).map(stripDict),
    judgment: t.currentJudgment ?? '',
    verifyBy: day(t.reviewBy),
    revisions: revisions.map((r) => ({
      date: day(r.at),
      from: null,
      to: null,
      reason: r.note ?? null,
    })),
  };
});

const forecasts = srcForecasts.forecasts.map((f) => ({
  id: f.id,
  claim: f.statement,
  madeAt: day(f.createdAt),
  dueBy: day(f.resolveBy),
  confidence: typeof f.probability === 'number' ? Math.round(f.probability * 100) : null,
  status: f.status ?? 'open',
  trendId: (f.linkedTrendIds ?? [])[0] ?? null,
  support: f.evidenceRefs ?? [],
  counter: (f.counterEvidence ?? []).map(stripDict),
  history: (f.revisions ?? []).map((r) => ({
    date: day(r.at),
    confidence: typeof f.probability === 'number' ? Math.round(f.probability * 100) : null,
    reason: r.note ?? null,
  })),
}));

// 公开模式：只保留被已发布事件引用的趋势；没有已发布事件就没有公开趋势与判断。
const publicTrends = MODE === 'public' ? trends.filter((t) => publicEvents.some((e) => e.trends.some((l) => l.id === t.id))) : trends;
const publicTrendIds = new Set(publicTrends.map((t) => t.id));
const publicForecasts = MODE === 'public' ? forecasts.filter((f) => f.trendId && publicTrendIds.has(f.trendId)) : forecasts;
if (MODE === 'public') {
  events.length = 0;
  events.push(...publicEvents);
  trends.length = 0;
  trends.push(...publicTrends);
  forecasts.length = 0;
  forecasts.push(...publicForecasts);
}

const dates = events.map((e) => e.date).filter(Boolean).sort();
// ISO 周号，用作期号；不编造「第几期」的累计数。
const d0 = dates.length ? new Date(dates[dates.length - 1] + 'T00:00:00Z') : null;
let weekNo = null;
if (d0) {
  const dayNum = (d0.getUTCDay() + 6) % 7;
  d0.setUTCDate(d0.getUTCDate() - dayNum + 3);
  weekNo = 1 + Math.round((d0 - Date.UTC(d0.getUTCFullYear(), 0, 4)) / 604800000);
}
const meta = {
  site: 'ERA · 时代趋势雷达',
  generatedAt: srcEvents.generatedAt ?? new Date().toISOString(),
  window: { from: dates[0] ?? null, to: dates[dates.length - 1] ?? null },
  counts: {
    events: events.length,
    trends: trends.length,
    forecasts: forecasts.length,
    published: publishedCount,
    pending: contentPending,
  },
  preview: {
    published: publishedCount,
    pending: contentPending,
  },
  mode: MODE,
  dataSource: 'content/ → export_astro.py（v1）',
  projection: 'scripts/from-export.mjs',
  runStatus: srcEvents.gates ? 'ok' : 'unknown',
  issue: weekNo ? `第 ${String(weekNo).padStart(2, '0')} 期` : '暂无已发布事件',
  editorNote:
    '本页数据由 content/ 经 export_astro.py 导出后投影生成，只做展示，不是权威源；' +
    '条目数少于半年实况是核验进度，不是当月没有发生。',
};

// 防呆：曾有人用 public 模式直接写 src/data，published=0 时静默写成 0 条，
// 站点、QA、构建各量到一套数据。写盘前拦一道——只拦「空结果」，缩量是发布链的正常行为。
const writingSrcData = basename(OUT) === 'data' && basename(dirname(OUT)) === 'src';
if (writingSrcData && MODE === 'public' && !args.includes('--force') && events.length === 0) {
  console.error('[from-export] 中止：public 模式导出 0 条事件，不写盘。published=0 时应改用 --mode preview。');
  process.exit(1);
}

writeFileSync(join(OUT, 'events.json'), JSON.stringify(events, null, 2) + '\n');
writeFileSync(join(OUT, 'trends.json'), JSON.stringify(trends, null, 2) + '\n');
writeFileSync(join(OUT, 'forecasts.json'), JSON.stringify(forecasts, null, 2) + '\n');
writeFileSync(join(OUT, 'meta.json'), JSON.stringify(meta, null, 2) + '\n');

console.log(
  JSON.stringify(
    {
      source: SRC,
      events: events.length,
      trends: trends.length,
      forecasts: forecasts.length,
      range: meta.window,
      backfill: events.filter((e) => e.backfill).length,
      pending: events.filter((e) => e.status === 'pending').length,
      mode: MODE,
    },
    null,
    2
  )
);
