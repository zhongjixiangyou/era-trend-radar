import events from '../data/events.json';
import trends from '../data/trends.json';
import forecasts from '../data/forecasts.json';
import snapshots from '../data/snapshots.json';
import meta from '../data/meta.json';

export type Score = {
  structural: number; scope: number; signal: number;
  novelty: number; timeliness: number; cross: number;
  /** 当前总分：契约规定的唯一来源。缺失时才回退到公式计算。 */
  total?: number;
};

export type EraEvent = {
  id: string; title: string; date: string; reportedAt?: string; collectedAt?: string;
  region: string; category: string; summary: string; facts: string[];
  metrics?: { label: string; value: string; note?: string }[];
  scores: Score; credibility: 'high' | 'medium' | 'low';
  status: 'confirmed' | 'partial' | 'pending';
  sources: { name: string; url: string; date?: string; official?: boolean }[];
  trends: { id: string; effect: 'support' | 'weaken' | 'challenge'; reason?: string }[];
  timeline?: { date: string; title: string; note?: string }[];
  scoreHistory?: { date: string; total: number; reason?: string }[];
  watch?: string; backfill?: boolean;
};

export type EraTrend = {
  id: string; title: string; thesis: string; domain: string;
  firstSeen: string; status: 'emerging' | 'strengthening' | 'stable' | 'weakening' | 'falsified';
  confidence: number; indicators: string[]; judgment: string; verifyBy?: string;
  revisions: { date: string; from: number | null; to: number; reason?: string }[];
};

export type EraForecast = {
  id: string; claim: string; madeAt: string; dueBy: string; confidence: number;
  status: 'open' | 'supported' | 'weakened' | 'falsified' | 'unresolved';
  trendId?: string; support: string[]; counter: string[];
  history: { date: string; confidence: number; reason?: string }[];
};

export type EraSnapshot = {
  id: string; period: 'weekly' | 'monthly'; date: string; title: string; summary: string;
  eventIds: string[]; trendChanges?: { trendId: string; from: string; to: string; reason?: string }[];
};

export const allEvents = events as EraEvent[];
export const allTrends = trends as EraTrend[];
export const allForecasts = forecasts as EraForecast[];
export const allSnapshots = snapshots as EraSnapshot[];
export const siteMeta = meta as {
  site: string; generatedAt: string; window: { from: string; to: string };
  counts: { events: number; trends: number; forecasts: number; published?: number; pending?: number };
  preview?: { published: number; pending: number };
  editorNote?: string; runStatus?: string; lastSuccessfulRun?: string; issue?: string;
};

export function total(s: Score): number {
  // scores.total 是唯一来源；没有才回退算（旧数据/外部导入）。
  if (typeof s.total === 'number') return s.total;
  // v1 口径：六维本身就是分项得分（满分 25/20/20/15/10/10），总分直接相加，无加权、无半值问题。
  return s.structural + s.scope + s.signal + s.novelty + s.timeliness + s.cross;
}

export const scoreLabels: [keyof Score, string][] = [
  ['structural', '结构性影响'],
  ['scope', '影响范围'],
  ['signal', '趋势信号'],
  ['novelty', '信息增量'],
  ['timeliness', '时效性'],
  ['cross', '跨领域'],
];

export const sortedEvents = [...allEvents].sort((a, b) => b.date.localeCompare(a.date));
/**
 * 只要数据集里还有 pending，整站就按「内部预览」渲染：候选可见，但必须挂横幅 + noindex。
 * 早先判据是「published===0」，导致 3 条已确认 + 8 条候选时会掉进非预览态，
 * 候选内容静默露出而没有任何提示——那正是半发布状态，不能算正式站点。
 */
export const previewMode = (siteMeta.preview?.pending ?? 0) > 0;
export const publishedEvents = sortedEvents.filter((e) => previewMode || e.status !== 'pending');
export const trendById = new Map(allTrends.map((t) => [t.id, t]));
export const eventById = new Map(allEvents.map((e) => [e.id, e]));

export function eventsForTrend(trendId: string) {
  return sortedEvents.filter((e) => e.trends.some((t) => t.id === trendId));
}

export function fmtDate(d: string | null | undefined): string {
  if (!d) return '暂无';
  const [y, m, day] = d.split('-');
  return `${y}.${m}.${day}`;
}

export function fmtCnDate(d: string): string {
  const [y, m, day] = d.split('-');
  return `${y} 年 ${Number(m)} 月 ${Number(day)} 日`;
}

export function weekday(d: string): string {
  const names = ['日', '一', '二', '三', '四', '五', '六'];
  return `星期${names[new Date(d + 'T00:00:00').getDay()]}`;
}

export const regionLabel: Record<string, string> = {
  global: '全球', china: '中国', us: '美国', europe: '欧洲', asia: '亚洲', other: '其他',
};

export const categoryLabel: Record<string, string> = {
  technology: '科技', economy: '经济', industry: '产业', society: '社会',
  policy: '政策', energy: '能源', demography: '人口',
};

export const effectLabel: Record<string, string> = {
  support: '支持', weaken: '削弱', challenge: '挑战',
};

export const statusLabel: Record<string, string> = {
  emerging: '初现', strengthening: '强化中', stable: '稳定',
  weakening: '走弱', falsified: '被证伪',
};

export const forecastStatusLabel: Record<string, string> = {
  open: '待验证', supported: '获支持', weakened: '被削弱',
  falsified: '被证伪', unresolved: '到期未决',
};
