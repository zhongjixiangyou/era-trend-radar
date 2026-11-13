// ERA 验收：9 条路由清单（唯一改动点）
// 明细路由的示例 id 从 src/data 生成，不写死（数据会换一套，写死就是假 404）。
// 可用环境变量覆盖：SAMPLE_EVENT_ID / SAMPLE_TREND_ID / ROUTES_JSON

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const dataDir = resolve(here, '../../src/data');

function firstId(file, what) {
  try {
    const raw = JSON.parse(readFileSync(resolve(dataDir, file), 'utf8'));
    const list = Array.isArray(raw) ? raw : (raw.items ?? raw.events ?? raw.trends ?? []);
    const id = list.find((x) => x && typeof x.id === 'string' && x.id)?.id;
    if (id) return id;
    throw new Error(`${file} 里没有带 id 的条目`);
  } catch (e) {
    throw new Error(
      `无法从 src/data/${file} 取${what}示例 id：${e.message}。` +
        `请用环境变量 SAMPLE_${what.toUpperCase()}_ID 指定，或检查数据文件。`,
    );
  }
}

export const SAMPLE_EVENT_ID = process.env.SAMPLE_EVENT_ID || firstId('events.json', 'event');
export const SAMPLE_TREND_ID = process.env.SAMPLE_TREND_ID || firstId('trends.json', 'trend');

const DEFAULT_ROUTES = [
  { path: '/', name: 'home', kind: 'page' },
  { path: '/events/', name: 'events', kind: 'list' },
  { path: `/events/${SAMPLE_EVENT_ID}/`, name: 'event-detail', kind: 'detail' },
  { path: '/emerging/', name: 'emerging', kind: 'list' },
  { path: '/trends/', name: 'trends', kind: 'list' },
  { path: `/trends/${SAMPLE_TREND_ID}/`, name: 'trend-detail', kind: 'detail' },
  { path: '/forecasts/', name: 'forecasts', kind: 'page' },
  { path: '/china/', name: 'china', kind: 'page' },
  { path: '/archive/', name: 'archive', kind: 'page' },
];

export function getRoutes() {
  if (process.env.ROUTES_JSON) {
    const parsed = JSON.parse(process.env.ROUTES_JSON);
    if (!Array.isArray(parsed) || parsed.length < 1) {
      throw new Error('ROUTES_JSON 必须是非空数组');
    }
    return parsed;
  }
  return DEFAULT_ROUTES;
}

function readJson(file) {
  return JSON.parse(readFileSync(resolve(dataDir, file), 'utf8'));
}

// 与 src/lib/data.ts:74-76 同一口径：一条已发布都没有时整站按内部预览渲染，候选全可见
export function previewMode() {
  const p = readJson('meta.json').preview ?? {};
  return (p.published ?? 0) === 0 && (p.pending ?? 0) > 0;
}

function visibleEvents() {
  const ev = readJson('events.json');
  return previewMode() ? ev : ev.filter((e) => e.status !== 'pending');
}

export function publishedEventCount() {
  return visibleEvents().length;
}

// 搜索用例也从数据生成：标题里截一段中文 + 中间插空格的变体 + 标题里的英文词
export function getSearchCases() {
  const titles = visibleEvents().map((e) => e.title || '').filter(Boolean);
  const cn = titles.map((t) => (t.match(/[\u4e00-\u9fa5]{6,}/) || [])[0]).find(Boolean) || null;
  const all = titles.join(' ');
  return {
    base: cn ? cn.slice(0, 6) : null,
    spaced: cn ? `${cn.slice(0, 3)} ${cn.slice(3, 6)}` : null,
    latin: (all.match(/[A-Za-z]{2,}/) || [])[0] || null,
  };
}
