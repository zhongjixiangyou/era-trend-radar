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
 *   收入 ≤ 0 且 利润 > 0  → 缩收增利 cycle
 *   收入 ≤ 0 且 利润 ≤ 0  → 收入利润双降 decline
 *
 * 四类互斥、且覆盖任意有效的二元数字输入。
 * 用纯正负号而不是「±1pp 死区」，是为了让读者看到的数字和归类一致：
 * 页面上写 +0.4%，就不该被归到下降那一类。
 */

export const CATEGORIES = [
  {
    key: 'sync',
    name: '同步增长',
    desc: '收入和利润同比均增长；盈利质量仍需结合现金流和利润率判断。',
  },
  {
    key: 'margin',
    name: '增收压利',
    desc: '收入同比增长、利润未增长；具体原因还需成本、价格与费用数据支持。',
  },
  {
    key: 'cycle',
    name: '缩收增利',
    desc: '收入未增长、利润同比增长；仅凭两项增速不能认定周期修复。',
  },
  {
    key: 'decline',
    name: '收入利润双降',
    desc: '收入和利润均未增长；是否持续或结构性变化需要多期证据。',
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
