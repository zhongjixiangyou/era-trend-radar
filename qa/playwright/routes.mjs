// ERA 验收：8 条路由清单（唯一改动点）
// 依据 docs/数据契约.md 与 docs/一组-执行规划.md 第 5 节。
// 第 8 条待主管确认（下注为 trends 详情页）；确认后只改这个文件。
// 可用环境变量覆盖：ROUTES_JSON='[{"path":"/","name":"home"},...]'

export const SAMPLE_EVENT_ID = process.env.SAMPLE_EVENT_ID || 'ev-20260715-chips-tariff';
export const SAMPLE_TREND_ID = process.env.SAMPLE_TREND_ID || 'tr-ai-capex-supply-chain';

const DEFAULT_ROUTES = [
  { path: '/', name: 'home', kind: 'page' },
  { path: '/events/', name: 'events', kind: 'list' },
  { path: `/events/${SAMPLE_EVENT_ID}/`, name: 'event-detail', kind: 'detail' },
  { path: '/trends/', name: 'trends', kind: 'list' },
  { path: `/trends/${SAMPLE_TREND_ID}/`, name: 'trend-detail', kind: 'detail' },
  { path: '/forecasts/', name: 'forecasts', kind: 'page' },
  { path: '/china/', name: 'china', kind: 'page' },
  { path: '/archive/', name: 'archive', kind: 'page' },
];

export function getRoutes() {
  if (process.env.ROUTES_JSON) {
    const parsed = JSON.parse(process.env.ROUTES_JSON);
    if (!Array.isArray(parsed) || parsed.length !== 8) {
      throw new Error(`ROUTES_JSON 必须是 8 条数组，收到 ${Array.isArray(parsed) ? parsed.length : 'non-array'}`);
    }
    return parsed;
  }
  return DEFAULT_ROUTES;
}
