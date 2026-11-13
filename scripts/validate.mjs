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

const errors = [];
const warnings = [];
const push = (path, code, message) => errors.push({ path, code, message });
const warn = (path, code, message) => warnings.push({ path, code, message });

function fail(code, message) {
  process.stderr.write(JSON.stringify({ ok: false, errors: [{ path: dataDir, code, message }] }) + '\n');
  process.exit(2);
}

const USAGE = '用法：node scripts/validate.mjs [--data <dir>]';
for (let i = 0; i < args.length; i++) {
  if (args[i] === '--data') {
    const v = args[++i];
    if (!v || v.startsWith('--')) fail('USAGE', `--data 后面要跟目录名。${USAGE}`);
    dataDir = v;
  } else if (args[i] === '--help' || args[i] === '-h') {
    process.stdout.write(USAGE + '\n--data 默认 src/data；退出码 0 通过 / 1 校验失败 / 2 用法或文件错误\n');
    process.exit(0);
  } else {
    fail('USAGE', `不认识的参数：${args[i]}。${USAGE}`);
  }
}

const root = resolve(process.cwd(), dataDir);

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
// 两套都认：v1 永久 ID（ev_slug / tr_slug / fc_slug，权威）与旧站格式（ev-YYYYMMDD-slug）。
const ID_RE = {
  event: /^(ev-\d{8}-[a-z0-9-]{1,40}|ev_[a-z0-9][a-z0-9-]{0,60})$/,
  trend: /^(tr-[a-z0-9-]{1,40}|tr_[a-z0-9][a-z0-9-]{0,60})$/,
  forecast: /^(fc-\d{6}-[a-z0-9-]{1,40}|fc_[a-z0-9][a-z0-9-]{0,60})$/,
  snapshot: /^snap-\d{4}-\d{2}-\d{2}$/,
};
const DIMS = ['structural', 'scope', 'signal', 'novelty', 'timeliness', 'cross'];
// v1 口径：六维本身就是分项得分（满分 25/20/20/15/10/10），总分 = 六项直接相加。
const CAPS = { structural: 25, scope: 20, signal: 20, novelty: 15, timeliness: 10, cross: 10 };
const REGIONS = ['global', 'china', 'us', 'europe', 'asia', 'other'];
const CATEGORIES = ['technology', 'economy', 'industry', 'society', 'policy', 'energy', 'demography'];
const EFFECTS = ['support', 'weaken', 'challenge'];
const CRED = ['high', 'medium', 'low'];
const E_STATUS = ['confirmed', 'partial', 'pending'];
const T_STATUS = ['emerging', 'strengthening', 'stable', 'weakening', 'falsified'];
const F_STATUS = ['open', 'supported', 'weakened', 'falsified', 'unresolved'];

const isDate = (v) => typeof v === 'string' && DATE_RE.test(v) && !Number.isNaN(Date.parse(v + 'T00:00:00Z'));
const isInt = (v) => Number.isInteger(v);
const isObj = (v) => v !== null && typeof v === 'object' && !Array.isArray(v);
/** 带时区的 ISO 8601 时间；Date.parse 会放过 2026/11/13 这类非 ISO 写法，必须先用正则卡住 */
const ISO_DT = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?(\.\d+)?(Z|[+-]\d{2}:\d{2})$/;
const isIsoDateTime = (v) => typeof v === 'string' && ISO_DT.test(v) && !Number.isNaN(Date.parse(v));
const SECRET_QUERY = /[?&](access_?token|token|api_?key|apikey|key|sig|signature|secret|password|passwd|pwd|auth)=/i;
/**
 * 来源链接校验：必须 http(s)，且不能把凭据写进 URL。
 * 拒两种：authority 里的 userinfo（https://user:pass@host/…），以及 ?token=/key=/sig= 一类敏感参数。
 * 返回 null 表示没问题，否则返回原因（直接当 message 用）。
 */
