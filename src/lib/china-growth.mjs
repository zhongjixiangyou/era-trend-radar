/**
 * 中国产业页的增速分档口径（唯一实现，页面和校验脚本共用）。
 *
 * 数据字段以一组落地的 src/data/china.json 为准：
 *   samples[] = { name, scope, revenueGrowth, profitGrowth, note?, eventId? }
 *   revenueGrowth / profitGrowth 允许为 null（该口径未公布），此时不参与四象限。
 *
 * 判定只用同比增速的正负：
 *   收入 > 0 且 利润 > 0  → 同步增长 sync
 *   收入 > 0 且 利润 ≤ 0  → 增收压利 margin
 *   收入 ≤ 0 且 利润 > 0  → 周期修复 cycle
 *   收入 ≤ 0 且 利润 ≤ 0  → 结构下降 decline
 *
 * 四类互斥、且覆盖任意有效的二元数字输入。
 * 用纯正负号而不是「±1pp 死区」，是为了让读者看到的数字和归类一致：
 * 页面上写 +0.4%，就不该被归到下降那一类。
 */

export const CATEGORIES = [
  {
    key: 'sync',
    name: '同步增长',
    desc: '收入和利润同向增长，规模扩张和盈利质量一致。',
  },
  {
    key: 'margin',
    name: '增收压利',
    desc: '收入还在涨，利润没跟上：价格战、成本上行或费用前置。',
  },
  {
    key: 'cycle',
    name: '周期修复',
    desc: '收入仍在收缩，但利润已经转正：去产能、低基数或价格触底回升。',
  },
  {
    key: 'decline',
    name: '结构下降',
    desc: '收入和利润同时走弱，需求或份额出现结构性流失。',
  },
];

/** 口径缺失：两个增速缺一个就分不进象限，单独列，不硬塞。 */
export const UNKNOWN = {
  key: 'unknown',
  name: '口径待补',
  desc: '收入或利润增速缺一项（或同口径下未公布），无法判定象限。',
};

const isNum = (v) => typeof v === 'number' && Number.isFinite(v);

/** @returns {'sync'|'margin'|'cycle'|'decline'|'unknown'} */
export function classify(revenueGrowth, profitGrowth) {
  if (!isNum(revenueGrowth) || !isNum(profitGrowth)) return 'unknown';
  if (revenueGrowth > 0) return profitGrowth > 0 ? 'sync' : 'margin';
  return profitGrowth > 0 ? 'cycle' : 'decline';
}

/**
 * 按四象限分组，缺口径的单独拿出来。
 * @returns {{ groups: Array<{key,name,desc,items:any[]}>, unknown: any[] }}
 */
export function groupSamples(samples = []) {
  const buckets = new Map(CATEGORIES.map((c) => [c.key, { ...c, items: [] }]));
  const unknown = [];
  for (const s of samples) {
    const key = classify(s.revenueGrowth, s.profitGrowth);
    if (key === 'unknown') {
      unknown.push({ ...s, category: key });
      continue;
    }
    buckets.get(key).items.push({ ...s, category: key });
  }
  return { groups: [...buckets.values()], unknown };
}

/** +18.2% / -8.5% / 0.0%；缺数据显示破折号，不显示 0。 */
export function fmtPct(v) {
  if (v === null || v === undefined || v === '') return '—';
  const n = Number(v);
  if (!Number.isFinite(n)) return '—';
  const sign = n > 0 ? '+' : '';
  return `${sign}${n.toFixed(1)}%`;
}

export const isUp = (v) => isNum(v) && v > 0;
