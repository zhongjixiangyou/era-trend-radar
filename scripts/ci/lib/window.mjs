/**
 * 统一窗口解析（Node 侧）——与 Python 侧 scripts/_core.py 的 resolve_window 同一口径。
 * 契约见 docs/window-rolling.md：一次运行只解析一次窗口，之后所有阶段复用同一个 asOf。
 *
 * 优先级：--as-of/ERA_AS_OF > --max-ts/ERA_MAX_TS > 配置 window > 时钟。
 * rolling：to = asOf 换算到 Asia/Shanghai 的日历日；from = to-(days-1)；上界 = asOf 时刻本身。
 * fixed：from/to 取 frozen（兼容老 start/end）；上界 = to 23:59:59.999+08:00。
 */

const TZ = "Asia/Shanghai";
const DAY_MS = 86400000;

const dayOf = (d, tz = TZ) => d.toLocaleDateString("sv-SE", { timeZone: tz });

function parseInstant(v, label) {
  if (!v) return null;
  const t = Date.parse(v);
  if (Number.isNaN(t)) throw new Error(`${label} 不是合法 ISO 8601 时间：${v}`);
  return new Date(t);
}

function shiftDay(day, delta) {
  // 只做日历位移，不经过本地时区，避免 DST/时区抖动
  const t = Date.parse(`${day}T12:00:00Z`) + delta * DAY_MS;
  return new Date(t).toISOString().slice(0, 10);
}

/** 某时区里 `YYYY-MM-DD` 当天 00:00 对应的 UTC 毫秒。
 *  fromTs 曾经硬编码 +08:00，timezone 不是 Asia/Shanghai 时会多放/少放几小时（审稿实测 UTC 多放 8 小时）。 */
function zonedMidnightUtc(dateStr, timeZone) {
  const [y, m, d] = dateStr.split("-").map(Number);
  const guess = Date.UTC(y, m - 1, d, 0, 0, 0);
  if (!timeZone || timeZone === "Asia/Shanghai") return guess - 8 * 3600 * 1000;
  const dtf = new Intl.DateTimeFormat("en-US", {
    timeZone, hour12: false,
    year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit",
  });
  const parts = Object.fromEntries(dtf.formatToParts(new Date(guess)).map((p) => [p.type, p.value]));
  const asUtc = Date.UTC(+parts.year, +parts.month - 1, +parts.day, +parts.hour % 24, +parts.minute, +parts.second);
  return guess - (asUtc - guess);
}

export function resolveWindow(config = {}, opts = {}) {
  const w = config?.window || {};
  const mode = String(opts.mode || w.mode || "fixed").toLowerCase();
  if (mode !== "rolling" && mode !== "fixed") throw new Error(`window.mode 只能是 rolling|fixed，收到 ${mode}`);
  const timezone = w.timezone || TZ;
  const days = Number(opts.days || w.days || 180);
  if (mode === "rolling" && (!Number.isInteger(days) || days < 1)) throw new Error(`window.days 必须是正整数，收到 ${days}`);

  const cliAsOf = opts.asOf ? parseInstant(opts.asOf, "--as-of") : null;
  const envAsOf = process.env.ERA_AS_OF ? parseInstant(process.env.ERA_AS_OF, "ERA_AS_OF") : null;
  const cliMax = opts.maxTs ? parseInstant(opts.maxTs, "--max-ts") : null;
  const envMax = process.env.ERA_MAX_TS ? parseInstant(process.env.ERA_MAX_TS, "ERA_MAX_TS") : null;

  // asOfSource=从哪来（cli/env/arg/clock），asOfFrom=用了哪个参数（as-of/max-ts/clock）——
  // 两者分开，才不会把 --max-ts 误报成 --as-of（契约 §3）。
  let asOfSource = "clock";
  let asOfFrom = "clock";
  let asOf = new Date();
  if (cliAsOf) { asOf = cliAsOf; asOfSource = "cli"; asOfFrom = "as-of"; }
  else if (envAsOf) { asOf = envAsOf; asOfSource = "env"; asOfFrom = "as-of"; }
  else if (cliMax) { asOf = cliMax; asOfSource = "cli"; asOfFrom = "max-ts"; }
  else if (envMax) { asOf = envMax; asOfSource = "env"; asOfFrom = "max-ts"; }
  else if (opts.now) { asOf = new Date(opts.now); asOfSource = "arg"; asOfFrom = "as-of"; }

  if (mode === "fixed") {
    const frozen = w.frozen && (w.frozen.start || w.frozen.end) ? w.frozen : w;
    const from = frozen.start;
    const to = frozen.end;
    if (!from || !to) throw new Error("fixed 模式缺少 window.start/end（或 window.frozen.start/end）");
    const upper = cliMax || envMax || new Date(Date.parse(`${to}T23:59:59.999+08:00`));
    return {
      mode, from, to, days: null, timezone,
      asOf: asOf.toISOString(), asOfSource, asOfFrom,
      upper: upper.toISOString(),
      fromTs: new Date(zonedMidnightUtc(from, timezone)).toISOString(),
      frozenAt: frozen.frozenAt || to,
    };
  }

  const to = dayOf(asOf, timezone);
  const from = shiftDay(to, -(days - 1));
  return {
    mode, from, to, days, timezone,
    asOf: asOf.toISOString(), asOfSource, asOfFrom,
    upper: asOf.toISOString(),
    fromTs: new Date(zonedMidnightUtc(from, timezone)).toISOString(),
  };
}

/** 判定一条记录的时间是否落在窗口内：日期型按日历日比，时刻型按 [from 00:00+08, upper] 比。 */
export function judge(dateOrTs, win) {
  if (typeof dateOrTs !== "string" || !/^\d{4}-\d{2}-\d{2}/.test(dateOrTs.trim())) return "invalid";
  const s = dateOrTs.trim();
  const dateOnly = s.length === 10;
  if (dateOnly) {
    if (s < win.from) return "old";
    if (s > win.to) return "future";
    return "ok";
  }
  const t = Date.parse(s);
  if (Number.isNaN(t)) return "invalid";
  if (t > Date.parse(win.upper)) return "future";
  if (t < Date.parse(win.fromTs)) return "old";
  return "ok";
}

/**
 * 当前生效窗口（报告/页面文案用）：优先吃 CI 钉好的 ERA_WINDOW_*，
 * 没有就按配置+环境重算一遍。任何脚本都不要自己写死日期。
 */
export function currentWindow(config = {}) {
  const envFrom = process.env.ERA_WINDOW_FROM;
  const envTo = process.env.ERA_WINDOW_TO;
  const envUpper = process.env.ERA_WINDOW_UPPER;
  if (envFrom && envTo) {
    return {
      mode: process.env.ERA_WINDOW_MODE || "rolling",
      from: envFrom,
      to: envTo,
      days: Number(process.env.ERA_WINDOW_DAYS || 0) || null,
      asOf: process.env.ERA_AS_OF || null,
      asOfSource: "env",
      upper: envUpper || null,
    };
  }
  return resolveWindow(config);
}
