// scripts/ci/lib/data.mjs — 对齐三组查错 check/SCHEMA.md v1.0 的数据校验核心。
// 单文件数据模型：content/data.json = { schemaVersion, scoringVersion, events[], trends[] }
// 只用标准库；validate.mjs / test-validate.mjs / analyze.mjs 共用。
import { dateOnly } from "./common.mjs";
import { resolveWindow as resolveSharedWindow } from "./window.mjs";

export const DIRECTIONS = ["支持", "削弱", "挑战"];

/** 方向枚举以 config.directions 为准；未配置时回落到冻结默认值。 */
function cfgDirections(config) {
  const list = config?.directions;
  return Array.isArray(list) && list.length ? list : DIRECTIONS;
}
export const EVENT_REQUIRED = [
  "id",
  "title",
  "dedupeKey",
  "dates",
  "sources",
  "trendLinks",
  "score",
  "firstScore",
  "firstJudgment",
  "scoreHistory",
  "judgmentHistory",
];
export const TREND_REQUIRED = ["id", "name", "firstJudgment", "judgmentHistory"];
export const DATE_FIELDS = ["occurred", "firstSeen", "collected"];

const isObj = (v) => v !== null && typeof v === "object" && !Array.isArray(v);
const isStr = (v) => typeof v === "string" && v.trim().length > 0;
const isInt = (v) => typeof v === "number" && Number.isInteger(v);
const deep = (a, b) => JSON.stringify(a) === JSON.stringify(b);

/** 窗口：不再自己算，统一走 ./window.mjs（契约 docs/window-rolling.md）。
 * 这里只把字段名适配成校验器用的 start/end，asOf 优先级（--as-of/ERA_AS_OF > --max-ts/ERA_MAX_TS > 时钟）
 * 与 collect/validate/export 完全一致，不会跨午夜出现两个「今天」。 */
export function resolveWindow(config, now) {
  const w = resolveSharedWindow(config, now ? { now } : {});
  return {
    mode: w.mode,
    start: w.from,
    end: w.to,
    days: w.days,
    upper: w.upper,
    asOf: w.asOf,
    asOfSource: w.asOfSource,
    asOfFrom: w.asOfFrom,
    ...(w.frozenAt ? { frozenAt: w.frozenAt } : {}),
  };
}

function parseDate(v) {
  if (!isStr(v)) return null;
  if (!/^\d{4}-\d{2}-\d{2}([T ].*)?$/.test(v.trim())) return null;
  const d = new Date(v.trim().length === 10 ? `${v.trim()}T00:00:00Z` : v.trim());
  return Number.isNaN(d.getTime()) ? null : d;
}

function scoreShape(score) {
  if (!isObj(score)) return null;
  return {
    version: score.version,
    dimensions: score.dimensions,
    total: score.total,
    displayedTotal: score.displayedTotal,
  };
}

function checkScore(score, config, p, errors) {
  if (!isObj(score)) {
    errors.push({ code: "WRONG_TYPE", path: `${p}.score`, message: "score 必须是对象" });
    return;
  }
  if (!isStr(score.version)) {
    errors.push({ code: "SCORE_VERSION_MISSING", path: `${p}.score.version`, message: "缺少评分版本" });
  } else if (score.version !== config.scoringVersion) {
    errors.push({
      code: "SCORE_VERSION_MISSING",
      path: `${p}.score.version`,
      message: `评分版本 ${score.version} != config ${config.scoringVersion}`,
    });
  }
  if (!isObj(score.dimensions)) {
    errors.push({ code: "MISSING_FIELD", path: `${p}.score.dimensions`, message: "缺少六维分数" });
    return;
  }
  let sum = 0;
  for (const dim of config.scoring.dimensions) {
    const v = score.dimensions[dim.key];
    const dp = `${p}.score.dimensions.${dim.key}`;
    if (!isInt(v)) {
      errors.push({ code: "WRONG_TYPE", path: dp, message: `缺少整数分数（0-${dim.max}）` });
      continue;
    }
    if (v < 0 || v > dim.max) {
      errors.push({ code: "DIMENSION_OVER_CAP", path: dp, message: `${v} 超出上限 ${dim.max}` });
    }
    sum += v;
    if (config.scoring.requireBasis && !isStr(score.basis?.[dim.key])) {
      errors.push({ code: "SCORE_BASIS_MISSING", path: `${dp}.basis`, message: "缺少该维度的评分依据" });
    }
  }
  for (const key of Object.keys(score.dimensions)) {
    if (!config.scoring.dimensions.some((d) => d.key === key)) {
      errors.push({ code: "WRONG_TYPE", path: `${p}.score.dimensions.${key}`, message: `未知评分维度 ${key}` });
    }
  }
  if (!isInt(score.total)) errors.push({ code: "WRONG_TYPE", path: `${p}.score.total`, message: "total 必须是整数" });
  if (score.total !== sum) {
    errors.push({ code: "SCORE_SUM_MISMATCH", path: `${p}.score.total`, message: `六维求和 ${sum} != total ${score.total}` });
  }
  if (score.displayedTotal !== score.total) {
    errors.push({
      code: "DISPLAYED_MISMATCH",
      path: `${p}.score.displayedTotal`,
      message: `displayedTotal ${score.displayedTotal} != total ${score.total}`,
    });
  }
}