function urlProblem(v) {
  if (typeof v !== 'string' || v.length === 0) return 'URL 不能为空';
  let u;
  try {
    u = new URL(v);
  } catch {
    return 'URL 不是合法地址';
  }
  if (u.protocol !== 'http:' && u.protocol !== 'https:') return 'URL 协议必须是 http 或 https';
  if (u.username || u.password) return 'URL 不能带用户名密码（userinfo），那等于把凭据发到页面和仓库里';
  if (SECRET_QUERY.test(u.search) || SECRET_QUERY.test('?' + u.hash.replace(/^#/, ''))) {
    return 'URL 不能带 token/key/sig 一类敏感查询参数';
  }
  return null;
}
const isHttp = (v) => urlProblem(v) === null;
const isNonEmptyStr = (v) => typeof v === 'string' && v.trim().length > 0;

for (const [name, arr] of Object.entries({ events, trends, forecasts, snapshots })) {
  if (!Array.isArray(arr)) push(name, 'TYPE', '必须是数组');
  else if (arr.length === 0 && name !== 'snapshots') push(name, 'EMPTY', '不能为空数组');
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
  if (isDate(e.date) && isDate(e.reportedAt) && e.reportedAt < e.date) {
    push(`${p}.reportedAt`, 'ORDER', `reportedAt=${e.reportedAt} 早于事情发生的 date=${e.date}`);
  }
  if (isDate(e.reportedAt) && isDate(e.collectedAt) && e.collectedAt < e.reportedAt) {
    push(`${p}.collectedAt`, 'ORDER', `collectedAt=${e.collectedAt} 早于 reportedAt=${e.reportedAt}`);
  }
  if (!REGIONS.includes(e.region)) push(`${p}.region`, 'ENUM', `region 非法：${e.region}`);
  if (!CATEGORIES.includes(e.category)) push(`${p}.category`, 'ENUM', `category 非法：${e.category}`);
  if (!CRED.includes(e.credibility)) push(`${p}.credibility`, 'ENUM', `credibility 非法：${e.credibility}`);
  if (!E_STATUS.includes(e.status)) push(`${p}.status`, 'ENUM', `status 非法：${e.status}`);
  if (e.credibility === 'low' && e.status !== 'pending') {
    push(`${p}.status`, 'CRED_RULE', 'credibility=low 时 status 必须为 pending');
  }

  // 六维 + 唯一当前分 scores.total
  if (!e.scores || typeof e.scores !== 'object') {
    push(`${p}.scores`, 'REQUIRED', '缺少 scores');
  } else {
    let sum = 0;
    let ok = true;
    for (const d of DIMS) {
      const v = e.scores[d];
      if (!isInt(v) || v < 0 || v > CAPS[d]) {
        push(`${p}.scores.${d}`, 'RANGE', `${d} 必须是 0–${CAPS[d]} 的整数`);
        ok = false;
      } else {
        sum += v;
      }
    }
    if (e.scores.rationale !== undefined) {
      if (!isObj(e.scores.rationale)) {
        push(`${p}.scores.rationale`, 'TYPE', 'rationale 必须是对象');
      } else {
        // 规范键名与 scores 对齐（novelty / cross）；delta / crossDomain 是二组旧导出的名字，
        // 迁移窗口内继续认，但每份数据提醒一次，别让新旧两套名字长期并存。
        const legacy = { novelty: 'delta', cross: 'crossDomain' };
        const missing = [];
        const aliased = [];
        for (const d of DIMS) {
          const r = e.scores.rationale;
          if (isNonEmptyStr(r[d])) continue;
          if (legacy[d] && isNonEmptyStr(r[legacy[d]])) {
            aliased.push(`${legacy[d]}→${d}`);
            continue;
          }
          missing.push(d);
        }
        if (missing.length > 0) {
          warn(`${p}.scores.rationale`, 'RATIONALE', `缺 ${missing.join('/')} 的分项理由`);
        }
        if (aliased.length > 0) {
          warn(`${p}.scores.rationale`, 'RATIONALE_ALIAS',
            `用了旧键名 ${aliased.join('、')}；规范名与 scores 一致，二组下轮导出请改名`);
        }
      }
    }
    if (!isInt(e.scores.total) || e.scores.total < 0 || e.scores.total > 100) {
      push(`${p}.scores.total`, 'RANGE', 'scores.total 是当前分的唯一来源，必须是 0–100 的整数');
    } else if (ok) {
      const expected = sum;
      if (e.scores.total !== expected) {
        push(`${p}.scores.total`, 'TOTAL_MISMATCH',
          `scores.total=${e.scores.total} 与六维合计 ${expected} 不一致`);
      }
    }
  }

  // 评分历史：日期严格递增 / 分值范围 / 末条等于当前分 / 首条落在采集日
  if (!Array.isArray(e.scoreHistory) || e.scoreHistory.length === 0) {
    push(`${p}.scoreHistory`, 'REQUIRED', 'scoreHistory 不能为空');
  } else {
    let prevDate = null;
    e.scoreHistory.forEach((h, j) => {
      if (!isDate(h?.date)) {
        push(`${p}.scoreHistory[${j}].date`, 'DATE', '日期格式错误');
      } else if (prevDate !== null && h.date < prevDate) {
        push(`${p}.scoreHistory[${j}].date`, 'ORDER', `评分历史日期不能倒退（${h.date} 早于 ${prevDate}）；同日多条按数组顺序算先后`);
      } else {
        prevDate = h.date;
      }
      if (!isInt(h?.total) || h.total < 0 || h.total > 100) {
        push(`${p}.scoreHistory[${j}].total`, 'RANGE', 'total 必须是 0–100 的整数');
      }
      if (h?.reason !== undefined && !isNonEmptyStr(h.reason)) {
        push(`${p}.scoreHistory[${j}].reason`, 'REQUIRED', 'reason 不能是空字符串');
      }
    });
    // 只有一次评分（首次建立）不需要理由；改过一次分却不写为什么，等于留痕没留字
    if (e.scoreHistory.length > 1) {
      const noReason = e.scoreHistory.filter((h) => !isNonEmptyStr(h?.reason)).length;
      if (noReason > 0) {
        warn(`${p}.scoreHistory`, 'HISTORY_REASON',
          `${e.scoreHistory.length} 条评分里有 ${noReason} 条没写 reason；改分要能回答「为什么改」，补上理由再发布`);
      }
    }
    const lastH = e.scoreHistory[e.scoreHistory.length - 1];
    if (isInt(e.scores?.total) && lastH?.total !== e.scores.total) {
      push(`${p}.scoreHistory[${e.scoreHistory.length - 1}].total`, 'HISTORY_TAIL',
        `末条历史 total=${lastH?.total} 与当前分 scores.total=${e.scores.total} 不一致；历史只许追加，改末条等于篡改`);
    }
    const firstH = e.scoreHistory[0];
    if (isDate(e.collectedAt) && isDate(firstH?.date) && firstH.date !== e.collectedAt.slice(0, 10)) {
      push(`${p}.scoreHistory[0].date`, 'HISTORY_HEAD',
        `首次评分日期必须等于采集日 ${e.collectedAt.slice(0, 10)}（写的是 ${firstH.date}）`);
    }
  }

  // 事实与来源
  if (!Array.isArray(e.facts) || e.facts.length < 2) push(`${p}.facts`, 'REQUIRED', '至少 2 条可核实事实');
  if (!Array.isArray(e.sources) || e.sources.length === 0) push(`${p}.sources`, 'REQUIRED', '至少 1 个来源');
  (e.sources ?? []).forEach((s, j) => {
    const urlErr = urlProblem(s?.url);
    if (urlErr) push(`${p}.sources[${j}].url`, 'URL', urlErr);
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
  if (t.confidence !== null && (!isInt(t.confidence) || t.confidence < 0 || t.confidence > 100)) {
    push(`${p}.confidence`, 'RANGE', 'confidence 必须是 0–100 的整数或 null（暂无）');
  }
  if (!Array.isArray(t.indicators) || t.indicators.length === 0) push(`${p}.indicators`, 'REQUIRED', '至少 1 个观察指标');
  if (!Array.isArray(t.revisions) || t.revisions.length === 0) {
    push(`${p}.revisions`, 'REQUIRED', 'revisions 不能为空');
  } else {
    t.revisions.forEach((r, j) => {
      if (!isDate(r?.date)) push(`${p}.revisions[${j}].date`, 'DATE', '日期格式错误');
      else if (j > 0 && isDate(t.revisions[j - 1]?.date) && r.date < t.revisions[j - 1].date) {
        push(`${p}.revisions[${j}].date`, 'ORDER', `修订日期不能倒退（${r.date} 早于 ${t.revisions[j - 1].date}）；同日多条按数组顺序算先后`);
      }
      if (r?.to !== null && (!isInt(r?.to) || r.to < 0 || r.to > 100)) {
        push(`${p}.revisions[${j}].to`, 'RANGE', 'to 必须是 0–100 的整数或 null（暂无）');
      }
      if (r?.from !== null && (!isInt(r?.from) || r.from < 0 || r.from > 100)) {
        push(`${p}.revisions[${j}].from`, 'RANGE', 'from 必须是 null 或 0–100 的整数');
      }
      if (j > 0 && r.from !== t.revisions[j - 1].to) {
        push(`${p}.revisions[${j}].from`, 'REVISION_CHAIN', `from=${r.from} 与上一条 to=${t.revisions[j - 1].to} 不连续`);
      }
      if (j === 0 && r.from !== null) push(`${p}.revisions[0].from`, 'REVISION_CHAIN', '第一条 from 必须为 null');
    });
    const last = t.revisions[t.revisions.length - 1].to;
    if (isInt(t.confidence) && last !== t.confidence) {
      push(`${p}.confidence`, 'REVISION_CHAIN', `confidence=${t.confidence} 与最后一条修订 ${last} 不一致`);
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
  if (Array.isArray(f.history)) {
    f.history.forEach((h, j) => {
      if (!isDate(h?.date)) push(`${p}.history[${j}].date`, 'DATE', '日期格式错误');
      if (!isInt(h?.confidence) || h.confidence < 0 || h.confidence > 100) {
        push(`${p}.history[${j}].confidence`, 'RANGE', 'confidence 必须是 0–100 的整数');
      }
      if (j > 0 && isDate(h?.date) && isDate(f.history[j - 1]?.date) && h.date < f.history[j - 1].date) {
        push(`${p}.history[${j}].date`, 'ORDER', `历史日期不能倒退（${h.date} 早于 ${f.history[j - 1].date}）；同日多条按数组顺序算先后`);
      }
    });
  }
  if (Array.isArray(f.history) && f.history.length > 0) {
    const last = f.history[f.history.length - 1];
    if (last.confidence !== f.confidence) push(`${p}.confidence`, 'REVISION_CHAIN', `confidence=${f.confidence} 与最后一条历史 ${last.confidence} 不一致`);
  }
});

// ---- 快照 ----
(Array.isArray(snapshots) ? snapshots : []).forEach((s, i) => {
  const p = `snapshots[${i}]`;
  if (!isDate(s.date)) push(`${p}.date`, 'DATE', 'date 格式错误');
  if (!['weekly', 'monthly'].includes(s.period)) push(`${p}.period`, 'ENUM', 'period 必须是 weekly 或 monthly');
  if (!isNonEmptyStr(s.title)) push(`${p}.title`, 'REQUIRED', 'title 不能为空');
  if (!isNonEmptyStr(s.summary)) push(`${p}.summary`, 'REQUIRED', 'summary 不能为空');
  if (!Array.isArray(s.eventIds)) {
    push(`${p}.eventIds`, 'TYPE', 'eventIds 必须是数组');
  } else {
    if (s.eventIds.length === 0) push(`${p}.eventIds`, 'REQUIRED', 'eventIds 不能为空');
    s.eventIds.forEach((id, j) => {
      if (!eventIds.has(id)) push(`${p}.eventIds[${j}]`, 'DANGLING', `引用了不存在的事件：${id}`);
    });
  }
  if (s.trendChanges !== undefined) {
    if (!Array.isArray(s.trendChanges)) {
      push(`${p}.trendChanges`, 'TYPE', 'trendChanges 必须是数组');
    } else {
      s.trendChanges.forEach((c, j) => {
        if (!trendIds.has(c?.trendId)) {
          push(`${p}.trendChanges[${j}].trendId`, 'DANGLING', `引用了不存在的趋势：${c?.trendId}`);
        }
        if (c?.from !== null && !T_STATUS.includes(c?.from)) {
          push(`${p}.trendChanges[${j}].from`, 'ENUM', `from 状态非法：${c?.from}`);
        }
        if (!T_STATUS.includes(c?.to)) {
          push(`${p}.trendChanges[${j}].to`, 'ENUM', `to 状态非法：${c?.to}`);
        }
        if (!isNonEmptyStr(c?.reason)) push(`${p}.trendChanges[${j}].reason`, 'REQUIRED', 'reason 不能为空');
      });
    }
  }
});

// ---- 孤儿趋势：能引用没被引用的趋势只警告，不拦发布（页面必须显式写「暂无关联事件」）----
{
  const referenced = new Set();
  for (const e of Array.isArray(events) ? events : []) {
    for (const t of Array.isArray(e.trends) ? e.trends : []) if (t?.id) referenced.add(t.id);
  }
  for (const f of Array.isArray(forecasts) ? forecasts : []) if (f?.trendId) referenced.add(f.trendId);
  for (const s of Array.isArray(snapshots) ? snapshots : []) {
    for (const c of Array.isArray(s.trendChanges) ? s.trendChanges : []) if (c?.trendId) referenced.add(c.trendId);
  }
  for (const id of trendIds) {
    if (!referenced.has(id)) {
      warn('trends', 'ORPHAN', `趋势 ${id} 没有被任何事件/判断/快照引用，页面需显式显示「暂无关联事件」`);
    }
  }
}

// ---- meta ----
const META_RUN = ['ok', 'stale', 'failed', 'partial'];
if (!isObj(meta)) {
  push('meta', 'TYPE', 'meta.json 必须是对象');
} else {
  if (!isNonEmptyStr(meta.site)) push('meta.site', 'REQUIRED', 'site 不能为空');
  if (!isNonEmptyStr(meta.editorNote)) push('meta.editorNote', 'REQUIRED', 'editorNote 不能为空');
  if (!isNonEmptyStr(meta.issue)) warn('meta.issue', 'MISSING_FIELD', '没有期号 issue，页面期号位会空着');
  if (!isIsoDateTime(meta.generatedAt)) {
    push('meta.generatedAt', 'DATE', 'generatedAt 必须是带时区的 ISO 时间，如 2026-11-13T09:00:00+08:00');
  }
  if (!META_RUN.includes(meta.runStatus)) {
    push('meta.runStatus', 'ENUM', `runStatus 必须是 ${META_RUN.join(' / ')} 之一`);
  }
  if (meta.runStatus !== 'ok' && !meta.lastSuccessfulRun) {
    warn('meta.lastSuccessfulRun', 'MISSING_FIELD', 'runStatus 非 ok 时应写 lastSuccessfulRun，否则不知道上次成功是什么时候');
  }
  if (meta.lastSuccessfulRun !== undefined && !isIsoDateTime(meta.lastSuccessfulRun)) {
    push('meta.lastSuccessfulRun', 'DATE', 'lastSuccessfulRun 必须是带时区的 ISO 时间');
  }
  if (!isObj(meta.window)) {
    push('meta.window', 'REQUIRED', 'window 必须给出覆盖区间');
  } else {
    if (!isDate(meta.window.from)) push('meta.window.from', 'DATE', '格式错误');
    if (!isDate(meta.window.to)) push('meta.window.to', 'DATE', '格式错误');
    if (isDate(meta.window.from) && isDate(meta.window.to) && meta.window.from > meta.window.to) {
      push('meta.window', 'ORDER', 'window.from 不能晚于 window.to');
    }
  }
  if (isObj(meta.counts)) {
    const pairs = [['events', events], ['trends', trends], ['forecasts', forecasts], ['snapshots', snapshots]];
    for (const [k, arr] of pairs) {
      if (meta.counts[k] === undefined) continue;
      const n = Array.isArray(arr) ? arr.length : 0;
      if (meta.counts[k] !== n) push(`meta.counts.${k}`, 'COUNT', `meta 写 ${meta.counts[k]}，实际 ${n}`);
    }
  } else {
    push('meta.counts', 'REQUIRED', 'counts 必须给出各数据文件条数');
  }
}

if (errors.length > 0) {
  process.stderr.write(JSON.stringify({ ok: false, errors, warnings }, null, 2) + '\n');
  process.exit(1);
}

process.stdout.write(JSON.stringify({
  ok: true,
  counts: { events: events.length, trends: trends.length, forecasts: forecasts.length, snapshots: snapshots.length },
  warnings,
}) + '\n');
