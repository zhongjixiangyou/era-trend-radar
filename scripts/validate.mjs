#!/usr/bin/env node
/**
 * ERA 数据校验 v1
 * 用法：node scripts/validate.mjs [--data src/data]
 * 退出码：0 通过 / 1 校验失败 / 2 用法或文件错误
 * 失败时向 stderr 输出 {"ok":false,"errors":[{path,code,message}]}
 */
import { readFileSync, existsSync } from 'node:fs';
import { resolve } from 'node:path';

const args = process.argv.slice(2);
let dataDir = 'src/data';
for (let i = 0; i < args.length; i++) {
  if (args[i] === '--data') dataDir = args[++i];
}

const root = resolve(process.cwd(), dataDir);
const errors = [];
const push = (path, code, message) => errors.push({ path, code, message });

function fail(code, message) {
  process.stderr.write(JSON.stringify({ ok: false, errors: [{ path: dataDir, code, message }] }) + '\n');
  process.exit(2);
}

const REQUIRED = ['meta.json', 'events.json', 'trends.json', 'forecasts.json', 'snapshots.json'];
for (const f of REQUIRED) {
  if (!existsSync(resolve(root, f))) fail('MISSING_FILE', `缺少 ${f}`);
}

function load(name) {
  try {
    return JSON.parse(readFileSync(resolve(root, name), 'utf8'));
  } catch (e) {
    fail('PARSE', `${name} 不是合法 JSON：${e.message}`);
  }
}

const meta = load('meta.json');
const events = load('events.json');
const trends = load('trends.json');
const forecasts = load('forecasts.json');
const snapshots = load('snapshots.json');

const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;
const ID_RE = {
  event: /^ev-\d{8}-[a-z0-9-]{1,40}$/,
  trend: /^tr-[a-z0-9-]{1,40}$/,
  forecast: /^fc-\d{6}-[a-z0-9-]{1,40}$/,
  snapshot: /^snap-\d{4}-\d{2}-\d{2}$/,
};
const DIMS = ['structural', 'scope', 'signal', 'novelty', 'timeliness', 'cross'];
const WEIGHTS = { structural: 0.25, scope: 0.2, signal: 0.2, novelty: 0.15, timeliness: 0.1, cross: 0.1 };
const REGIONS = ['global', 'china', 'us', 'europe', 'asia', 'other'];
const CATEGORIES = ['technology', 'economy', 'industry', 'society', 'policy', 'energy', 'demography'];
const EFFECTS = ['support', 'weaken', 'challenge'];
const CRED = ['high', 'medium', 'low'];
const E_STATUS = ['confirmed', 'partial', 'pending'];
const T_STATUS = ['emerging', 'strengthening', 'stable', 'weakening', 'falsified'];
const F_STATUS = ['open', 'supported', 'weakened', 'falsified', 'unresolved'];

const isDate = (v) => typeof v === 'string' && DATE_RE.test(v) && !Number.isNaN(Date.parse(v + 'T00:00:00Z'));
const isInt = (v) => Number.isInteger(v);
const isHttp = (v) => typeof v === 'string' && /^https?:\/\/\S+$/i.test(v);
const isNonEmptyStr = (v) => typeof v === 'string' && v.trim().length > 0;

for (const [name, arr] of Object.entries({ events, trends, forecasts, snapshots })) {
  if (!Array.isArray(arr)) push(name, 'TYPE', '必须是数组');
  else if (arr.length === 0) push(name, 'EMPTY', '不能为空数组');
}

// ---- 唯一 ID ----
function uniqueIds(list, path, re) {
  const seen = new Set();
  (Array.isArray(list) ? list : []).forEach((item, i) => {
    const id = item?.id;
    if (!isNonEmptyStr(id)) return push(`${path}[${i}].id`, 'REQUIRED', '缺少 id');
    if (seen.has(id)) push(`${path}[${i}].id`, 'DUPLICATE', `重复 id：${id}`);
    seen.add(id);
    if (!re.test(id)) push(`${path}[${i}].id`, 'FORMAT', `id 不符合格式：${id}`);
  });
  return seen;
}

const eventIds = uniqueIds(events, 'events', ID_RE.event);
const trendIds = uniqueIds(trends, 'trends', ID_RE.trend);
uniqueIds(forecasts, 'forecasts', ID_RE.forecast);
uniqueIds(snapshots, 'snapshots', ID_RE.snapshot);

