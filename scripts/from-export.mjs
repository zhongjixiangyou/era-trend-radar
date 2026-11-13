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
import { readFileSync, writeFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { join, resolve } from 'node:path';

const args = process.argv.slice(2);
const dirArg = args.indexOf('--dir');
const outArg = args.indexOf('--out');
const SRC = dirArg >= 0 ? args[dirArg + 1] : join(homedir(), '.dsh/era-work/data');
const OUT = resolve(process.cwd(), outArg >= 0 ? args[outArg + 1] : 'src/data');

const read = (name) => JSON.parse(readFileSync(join(SRC, name), 'utf8'));
const day = (iso) => (iso ? String(iso).slice(0, 10) : null);

const CATEGORY = { 中国经济与产业: 'industry', 全球宏观: 'economy', 未来趋势: 'technology' };
const REGION = { 中国: 'china', 全球: 'global', 美国: 'us', 中东: 'asia', 欧洲: 'europe', 亚洲: 'asia' };
const CRED = { multi_independent: 'high', single_primary: 'medium', secondary_only: 'low', unverified: 'low' };

const srcEvents = read('events.json');
const srcTrends = read('trends.json');
const srcForecasts = read('forecasts.json');

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

const dates = events.map((e) => e.date).filter(Boolean).sort();
// ISO 周号，用作期号；不编造「第几期」的累计数。
const d0 = new Date((dates[dates.length - 1] ?? new Date().toISOString().slice(0, 10)) + 'T00:00:00Z');
const dayNum = (d0.getUTCDay() + 6) % 7;
d0.setUTCDate(d0.getUTCDate() - dayNum + 3);
const weekNo = 1 + Math.round((d0 - Date.UTC(d0.getUTCFullYear(), 0, 4)) / 604800000);
const meta = {
  site: 'ERA · 时代趋势雷达',
  generatedAt: srcEvents.generatedAt ?? new Date().toISOString(),
  window: { from: dates[0] ?? null, to: dates[dates.length - 1] ?? null },
  counts: {
    events: events.length,
    trends: trends.length,
    forecasts: forecasts.length,
    published: events.filter((e) => e.status !== 'pending').length,
    pending: events.filter((e) => e.status === 'pending').length,
  },
  preview: {
    published: events.filter((e) => e.status !== 'pending').length,
    pending: events.filter((e) => e.status === 'pending').length,
  },
  dataSource: 'content/ → export_astro.py（v1）',
  projection: 'scripts/from-export.mjs',
  runStatus: srcEvents.gates ? 'ok' : 'unknown',
  issue: `第 ${String(weekNo).padStart(2, '0')} 期`,
  editorNote:
    '本页数据由 content/ 经 export_astro.py 导出后投影生成，只做展示，不是权威源；' +
    '条目数少于半年实况是核验进度，不是当月没有发生。',
};

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
    },
    null,
    2
  )
);