function checkFirstScore(ev, p, errors) {
  if (!isObj(ev.firstScore)) {
    errors.push({ code: "MISSING_FIELD", path: `${p}.firstScore`, message: "缺少 firstScore" });
    return;
  }
  if (!Array.isArray(ev.scoreHistory) || ev.scoreHistory.length === 0) {
    errors.push({ code: "MISSING_FIELD", path: `${p}.scoreHistory`, message: "scoreHistory 至少一条" });
    return;
  }
  const first = scoreShape(ev.scoreHistory[0]?.score);
  if (first && !deep(scoreShape(ev.firstScore), first)) {
    errors.push({ code: "FIRST_RECORD_MISMATCH", path: `${p}.firstScore`, message: "firstScore 与 scoreHistory[0].score 不一致" });
  }
  const last = scoreShape(ev.scoreHistory[ev.scoreHistory.length - 1]?.score);
  if (last && !deep(scoreShape(ev.score), last)) {
    errors.push({ code: "FIRST_RECORD_MISMATCH", path: `${p}.score`, message: "当前 score 与 scoreHistory 最后一条不一致" });
  }
  let prevRev = 0;
  let prevDate = null;
  ev.scoreHistory.forEach((h, i) => {
    const hp = `${p}.scoreHistory[${i}]`;
    if (!isObj(h) || !isInt(h.revision)) {
      errors.push({ code: "WRONG_TYPE", path: `${hp}.revision`, message: "revision 必须是整数" });
      return;
    }
    if (h.revision !== prevRev + 1) {
      errors.push({ code: "HISTORY_REVISION_GAP", path: `${hp}.revision`, message: `revision 必须从 1 连续递增，期望 ${prevRev + 1}` });
    }
    prevRev = h.revision;
    const d = parseDate(h.date);
    if (!d) errors.push({ code: "EVENT_DATE_INVALID", path: `${hp}.date`, message: "修订日期非法" });
    else if (prevDate && d < prevDate) errors.push({ code: "HISTORY_REVISION_GAP", path: `${hp}.date`, message: "修订日期倒序" });
    if (d) prevDate = d;
    if (!isObj(scoreShape(h.score))) errors.push({ code: "WRONG_TYPE", path: `${hp}.score`, message: "修订缺少 score" });
  });
}