// ---- 事件 ----
(Array.isArray(events) ? events : []).forEach((e, i) => {
  const p = `events[${i}]`;
  for (const k of ['title', 'summary']) if (!isNonEmptyStr(e[k])) push(`${p}.${k}`, 'REQUIRED', `${k} 不能为空`);
  if (isNonEmptyStr(e.title) && e.title.length > 42) push(`${p}.title`, 'LENGTH', `标题超过 42 字（${e.title.length}）`);
  for (const k of ['date', 'reportedAt']) if (!isDate(e[k])) push(`${p}.${k}`, 'DATE', `${k} 不是 YYYY-MM-DD`);
  if (e.backfill === true && !isDate(e.collectedAt)) push(`${p}.collectedAt`, 'BACKFILL', 'backfill=true 必须写 collectedAt');
  if (e.collectedAt && !isDate(e.collectedAt)) push(`${p}.collectedAt`, 'DATE', 'collectedAt 格式错误');
  if (!REGIONS.includes(e.region)) push(`${p}.region`, 'ENUM', `region 非法：${e.region}`);
  if (!CATEGORIES.includes(e.category)) push(`${p}.category`, 'ENUM', `category 非法：${e.category}`);
  if (!CRED.includes(e.credibility)) push(`${p}.credibility`, 'ENUM', `credibility 非法：${e.credibility}`);
  if (!E_STATUS.includes(e.status)) push(`${p}.status`, 'ENUM', `status 非法：${e.status}`);
  if (e.credibility === 'low' && e.status !== 'pending') {
    push(`${p}.status`, 'CRED_RULE', 'credibility=low 时 status 必须为 pending');
  }

  // 六维
  if (!e.scores || typeof e.scores !== 'object') {
    push(`${p}.scores`, 'REQUIRED', '缺少 scores');
  } else {
    let sum = 0;
    let ok = true;
    for (const d of DIMS) {
      const v = e.scores[d];
      if (!isInt(v) || v < 0 || v > 100) {
        push(`${p}.scores.${d}`, 'RANGE', `${d} 必须是 0–100 的整数`);
        ok = false;
      } else {
        sum += v * WEIGHTS[d];
      }
    }
    if (ok) {
      const expected = Math.round(sum);
      const recorded = e.scoreHistory?.[e.scoreHistory.length - 1]?.total;
      if (recorded === undefined) {
        push(`${p}.scoreHistory`, 'REQUIRED', 'scoreHistory 不能为空');
      } else if (recorded !== expected) {
        push(`${p}.scoreHistory[${e.scoreHistory.length - 1}].total`, 'SCORE_MISMATCH',
          `记录总分 ${recorded} 与公式值 ${expected} 不一致`);
      }
    }
  }
  if (!Array.isArray(e.scoreHistory) || e.scoreHistory.length === 0) {
    push(`${p}.scoreHistory`, 'REQUIRED', 'scoreHistory 不能为空');
  }

  // 事实与来源
  if (!Array.isArray(e.facts) || e.facts.length < 2) push(`${p}.facts`, 'REQUIRED', '至少 2 条可核实事实');
  if (!Array.isArray(e.sources) || e.sources.length === 0) push(`${p}.sources`, 'REQUIRED', '至少 1 个来源');
  (e.sources ?? []).forEach((s, j) => {
    if (!isHttp(s?.url)) push(`${p}.sources[${j}].url`, 'URL', '来源 URL 必须是 http(s)');
    if (!isNonEmptyStr(s?.name)) push(`${p}.sources[${j}].name`, 'REQUIRED', '来源缺少名称');
    if (s?.date && !isDate(s.date)) push(`${p}.sources[${j}].date`, 'DATE', '来源日期格式错误');
  });

  // 趋势引用
  if (!Array.isArray(e.trends)) {
    push(`${p}.trends`, 'REQUIRED', 'trends 必须是数组');
  } else {
    e.trends.forEach((t, j) => {
      if (!trendIds.has(t?.id)) push(`${p}.trends[${j}].id`, 'DANGLING', `引用了不存在的趋势：${t?.id}`);
      if (!EFFECTS.includes(t?.effect)) push(`${p}.trends[${j}].effect`, 'ENUM', `effect 非法：${t?.effect}`);
    });
  }

  if (meta?.window?.from && isDate(e.date) && e.date < meta.window.from) {
    push(`${p}.date`, 'WINDOW', `事件日期早于覆盖窗口 ${meta.window.from}`);
  }
  if (meta?.window?.to && isDate(e.date) && e.date > meta.window.to) {
    push(`${p}.date`, 'WINDOW', `事件日期晚于覆盖窗口 ${meta.window.to}`);
  }
});

// ---- 趋势 ----
(Array.isArray(trends) ? trends : []).forEach((t, i) => {
  const p = `trends[${i}]`;
  for (const k of ['title', 'thesis', 'judgment']) if (!isNonEmptyStr(t[k])) push(`${p}.${k}`, 'REQUIRED', `${k} 不能为空`);
  if (!isDate(t.firstSeen)) push(`${p}.firstSeen`, 'DATE', 'firstSeen 格式错误');
  if (!T_STATUS.includes(t.status)) push(`${p}.status`, 'ENUM', `status 非法：${t.status}`);
  if (!isInt(t.confidence) || t.confidence < 0 || t.confidence > 100) push(`${p}.confidence`, 'RANGE', 'confidence 必须是 0–100 的整数');
  if (!Array.isArray(t.indicators) || t.indicators.length === 0) push(`${p}.indicators`, 'REQUIRED', '至少 1 个观察指标');
  if (!Array.isArray(t.revisions) || t.revisions.length === 0) {
    push(`${p}.revisions`, 'REQUIRED', 'revisions 不能为空');
  } else {
    t.revisions.forEach((r, j) => {
      if (!isDate(r?.date)) push(`${p}.revisions[${j}].date`, 'DATE', '日期格式错误');
      if (!isInt(r?.to) || r.to < 0 || r.to > 100) push(`${p}.revisions[${j}].to`, 'RANGE', 'to 必须是 0–100');
      if (j > 0 && r.from !== t.revisions[j - 1].to) {
        push(`${p}.revisions[${j}].from`, 'CHAIN', `from=${r.from} 与上一条 to=${t.revisions[j - 1].to} 不连续`);
      }
      if (j === 0 && r.from !== null) push(`${p}.revisions[0].from`, 'CHAIN', '第一条 from 必须为 null');
    });
    const last = t.revisions[t.revisions.length - 1].to;
    if (isInt(t.confidence) && last !== t.confidence) {
      push(`${p}.confidence`, 'CHAIN', `confidence=${t.confidence} 与最后一条修订 ${last} 不一致`);
    }
  }
  if (t.verifyBy && !isDate(t.verifyBy)) push(`${p}.verifyBy`, 'DATE', 'verifyBy 格式错误');
});

// ---- 判断 ----
(Array.isArray(forecasts) ? forecasts : []).forEach((f, i) => {
  const p = `forecasts[${i}]`;
  if (!isNonEmptyStr(f.claim)) push(`${p}.claim`, 'REQUIRED', 'claim 不能为空');
  if (!isDate(f.madeAt)) push(`${p}.madeAt`, 'DATE', 'madeAt 格式错误');
  if (!isDate(f.dueBy)) push(`${p}.dueBy`, 'DATE', 'dueBy 格式错误');
  if (isDate(f.madeAt) && isDate(f.dueBy) && f.dueBy < f.madeAt) push(`${p}.dueBy`, 'ORDER', 'dueBy 不能早于 madeAt');
  if (!F_STATUS.includes(f.status)) push(`${p}.status`, 'ENUM', `status 非法：${f.status}`);
  if (!isInt(f.confidence) || f.confidence < 0 || f.confidence > 100) push(`${p}.confidence`, 'RANGE', 'confidence 必须是 0–100 的整数');
  if (f.trendId && !trendIds.has(f.trendId)) push(`${p}.trendId`, 'DANGLING', `引用了不存在的趋势：${f.trendId}`);
  if (!Array.isArray(f.history) || f.history.length === 0) push(`${p}.history`, 'REQUIRED', 'history 不能为空');
  if (!Array.isArray(f.support)) push(`${p}.support`, 'TYPE', 'support 必须是数组');
  if (!Array.isArray(f.counter)) push(`${p}.counter`, 'TYPE', 'counter 必须是数组');
  if (Array.isArray(f.history) && f.history.length > 0) {
    const last = f.history[f.history.length - 1];
    if (last.confidence !== f.confidence) push(`${p}.confidence`, 'CHAIN', `confidence=${f.confidence} 与最后一条历史 ${last.confidence} 不一致`);
  }
});

// ---- 快照 ----
(Array.isArray(snapshots) ? snapshots : []).forEach((s, i) => {
  const p = `snapshots[${i}]`;
  if (!isDate(s.date)) push(`${p}.date`, 'DATE', 'date 格式错误');
  if (!['weekly', 'monthly'].includes(s.period)) push(`${p}.period`, 'ENUM', 'period 必须是 weekly 或 monthly');
  if (!isNonEmptyStr(s.title)) push(`${p}.title`, 'REQUIRED', 'title 不能为空');
  if (!Array.isArray(s.eventIds)) push(`${p}.eventIds`, 'TYPE', 'eventIds 必须是数组');
  (s.eventIds ?? []).forEach((id, j) => {
    if (!eventIds.has(id)) push(`${p}.eventIds[${j}]`, 'DANGLING', `引用了不存在的事件：${id}`);
  });
});

// ---- meta ----
if (meta?.window?.from && !isDate(meta.window.from)) push('meta.window.from', 'DATE', '格式错误');
if (meta?.window?.to && !isDate(meta.window.to)) push('meta.window.to', 'DATE', '格式错误');
if (meta?.counts) {
  if (meta.counts.events !== events.length) push('meta.counts.events', 'COUNT', `meta 写 ${meta.counts.events}，实际 ${events.length}`);
  if (meta.counts.trends !== trends.length) push('meta.counts.trends', 'COUNT', `meta 写 ${meta.counts.trends}，实际 ${trends.length}`);
  if (meta.counts.forecasts !== forecasts.length) push('meta.counts.forecasts', 'COUNT', `meta 写 ${meta.counts.forecasts}，实际 ${forecasts.length}`);
}

if (errors.length > 0) {
  process.stderr.write(JSON.stringify({ ok: false, errors }, null, 2) + '\n');
  process.exit(1);
}

process.stdout.write(JSON.stringify({
  ok: true,
  counts: { events: events.length, trends: trends.length, forecasts: forecasts.length, snapshots: snapshots.length },
}) + '\n');