function checkJudgment(item, p, errors, label) {
  if (!Array.isArray(item.judgmentHistory) || item.judgmentHistory.length === 0) {
    errors.push({ code: "MISSING_FIELD", path: `${p}.judgmentHistory`, message: "judgmentHistory 至少一条" });
    return;
  }
  const firstJudgment = item.firstJudgment;
  if (!isObj(firstJudgment)) {
    errors.push({ code: "MISSING_FIELD", path: `${p}.firstJudgment`, message: "缺少 firstJudgment" });
  } else {
    const first = item.judgmentHistory[0]?.judgment;
    if (!isObj(first) || !deep(firstJudgment, first)) {
      errors.push({
        code: "FIRST_RECORD_MISMATCH",
        path: `${p}.firstJudgment`,
        message: `首次判断与 judgmentHistory[0].judgment 不一致（${label}）`,
      });
    }
    for (const k of ["date", "text"]) {
      if (!isStr(firstJudgment[k])) {
        errors.push({ code: "MISSING_FIELD", path: `${p}.firstJudgment.${k}`, message: `首次判断缺少 ${k}` });
      }
    }
    const d = parseDate(firstJudgment.date);
    if (firstJudgment.date && !d) {
      errors.push({ code: "EVENT_DATE_INVALID", path: `${p}.firstJudgment.date`, message: "判断日期非法" });
    }
  }
  let prevRev = 0;
  let prevDate = null;
  item.judgmentHistory.forEach((h, i) => {
    const hp = `${p}.judgmentHistory[${i}]`;
    if (!isObj(h) || !isInt(h.revision)) {
      errors.push({ code: "WRONG_TYPE", path: `${hp}.revision`, message: "revision 必须是整数" });
      return;
    }
    if (h.revision !== prevRev + 1) {
      errors.push({ code: "HISTORY_REVISION_GAP", path: `${hp}.revision`, message: `revision 必须连续递增，期望 ${prevRev + 1}` });
    }
    prevRev = h.revision;
    const d = parseDate(h.date);
    if (!d) errors.push({ code: "EVENT_DATE_INVALID", path: `${hp}.date`, message: "判断修订日期非法" });
    else if (prevDate && d < prevDate) errors.push({ code: "HISTORY_REVISION_GAP", path: `${hp}.date`, message: "判断修订日期倒序" });
    if (d) prevDate = d;
    if (!isObj(h.judgment)) errors.push({ code: "WRONG_TYPE", path: `${hp}.judgment`, message: "缺少 judgment" });
  });
}

function checkDates(ev, config, p, errors, warnings, win) {
  if (!isObj(ev.dates)) {
    errors.push({ code: "MISSING_FIELD", path: `${p}.dates`, message: "缺少 dates" });
    return;
  }
  const parsed = {};
  for (const f of DATE_FIELDS) {
    const raw = ev.dates[f];
    const d = parseDate(raw);
    if (!d) {
      errors.push({ code: "EVENT_DATE_INVALID", path: `${p}.dates.${f}`, message: `${f} 缺失或非法日期` });
      continue;
    }
    parsed[f] = d;
    const day = dateOnly(raw);
    // 契约：窗口外旧记录只提示（历史保留、只是不展示）；上界之后才是错。
    if (day < win.start) {
      warnings.push({ code: "EVENT_DATE_OUT_OF_RANGE", path: `${p}.dates.${f}`, message: `${day} 早于窗口 ${win.start}（${win.mode}，历史保留、不展示）` });
    } else if (day > win.end) {
      errors.push({ code: "EVENT_DATE_OUT_OF_RANGE", path: `${p}.dates.${f}`, message: `${day} 晚于窗口 ${win.end}（${win.mode}）` });
    }
  }
  if (parsed.occurred && parsed.collected && parsed.occurred > parsed.collected) {
    warnings.push({ code: "DATE_ORDER", path: `${p}.dates`, message: "发生日晚于抓取日（回溯研究需显式标注）" });
  }
}

export function validateData(doc, config, opts = {}) {
  const win = resolveWindow(config, opts.now);
  const errors = [];
  const warnings = [];
  const push = (code, path, message) => errors.push({ code, path, message });

  if (!isObj(doc)) return { errors: [{ code: "WRONG_TYPE", path: "$", message: "数据根必须是对象" }], warnings, stats: {} };
  if (!Array.isArray(doc.events)) push("WRONG_TYPE", "events", "events 必须是数组");
  if (!Array.isArray(doc.trends)) push("WRONG_TYPE", "trends", "trends 必须是数组");
  if (doc.schemaVersion !== config.schemaVersion) {
    push("SCHEMA_VERSION_MISMATCH", "schemaVersion", `${doc.schemaVersion} != config ${config.schemaVersion}`);
  }
  if (doc.scoringVersion && doc.scoringVersion !== config.scoringVersion) {
    push("SCORE_VERSION_MISSING", "scoringVersion", `${doc.scoringVersion} != config ${config.scoringVersion}`);
  }
  if (!isStr(doc.generatedAt) || !parseDate(doc.generatedAt)) push("MISSING_FIELD", "generatedAt", "缺少 generatedAt");
  if (errors.length) return { errors, warnings, stats: { events: 0, trends: 0 } };

  const events = doc.events;
  const trends = doc.trends;
  const eventIds = new Set();
  const dedupe = new Map();
  const trendIds = new Set();
  const trendById = new Map();
  for (const t of trends) {
    if (!isObj(t) || !isStr(t.id)) {
      push("MISSING_FIELD", "trends[?].id", "趋势缺少 id");
      continue;
    }
    if (trendIds.has(t.id)) push("DUPLICATE_ID", `trends[id=${t.id}]`, `趋势 id 重复: ${t.id}`);
    trendIds.add(t.id);
    trendById.set(t.id, t);
  }

  events.forEach((ev, i) => {
    const p = `events[${i}]`;
    if (!isObj(ev)) {
      push("WRONG_TYPE", p, "事件必须是对象");
      return;
    }
    for (const key of EVENT_REQUIRED) {
      if (ev[key] === undefined || ev[key] === null) push("MISSING_FIELD", `${p}.${key}`, `缺少必填字段 ${key}`);
    }
    if (!isStr(ev.id)) push("MISSING_FIELD", `${p}.id`, "缺少 id");
    else if (eventIds.has(ev.id)) push("DUPLICATE_ID", `${p}.id`, `事件 id 重复: ${ev.id}`);
    else eventIds.add(ev.id);
    if (!isStr(ev.title)) push("MISSING_FIELD", `${p}.title`, "缺少 title");
    if (ev.summary !== undefined && !isStr(ev.summary)) push("WRONG_TYPE", `${p}.summary`, "summary 必须是字符串");
    if (!isStr(ev.dedupeKey)) push("MISSING_FIELD", `${p}.dedupeKey`, "缺少 dedupeKey");
    else if (dedupe.has(ev.dedupeKey)) {
      push("DUPLICATE_DEDUPE_KEY", `${p}.dedupeKey`, `去重键重复: ${ev.dedupeKey}（events[${dedupe.get(ev.dedupeKey)}]）`);
    } else dedupe.set(ev.dedupeKey, i);

    checkDates(ev, config, p, errors, warnings, win);
    checkScore(ev.score, config, p, errors);
    checkFirstScore(ev, p, errors);
    checkJudgment(ev, p, errors, "event");

    if (!Array.isArray(ev.sources) || ev.sources.length === 0) {
      push("SOURCE_MISSING", `${p}.sources`, "至少一条来源");
    } else {
      const seen = new Set();
      ev.sources.forEach((s, j) => {
        const sp = `${p}.sources[${j}]`;
        if (!isObj(s) || !isStr(s.url)) {
          push("SOURCE_URL_INVALID", `${sp}.url`, "来源 URL 缺失");
          return;
        }
        if (!/^https?:\/\/[^\s]+$/i.test(s.url)) push("SOURCE_URL_INVALID", `${sp}.url`, `URL 必须是绝对 http(s): ${s.url}`);
        if (seen.has(s.url)) push("SOURCE_URL_INVALID", `${sp}.url`, `同事件来源重复: ${s.url}`);
        seen.add(s.url);
      });
    }

    if (!Array.isArray(ev.trendLinks)) {
      push("WRONG_TYPE", `${p}.trendLinks`, "trendLinks 必须是数组");
    } else {
      const seen = new Set();
      ev.trendLinks.forEach((ref, j) => {
        const rp = `${p}.trendLinks[${j}]`;
        if (!isObj(ref) || !isStr(ref.trendId)) {
          push("MISSING_FIELD", `${rp}.trendId`, "trendId 缺失");
          return;
        }
        if (!trendIds.has(ref.trendId)) push("TREND_LINK_MISSING", `${rp}.trendId`, `断链：趋势 ${ref.trendId} 不存在`);
        const directions = cfgDirections(config);
        if (!directions.includes(ref.direction)) {
          push("DIRECTION_INVALID", `${rp}.direction`, `方向必须是 ${directions.join("/")}，实际 ${ref.direction}`);
        }
        if (seen.has(ref.trendId)) push("TREND_LINK_MISSING", rp, `重复引用趋势 ${ref.trendId}`);
        seen.add(ref.trendId);
      });
    }
  });

  trends.forEach((tr, i) => {
    const p = `trends[${i}]`;
    if (!isObj(tr)) {
      push("WRONG_TYPE", p, "趋势必须是对象");
      return;
    }
    for (const key of TREND_REQUIRED) {
      if (tr[key] === undefined || tr[key] === null) push("MISSING_FIELD", `${p}.${key}`, `缺少必填字段 ${key}`);
    }
    if (!isStr(tr.id)) push("MISSING_FIELD", `${p}.id`, "缺少 id");
    if (!isStr(tr.name)) push("MISSING_FIELD", `${p}.name`, "缺少 name");
    checkJudgment(tr, p, errors, "trend");
    if (tr.eventIds !== undefined) {
      if (!Array.isArray(tr.eventIds)) push("WRONG_TYPE", `${p}.eventIds`, "eventIds 必须是数组");
      else {
        for (const eid of tr.eventIds) {
          if (!eventIds.has(eid)) push("BACKLINK_MISMATCH", `${p}.eventIds`, `断链：事件 ${eid} 不存在`);
        }
      }
    }
  });

  // 双向一致：事件→趋势有链接时，趋势 eventIds 若存在必须回链。
  events.forEach((ev, i) => {
    if (!isObj(ev) || !Array.isArray(ev.trendLinks)) return;
    ev.trendLinks.forEach((ref, j) => {
      const t = trendById.get(ref?.trendId);
      if (t && Array.isArray(t.eventIds) && !t.eventIds.includes(ev.id)) {
        push(
          "BACKLINK_MISMATCH",
          `events[${i}].trendLinks[${j}]`,
          `事件 ${ev.id} 链到趋势 ${ref.trendId}，但趋势 eventIds 没有回链`,
        );
      }
    });
  });

  if (opts.baseline) {
    const base = opts.baseline;
    const baseEvents = Array.isArray(base?.events) ? base.events : [];
    const baseById = new Map(baseEvents.filter((e) => isObj(e) && isStr(e.id)).map((e) => [e.id, e]));
    events.forEach((ev, i) => {
      const old = baseById.get(ev?.id);
      if (!old) return;
      const p = `events[${i}]`;
      if (old.firstScore && !deep(old.firstScore, ev.firstScore)) {
        push("FIRST_RECORD_OVERWRITTEN", `${p}.firstScore`, "首次评分被改写（相对基线）");
      }
      if (old.firstJudgment && !deep(old.firstJudgment, ev.firstJudgment)) {
        push("FIRST_RECORD_OVERWRITTEN", `${p}.firstJudgment`, "首次判断被改写（相对基线）");
      }
      if (Array.isArray(old.scoreHistory) && (!Array.isArray(ev.scoreHistory) || ev.scoreHistory.length < old.scoreHistory.length)) {
        push("HISTORY_SHRUNK", `${p}.scoreHistory`, `评分历史从 ${old.scoreHistory.length} 条变少`);
      }
      if (Array.isArray(old.judgmentHistory) && (!Array.isArray(ev.judgmentHistory) || ev.judgmentHistory.length < old.judgmentHistory.length)) {
        push("HISTORY_SHRUNK", `${p}.judgmentHistory`, `判断历史从 ${old.judgmentHistory.length} 条变少`);
      }
    });
    for (const old of baseEvents) {
      if (isObj(old) && isStr(old.id) && !eventIds.has(old.id)) {
        warnings.push({ code: "EVENT_DISAPPEARED", path: `events[id=${old.id}]`, message: "基线里的事件在新数据中消失" });
      }
    }
  }

  return {
    errors,
    warnings,
    stats: {
      events: events.length,
      trends: trends.length,
      sources: events.reduce((n, e) => n + (Array.isArray(e?.sources) ? e.sources.length : 0), 0),
      revisions: events.reduce((n, e) => n + (Array.isArray(e?.judgmentHistory) ? e.judgmentHistory.length : 0), 0),
    },
  };
}

/** 从 git HEAD 读基线；不在 git 里或文件不存在时返回 null。 */
export function readGitBaseline(run, dataRelPath) {
  const res = run("git", ["show", `HEAD:${dataRelPath}`]);
  if (res.status !== 0 || !res.stdout) return null;
  try {
    return JSON.parse(res.stdout);
  } catch {
    return null;
  }
}
