"""ERA 数据层公共库 —— 只依赖 Python 标准库，GitHub Actions 可直接跑。

包含：JSON 加载、三张表（event/trend/forecast）校验、六维评分、去重、报告渲染。

约定
----
* 目录根 = 本文件上一级（scripts/ 的父目录），可用环境变量 ERA_WORK_DIR 覆盖。
* 时间上限默认 2026-11-13T23:59:59+08:00，可用 ERA_MAX_TS 覆盖。
* 退出码（各入口脚本统一）：0=通过 / 1=发现错误 / 2=用法或读取失败 / 3=来源失败 / 4=无密钥（分析未执行）。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = "era-schema/v1"
EXIT_OK, EXIT_FAIL, EXIT_USAGE = 0, 1, 2
EXIT_SOURCE, EXIT_NO_KEY = 3, 4  # 3=来源/网络失败；4=无密钥（分析未执行）


def era_home() -> Path:
    env = os.environ.get("ERA_WORK_DIR")
    if env:
        return Path(env).expanduser().resolve()
    return Path(__file__).resolve().parent.parent


ERA_HOME = era_home()
CONTENT_DIR = ERA_HOME / "content"
REPORTS_DIR = ERA_HOME / "reports"
SCHEMA_DIR = ERA_HOME / "schema"

CATEGORIES = ("全球宏观", "中国经济与产业", "科技与社会", "未来趋势")
SOURCE_TYPES = ("primary", "corroboration")
CRED_LEVELS = ("verified", "partial", "unverified")
STATUSES = ("published", "pending")
DIRECTIONS = ("support", "weaken", "challenge")
STAGES = ("emerging", "strengthening", "stable", "weakening", "falsified")
OUTCOMES = ("hit", "partial", "miss", "unresolved")
EVIDENCE_GRADES = ("multi_independent", "single_primary", "secondary_only", "unverified")
VERIFY_STATUSES = ("not_started", "pending", "passed", "failed")
CHECK_RESULTS = ("pass", "fail", "unknown")
STRONG_EVIDENCE = ("single_primary", "multi_independent")
FC_STATUSES = ("open", "resolved", "void")

DIMS = ("structural", "scope", "signal", "delta", "timeliness", "crossDomain")
DIM_CAPS = {"structural": 25, "scope": 20, "signal": 20, "delta": 15, "timeliness": 10, "crossDomain": 10}
DIM_CN = {
    "structural": "结构性",
    "scope": "影响范围",
    "signal": "信号强度",
    "delta": "变化幅度",
    "timeliness": "时效性",
    "crossDomain": "跨域外溢",
}
DIM_GUIDE = {
    "structural": "是否触及制度/结构层：规则、产能、人口结构、技术范式；一次性事件得 0-8，结构性变化 18-25",
    "scope": "波及面：全球/多国 16-20，全国 11-15，行业 6-10，单机构 ≤5",
    "signal": "证据硬度：一手数据/官方原文 16-20，权威媒体+多源 11-15，单一二手 6-10，仅传闻 ≤5",
    "delta": "与现状的偏离：范式级 11-15，明显超预期 7-10，小幅 4-6，符合预期 ≤3",
    "timeliness": "距 collectedAt：≤3 天 9-10，≤7 天 7-8，≤14 天 4-6，≤30 天 2-3，更久 ≤1",
    "crossDomain": "跨领域外溢：≥3 个领域 8-10，2 个领域 5-7，1 个 0-4",
}

CN_TZ = dt.timezone(dt.timedelta(hours=8))
MAX_TS_DEFAULT = "2026-11-13T23:59:59+08:00"
_MAX_DT: dt.datetime | None = None
_ACTIVE_WINDOW: "Window | None" = None

ID_PATTERNS = {
    "event": r"^ev_[a-z0-9][a-z0-9_-]{2,63}$",
    "trend": r"^tr_[a-z0-9][a-z0-9_-]{2,63}$",
    "forecast": r"^fc_[a-z0-9][a-z0-9_-]{2,63}$",
}
DEDUPE_KEY_RE = r"^[a-z0-9][a-z0-9._:-]{7,127}$"
SCORER_VERSION_RE = r"^score/v\d+\.\d+$"

def _schema_keys(name: str, fallback: set[str]) -> set[str]:
    """字段白名单以 schema 文件为唯一权威；读不到才退回内置集合，避免契约与校验漂移。"""
    path = SCHEMA_DIR / f"{name}.schema.json"
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        keys = set((doc.get("properties") or {}).keys())
        return keys or fallback
    except (OSError, json.JSONDecodeError):
        return fallback


EVENT_KEYS = _schema_keys("event", {
    "id", "dedupeKey", "title", "summary", "facts", "analysis", "category", "region",
    "occurredAt", "firstReportedAt", "collectedAt", "sources", "scores", "credibility",
    "status", "trendLinks", "revisions", "forecastRefs", "retro",
    "evidenceGrade", "verification", "scoreHistory", "linkHistory", "milestones",
})
TREND_KEYS = _schema_keys("trend", {
    "id", "title", "thesis", "firstSeenAt", "stage", "indicators", "evidenceRefs",
    "counterEvidence", "currentJudgment", "reviewBy", "revisions", "counterSearch",
})
FORECAST_KEYS = _schema_keys("forecast", {
    "id", "statement", "createdAt", "resolveBy", "probability", "rationale",
    "indicators", "linkedTrendIds", "evidenceRefs", "status", "resolution", "revisions",
    "firstJudgment",
})


# --------------------------------------------------------------------------- #
# 时间
# --------------------------------------------------------------------------- #
_ISO_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})"
    r"(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,6}))?)?(Z|[+-]\d{2}:\d{2})?)?$"
)


def set_max_ts(value: str) -> None:
    """只设时间上限（兼容老入口）：清掉已解析的窗口，避免两条口径打架。"""
    global MAX_TS_DEFAULT, _MAX_DT, _ACTIVE_WINDOW
    MAX_TS_DEFAULT = value
    _MAX_DT = None
    _ACTIVE_WINDOW = None


def max_dt() -> dt.datetime:
    """数据时间上限：有窗口时由窗口上界驱动，否则退回 ERA_MAX_TS/内置常量。"""
    global _MAX_DT
    if _ACTIVE_WINDOW is not None:
        return _ACTIVE_WINDOW.upper
    if _MAX_DT is None:
        parsed = parse_ts(MAX_TS_DEFAULT)
        if parsed is None:
            raise SystemExit(f"ERA_MAX_TS 不是合法 ISO 8601 时间: {MAX_TS_DEFAULT}")
        _MAX_DT = as_aware(parsed)
    return _MAX_DT


def parse_ts(value: Any) -> dt.datetime | None:
    """解析 ISO 8601，返回 datetime（可能无时区）。非法返回 None。"""
    if not isinstance(value, str):
        return None
    m = _ISO_RE.match(value.strip())
    if not m:
        return None
    y, mo, d, hh, mi, ss, frac, tz = m.groups()
    try:
        micro = int((frac or "0").ljust(6, "0")[:6])
        tzinfo = None
        if tz == "Z":
            tzinfo = dt.timezone.utc
        elif tz:
            sign = 1 if tz[0] == "+" else -1
            tzinfo = dt.timezone(sign * dt.timedelta(hours=int(tz[1:3]), minutes=int(tz[4:6])))
        return dt.datetime(int(y), int(mo), int(d), int(hh or 0), int(mi or 0), int(ss or 0), micro, tzinfo)
    except ValueError:
        return None


def as_aware(value: dt.datetime) -> dt.datetime:
    return value if value.tzinfo else value.replace(tzinfo=CN_TZ)


def now_ts() -> dt.datetime:
    return dt.datetime.now(tz=CN_TZ)


def fmt_ts(value: dt.datetime | None) -> str:
    if value is None:
        return "-"
    return value.isoformat(timespec="seconds")


def today_str() -> str:
    return now_ts().strftime("%Y-%m-%d")


# --------------------------------------------------------------------------- #
# 窗口 —— 唯一口径见 docs/window-rolling.md
# 与 Node 侧 scripts/ci/lib/window.mjs 一一对应；两边边界必须一致，
# 任何一侧改了边界都要同步改另一侧，并补 docs 里 §6 的边界用例。
# --------------------------------------------------------------------------- #
WINDOW_MODES = ("rolling", "fixed")
DEFAULT_WINDOW_DAYS = 180
DEFAULT_WINDOW_TZ = "Asia/Shanghai"

JUDGE_OK, JUDGE_OLD, JUDGE_FUTURE, JUDGE_INVALID = "ok", "old", "future", "invalid"
_CN_TZ_NAMES = {"asia/shanghai", "cn", "prc", "utc+8", "+08:00", "+0800", "etc/gmt-8"}


def tz_for(name: str) -> dt.tzinfo:
    """Asia/Shanghai 走固定 +08:00（无夏令时，不用依赖 tzdata）；其它时区尽力解析。"""
    key = (name or DEFAULT_WINDOW_TZ).strip()
    if key.lower() in _CN_TZ_NAMES:
        return CN_TZ
    try:
        from zoneinfo import ZoneInfo  # noqa: PLC0415 —— 只有非默认时区才需要

        return ZoneInfo(key)
    except Exception:  # noqa: BLE001 —— 缺时区库/写错名字时退回 +08:00，不阻断流水线
        return CN_TZ


@dataclass(frozen=True)
class Window:
    """一次运行解析出来的唯一窗口；所有阶段都复用它，不许各自取 now()。"""

    mode: str
    from_date: dt.date
    to_date: dt.date
    upper: dt.datetime          # 上界（含）：rolling=asOf 时刻；fixed=to 23:59:59.999+08
    from_ts: dt.datetime        # 下界（含）：from 00:00:00+08
    as_of: dt.datetime
    as_of_source: str           # 谁给的：cli / env / arg / clock（枚举保持不变）
    as_of_from: str = "clock"  # 走哪条通道：as-of / max-ts / clock / arg
    days: int | None = None
    timezone: str = DEFAULT_WINDOW_TZ
    frozen_at: str | None = None

    @property
    def from_str(self) -> str:
        return self.from_date.isoformat()

    @property
    def to_str(self) -> str:
        return self.to_date.isoformat()

    def judge(self, value: Any) -> str:
        """判定时间/日期落在窗口哪一侧：ok / old / future / invalid。

        日期型按日历日比（date==to 合法）；时刻型按 [from 00:00+08, upper] 比。
        """
        if isinstance(value, dt.datetime):
            ts = as_aware(value)
            return self._judge_ts(ts)
        if isinstance(value, dt.date):
            return self._judge_date(value.isoformat())
        if not isinstance(value, str):
            return JUDGE_INVALID
        text = value.strip()
        if not text:
            return JUDGE_INVALID
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            return self._judge_date(text)
        parsed = parse_ts(text)
        if parsed is None:
            return JUDGE_INVALID
        return self._judge_ts(as_aware(parsed))

    def _judge_date(self, day: str) -> str:
        if day < self.from_str:
            return JUDGE_OLD
        if day > self.to_str:
            return JUDGE_FUTURE
        return JUDGE_OK

    def _judge_ts(self, ts: dt.datetime) -> str:
        if ts > self.upper:
            return JUDGE_FUTURE
        if ts < self.from_ts:
            return JUDGE_OLD
        return JUDGE_OK

    def covers(self, value: Any) -> bool:
        return self.judge(value) == JUDGE_OK

    def as_meta(self) -> dict:
        """站点/导出产物里的 meta.window；rolling 不写 frozenAt。"""
        meta: dict[str, Any] = {
            "from": self.from_str,
            "to": self.to_str,
            "mode": self.mode,
            "asOf": fmt_ts(self.as_of),
            "asOfFrom": self.as_of_from,
            "days": self.days,
        }
        if self.mode == "fixed" and self.frozen_at:
            meta["frozenAt"] = self.frozen_at
        return meta

    def as_report(self) -> dict:
        """各阶段报告里的 window（比 meta 多 asOfSource，便于追是哪一级给的 asOf）。"""
        report = self.as_meta()
        report["asOfSource"] = self.as_of_source
        return report


def _instant(value: str, label: str) -> dt.datetime:
    parsed = parse_ts(value)
    if parsed is None:
        raise SystemExit(f"{label} 不是合法 ISO 8601 时间: {value}")
    return as_aware(parsed)


def _day(value: str, label: str) -> dt.date:
    text = str(value).strip()
    try:
        return dt.date.fromisoformat(text[:10])
    except ValueError:
        raise SystemExit(f"{label} 不是合法日期（YYYY-MM-DD）: {value}") from None


def resolve_window(
    config: Any = None,
    *,
    as_of: str | None = None,
    max_ts: str | None = None,
    now: dt.datetime | None = None,
    mode: str | None = None,
    days: int | None = None,
    use_env: bool = True,
) -> Window:
    """按 docs/window-rolling.md 解析窗口（纯函数，不设全局状态）。

    优先级：--as-of/ERA_AS_OF > --max-ts/ERA_MAX_TS > 配置 window > 时钟。
    rolling：to=asOf 在配置时区里的日历日，from=to-(days-1)，上界=asOf 时刻本身。
    fixed：from/to 取 frozen.start/end（兼容老 start/end），上界=to 23:59:59.999+08，
           max-ts 可覆盖上界，日期仍取配置。
    """
    cfg = config if isinstance(config, dict) else {}
    w = cfg.get("window") if isinstance(cfg.get("window"), dict) else {}

    win_mode = str(mode or w.get("mode") or "fixed").strip().lower()
    if win_mode not in WINDOW_MODES:
        raise SystemExit(f"window.mode 只能是 rolling|fixed，收到 {win_mode}")
    tzname = str(w.get("timezone") or w.get("tz") or DEFAULT_WINDOW_TZ)
    tz = tz_for(tzname)
    win_days = int(days or w.get("days") or DEFAULT_WINDOW_DAYS)
    if win_mode == "rolling" and win_days < 1:
        raise SystemExit(f"window.days 必须是正整数，收到 {win_days}")

    cli_as_of = _instant(as_of, "--as-of") if as_of else None
    env_as_of = None
    cli_max = _instant(max_ts, "--max-ts") if max_ts else None
    env_max = None
    if use_env:
        raw_env_as_of = os.environ.get("ERA_AS_OF")
        raw_env_max = os.environ.get("ERA_MAX_TS")
        env_as_of = _instant(raw_env_as_of, "ERA_AS_OF") if raw_env_as_of else None
        env_max = _instant(raw_env_max, "ERA_MAX_TS") if raw_env_max else None

    # as_of_source=谁给的（cli/env/arg/clock，枚举不动）；as_of_from=走哪条通道（as-of/max-ts/arg/clock）
    as_of_source, as_of_from, as_of_dt = "clock", "clock", now_ts()
    if cli_as_of is not None:
        as_of_source, as_of_from, as_of_dt = "cli", "as-of", cli_as_of
    elif env_as_of is not None:
        as_of_source, as_of_from, as_of_dt = "env", "as-of", env_as_of
    elif cli_max is not None:
        as_of_source, as_of_from, as_of_dt = "cli", "max-ts", cli_max
    elif env_max is not None:
        as_of_source, as_of_from, as_of_dt = "env", "max-ts", env_max
    elif now is not None:
        as_of_source, as_of_from, as_of_dt = "arg", "arg", as_aware(now)

    # CI 用 window.mjs 钉过一轮 asOf 时会多导 ERA_WINDOW_AS_OF_FROM：
    # 以它为准，避免「CI 里是 --max-ts、Python 报成 as-of」两套说法。
    if cli_as_of is None and env_as_of is not None:
        pinned = (os.environ.get("ERA_WINDOW_AS_OF_FROM") or "").strip()
        if pinned in ("as-of", "max-ts", "clock", "arg"):
            as_of_from = pinned

    if win_mode == "fixed":
        frozen_raw = w.get("frozen")
        frozen = (
            frozen_raw
            if isinstance(frozen_raw, dict) and (frozen_raw.get("start") or frozen_raw.get("end"))
            else w
        )
        start_raw, end_raw = frozen.get("start"), frozen.get("end")
        if not start_raw or not end_raw:
            raise SystemExit("window.mode=fixed 缺少 window.start/end（或 window.frozen.start/end）")
        from_date = _day(start_raw, "window.frozen.start")
        to_date = _day(end_raw, "window.frozen.end")
        upper = cli_max or env_max or dt.datetime.combine(
            to_date, dt.time(23, 59, 59, 999000), CN_TZ
        )
        return Window(
            mode="fixed",
            from_date=from_date,
            to_date=to_date,
            upper=upper,
            from_ts=dt.datetime.combine(from_date, dt.time(0, 0), CN_TZ),
            as_of=as_of_dt,
            as_of_source=as_of_source,
            as_of_from=as_of_from,
            days=None,
            timezone=tzname,
            frozen_at=str(frozen.get("frozenAt") or end_raw),
        )

    to_date = as_of_dt.astimezone(tz).date()
    from_date = to_date - dt.timedelta(days=win_days - 1)
    return Window(
        mode="rolling",
        from_date=from_date,
        to_date=to_date,
        upper=as_of_dt,
        from_ts=dt.datetime.combine(from_date, dt.time(0, 0), CN_TZ),
        as_of=as_of_dt,
        as_of_source=as_of_source,
        as_of_from=as_of_from,
        days=win_days,
        timezone=tzname,
    )


def set_window(win: Window | None) -> None:
    """把窗口设为本次运行的有效窗口；max_dt() 随之上移到窗口上界。"""
    global _ACTIVE_WINDOW, _MAX_DT
    _ACTIVE_WINDOW = win
    _MAX_DT = win.upper if win else None


def active_window() -> Window | None:
    return _ACTIVE_WINDOW


def apply_window(
    config: Any = None,
    *,
    as_of: str | None = None,
    max_ts: str | None = None,
    now: dt.datetime | None = None,
) -> Window:
    """解析并设为有效窗口（各入口脚本一次调用即可）。"""
    win = resolve_window(config, as_of=as_of, max_ts=max_ts, now=now)
    set_window(win)
    return win


def load_pipeline_config(path: str | os.PathLike | None = None) -> dict:
    """读 pipeline.config.json；缺文件返回空 dict（由 resolve_window 决定怎么报错）。"""
    p = Path(path) if path else ERA_HOME / "pipeline.config.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


# --------------------------------------------------------------------------- #
# 文本
# --------------------------------------------------------------------------- #
def char_len(text: str) -> int:
    """按"字"计数：不计空白。"""
    return len(re.sub(r"\s+", "", text))


def short(value: Any, limit: int = 70) -> str:
    if isinstance(value, str):
        s = value
    else:
        s = json.dumps(value, ensure_ascii=False)
    s = s.replace("\n", " ").replace("\r", " ")
    return s if len(s) <= limit else s[: limit - 1] + "…"


def typename(value: Any) -> str:
    if isinstance(value, bool):
        return "布尔"
    if isinstance(value, (int, float)):
        return "数字"
    if isinstance(value, str):
        return "字符串"
    if isinstance(value, list):
        return "数组"
    if isinstance(value, dict):
        return "对象"
    if value is None:
        return "null"
    return type(value).__name__


def norm_title(text: str) -> str:
    s = (text or "").lower()
    s = re.sub(r"[\s\u3000\u200b]+", "", s)
    s = re.sub(r"[^\w\u4e00-\u9fff]+", "", s)
    return s


def bigrams(text: str) -> set[str]:
    if len(text) < 2:
        return {text} if text else set()
    return {text[i: i + 2] for i in range(len(text) - 1)}


def similarity(a: str, b: str) -> float:
    """标题相似度：字符二元组 Jaccard 与序列相似度取较大者。"""
    na, nb = norm_title(a), norm_title(b)
    if not na or not nb:
        return 0.0
    ga, gb = bigrams(na), bigrams(nb)
    union = ga | gb
    jac = len(ga & gb) / len(union) if union else 0.0
    ratio = SequenceMatcher(None, na, nb).ratio()
    return max(jac, ratio * 0.92)


def make_dedupe_key(title: str, region: str = "", date: str = "") -> str:
    base = "|".join([
        norm_title(title),
        re.sub(r"\s+", "", (region or "").lower()),
        re.sub(r"[^0-9]", "", (date or ""))[:8],
    ])
    return "ev:" + hashlib.sha1(base.encode("utf-8")).hexdigest()[:12]


# --------------------------------------------------------------------------- #
# 校验结果
# --------------------------------------------------------------------------- #
@dataclass
class Finding:
    level: str  # error | warn
    code: str
    path: str
    field: str
    message: str

    def as_dict(self) -> dict:
        return {
            "level": self.level,
            "code": self.code,
            "path": self.path,
            "field": self.field,
            "message": self.message,
        }

    def fmt(self) -> str:
        loc = f"{self.path}#{self.field}" if self.field else self.path
        return f"{self.level.upper():<5} [{self.code}] {loc}: {self.message}"


class Ctx:
    def __init__(self, strict_refs: bool = False):
        self.findings: list[Finding] = []
        self.strict_refs = strict_refs
        self.ids: dict[str, str] = {}
        self.dedupe_keys: dict[str, str] = {}
        self.id_sets: dict[str, set[str]] = {"event": set(), "trend": set(), "forecast": set()}
        self.stats = {"records": 0, "event": 0, "trend": 0, "forecast": 0}

    def err(self, code: str, path: str, field: str, message: str) -> None:
        self.findings.append(Finding("error", code, path, field, message))

    def warn(self, code: str, path: str, field: str, message: str) -> None:
        self.findings.append(Finding("warn", code, path, field, message))

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.level == "error"]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.level == "warn"]

    @property
    def ok(self) -> bool:
        return not self.errors


_MISSING = object()


class Field:
    """带路径前缀的字段读取 + 校验。"""

    def __init__(self, ctx: Ctx, data: Any, label: str, prefix: str = ""):
        self.ctx, self.d, self.label, self.prefix = ctx, data, label, prefix

    def sub(self, data: Any, suffix: str) -> "Field":
        return Field(self.ctx, data, self.label, f"{self.prefix}{suffix}")

    def _name(self, key: str) -> str:
        return f"{self.prefix}{key}"

    def _get(self, key: str, required: bool) -> Any:
        if not isinstance(self.d, dict):
            return _MISSING
        value = self.d.get(key, _MISSING)
        if value is _MISSING or value is None:
            if required:
                self.ctx.err("F_MISSING", self.label, self._name(key), f"缺少必填字段 {self._name(key)}")
            return _MISSING
        if isinstance(value, str) and not value.strip():
            if required:
                self.ctx.err("F_EMPTY", self.label, self._name(key), f"{self._name(key)} 为空字符串")
            return _MISSING
        return value

    def text(
        self,
        key: str,
        *,
        min_len: int = 1,
        max_len: int | None = None,
        required: bool = True,
        pattern: str | None = None,
        enum: Iterable[str] | None = None,
    ) -> str | None:
        value = self._get(key, required)
        if value is _MISSING:
            return None
        if not isinstance(value, str):
            self.ctx.err("F_TYPE", self.label, self._name(key),
                         f"{self._name(key)} 应为字符串，实际 {typename(value)}")
            return None
        length = char_len(value)
        if length < min_len:
            self.ctx.err("F_LEN", self.label, self._name(key),
                         f"{self._name(key)} 至少 {min_len} 字，实际 {length} 字")
            return value
        if max_len is not None and length > max_len:
            self.ctx.err("F_LEN", self.label, self._name(key),
                         f"{self._name(key)} 最多 {max_len} 字，实际 {length} 字（{short(value, 40)}）")
        if enum is not None and value not in tuple(enum):
            self.ctx.err("F_ENUM", self.label, self._name(key),
                         f"{self._name(key)}='{value}' 不在允许值 {'/'.join(enum)} 内")
        if pattern is not None and not re.search(pattern, value):
            self.ctx.err("F_PATTERN", self.label, self._name(key),
                         f"{self._name(key)}='{short(value, 40)}' 不符合格式 {pattern}")
        return value

    def number(self, key: str, *, lo: float | None = None, hi: float | None = None,
               required: bool = True) -> float | None:
        value = self._get(key, required)
        if value is _MISSING:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            self.ctx.err("F_TYPE", self.label, self._name(key),
                         f"{self._name(key)} 应为数字，实际 {typename(value)}")
            return None
        if lo is not None and value < lo:
            self.ctx.err("F_RANGE", self.label, self._name(key), f"{self._name(key)}={value:g} 小于下限 {lo:g}")
        if hi is not None and value > hi:
            self.ctx.err("F_RANGE", self.label, self._name(key), f"{self._name(key)}={value:g} 超过上限 {hi:g}")
        return float(value)

    def boolean(self, key: str, *, required: bool = True) -> bool | None:
        value = self._get(key, required)
        if value is _MISSING:
            return None
        if not isinstance(value, bool):
            self.ctx.err("F_TYPE", self.label, self._name(key),
                         f"{self._name(key)} 应为布尔，实际 {typename(value)}")
            return None
        return value

    def arr(self, key: str, *, min_items: int = 0, max_items: int | None = None,
            required: bool = True) -> list | None:
        value = self._get(key, required)
        if value is _MISSING:
            if required is False and min_items == 0:
                return []
            return None
        if not isinstance(value, list):
            self.ctx.err("F_TYPE", self.label, self._name(key),
                         f"{self._name(key)} 应为数组，实际 {typename(value)}")
            return None
        if len(value) < min_items:
            self.ctx.err("F_LEN", self.label, self._name(key),
                         f"{self._name(key)} 至少 {min_items} 项，实际 {len(value)} 项")
        if max_items is not None and len(value) > max_items:
            self.ctx.err("F_LEN", self.label, self._name(key),
                         f"{self._name(key)} 最多 {max_items} 项，实际 {len(value)} 项")
        return value

    def obj(self, key: str, *, required: bool = True) -> dict | None:
        value = self._get(key, required)
        if value is _MISSING:
            return None
        if not isinstance(value, dict):
            self.ctx.err("F_TYPE", self.label, self._name(key),
                         f"{self._name(key)} 应为对象，实际 {typename(value)}")
            return None
        return value

    def ts(self, key: str, *, required: bool = True, want_tz: bool = False,
           limit: bool = True) -> dt.datetime | None:
        raw = self._get(key, required)
        if raw is _MISSING:
            return None
        parsed = parse_ts(raw)
        if parsed is None:
            self.ctx.err("TS_INVALID", self.label, self._name(key),
                         f"{self._name(key)}='{short(raw, 40)}' 不是合法 ISO 8601 时间")
            return None
        date_only = bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(raw).strip()))
        if want_tz and parsed.tzinfo is None and not date_only:
            self.ctx.warn("TS_NO_TZ", self.label, self._name(key),
                          f"{self._name(key)} 未带时区，按 +08:00 解释（建议写全 2026-03-01T09:00:00+08:00）")
        aware = as_aware(parsed)
        if limit and aware > max_dt():
            self.ctx.err("TS_FUTURE", self.label, self._name(key),
                         f"{self._name(key)}={fmt_ts(aware)} 晚于数据上限 {fmt_ts(max_dt())}")
        return aware


# --------------------------------------------------------------------------- #
# 记录加载
# --------------------------------------------------------------------------- #
@dataclass
class Record:
    kind: str
    data: dict
    label: str
    src: Path

    @property
    def rid(self) -> str:
        return str(self.data.get("id", ""))


BUNDLE_KEYS = {"event": "events", "trend": "trends", "forecast": "forecasts"}


def guess_kind(obj: Any) -> str | None:
    if not isinstance(obj, dict):
        return None
    rid = obj.get("id")
    if isinstance(rid, str):
        if rid.startswith("ev_"):
            return "event"
        if rid.startswith("tr_"):
            return "trend"
        if rid.startswith("fc_"):
            return "forecast"
    if "dedupeKey" in obj or "facts" in obj or "credibility" in obj:
        return "event"
    if "thesis" in obj or "stage" in obj or "counterEvidence" in obj:
        return "trend"
    if "resolveBy" in obj or "probability" in obj or "statement" in obj:
        return "forecast"
    return None


def rel_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ERA_HOME))
    except Exception:
        return str(path)


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def iter_json_files(paths: Iterable[str]) -> list[Path]:
    files: list[Path] = []
    for raw in paths:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = (Path.cwd() / path)
        if path.is_dir():
            for item in sorted(path.rglob("*.json")):
                rel = item.relative_to(path)
                # 跳过 _intake/、.cache/ 这类过程目录与 inbox/：候选与草稿不是正式内容
                if any(part.startswith(("_", ".")) or part == "inbox" for part in rel.parts):
                    continue
                files.append(item)
        elif path.is_file():
            files.append(path)
        else:
            raise FileNotFoundError(str(raw))
    return files


def load_paths(paths: Iterable[str]) -> tuple[list[Record], list[Finding], list[Path]]:
    """读入 JSON（单条 / 数组 / {events,trends,forecasts} 集合），返回记录与读取错误。"""
    findings: list[Finding] = []
    records: list[Record] = []
    files = iter_json_files(paths)
    for path in files:
        label = rel_path(path)
        try:
            doc = read_json(path)
        except json.JSONDecodeError as exc:
            findings.append(Finding("error", "E_JSON", label, "", f"JSON 解析失败：{exc.msg}（第 {exc.lineno} 行第 {exc.colno} 列）"))
            continue
        except OSError as exc:
            findings.append(Finding("error", "E_READ", label, "", f"读取失败：{exc}"))
            continue

        if isinstance(doc, dict) and not doc.get("id") and any(k in doc for k in BUNDLE_KEYS.values()):
            for kind, key in BUNDLE_KEYS.items():
                items = doc.get(key)
                if items is None:
                    continue
                if not isinstance(items, list):
                    findings.append(Finding("error", "E_SHAPE", label, key, f"{key} 应为数组，实际 {typename(items)}"))
                    continue
                for i, obj in enumerate(items):
                    if not isinstance(obj, dict):
                        findings.append(Finding("error", "E_SHAPE", label, f"{key}[{i}]", "记录应为对象"))
                        continue
                    records.append(Record(kind, obj, f"{label}#{key}[{i}]", path))
            continue

        items = doc if isinstance(doc, list) else [doc]
        for i, obj in enumerate(items):
            if not isinstance(obj, dict):
                findings.append(Finding("error", "E_SHAPE", label, f"[{i}]", f"记录应为对象，实际 {typename(obj)}"))
                continue
            kind = guess_kind(obj)
            suffix = f"[{i}]" if isinstance(doc, list) else ""
            if kind is None:
                findings.append(Finding("error", "E_KIND", f"{label}{suffix}", "id",
                                        "无法判断记录类型（id 应以 ev_/tr_/fc_ 开头，或含 dedupeKey/thesis/resolveBy）"))
                continue
            records.append(Record(kind, obj, f"{label}{suffix}", path))
    return records, findings, files


# --------------------------------------------------------------------------- #
# 六维评分
# --------------------------------------------------------------------------- #
def sum_dims(values: dict[str, float]) -> float:
    total = sum(values[d] for d in DIMS)
    return int(total) if float(total).is_integer() else round(total, 2)


def validate_scores(ctx: Ctx, label: str, scores: Any, prefix: str = "scores.") -> None:
    if scores is None:
        return
    if not isinstance(scores, dict):
        ctx.err("F_TYPE", label, prefix.rstrip("."), f"scores 应为对象，实际 {typename(scores)}")
        return
    fld = Field(ctx, scores, label, prefix)
    for key in sorted(set(scores) - set(DIMS) - {"total", "rationale", "scorerVersion"}):
        ctx.warn("W_UNKNOWN_FIELD", label, f"{prefix}{key}", f"未知评分字段 {key}（会被忽略）")

    values: dict[str, float] = {}
    for dim in DIMS:
        value = fld.number(dim, lo=0, hi=DIM_CAPS[dim])
        if value is not None:
            values[dim] = value

    total = fld.number("total", lo=0, hi=100)

    rationale = fld.obj("rationale")
    if rationale is not None:
        rfld = Field(ctx, rationale, label, f"{prefix}rationale.")
        for dim in DIMS:
            rfld.text(dim, min_len=6, max_len=400)
        for key in sorted(set(rationale) - set(DIMS)):
            # 契约：维度名与 scores 一致（delta / crossDomain）。
            # novelty / cross 是历史别名，不允许长期并存，直接报错而不是告警。
            if key in {"novelty", "cross"}:
                ctx.err("E_RATIONALE_ALIAS", label, f"{prefix}rationale.{key}",
                        f"维度名 {key} 不在契约内；scores 与 rationale 必须同用 "
                        f"{'delta' if key == 'novelty' else 'crossDomain'}")
            else:
                ctx.warn("W_UNKNOWN_FIELD", label, f"{prefix}rationale.{key}", f"未知维度 {key}")

    fld.text("scorerVersion", min_len=6, max_len=32, pattern=SCORER_VERSION_RE)

    if len(values) == len(DIMS) and total is not None:
        expected = sum_dims(values)
        if abs(total - expected) > 1e-6:
            detail = "+".join(f"{values[d]:g}" for d in DIMS)
            ctx.err("SCORE_TOTAL_MISMATCH", label, f"{prefix}total",
                    f"total={total:g} 与六维之和 {expected:g}（{detail}）不符；用 score.py apply 重算，禁止手填")


# --------------------------------------------------------------------------- #
# 三张表校验
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# v2 新增：证据等级 / 核验 / 历史不可覆盖 / 发布门禁
# --------------------------------------------------------------------------- #
def verification(ctx: Ctx, label: str, value: Any) -> None:
    if value is None:
        return
    if not isinstance(value, dict):
        ctx.err("F_TYPE", label, "verification", f"verification 应为对象，实际 {typename(value)}")
        return
    sub = Field(ctx, value, label, "verification.")
    sub.text("status", enum=VERIFY_STATUSES)
    sub.text("method", min_len=2, max_len=200, required=False)
    sub.ts("checkedAt", required=False, want_tz=True, limit=False)
    sub.text("note", required=False, max_len=300)
    checks = sub.arr("checks", min_items=0, max_items=12, required=False)
    if checks:
        for i, item in enumerate(checks):
            if not isinstance(item, dict):
                ctx.err("F_TYPE", label, f"verification.checks[{i}]", "应为对象")
                continue
            c = Field(ctx, item, label, f"verification.checks[{i}].")
            c.text("kind", min_len=2, max_len=40)
            c.text("result", enum=CHECK_RESULTS)
            c.text("evidence", min_len=2, max_len=300, required=False)
            for key in sorted(set(item) - {"kind", "result", "evidence"}):
                ctx.warn("W_UNKNOWN_FIELD", label, f"verification.checks[{i}].{key}", f"未知字段 {key}")
        # passed 不得与 unknown 检查共存：靠声明过门，等于没核验
        if value.get("status") == "passed":
            unk = [i for i, it in enumerate(checks)
                   if isinstance(it, dict) and it.get("result") == "unknown"]
            if unk:
                ctx.err("VERIFY_PASS_WITH_UNKNOWN", label, "verification.status",
                        f"status=passed 但 checks 里有 {len(unk)} 条 unknown（索引 {unk}）："
                        "未闭合的核验不得判通过，请补原文事实或降为 pending")
            if not any(isinstance(it, dict) and it.get("kind") == "source_text" for it in checks):
                ctx.err("VERIFY_PASS_LINK_ONLY", label, "verification.status",
                        "status=passed 但没有任何 kind=source_text 的原文核对记录："
                        "HTTP 可访问只是链接检查，不等于事实核验通过，请降为 pending")
    for key in sorted(set(value) - {"status", "method", "checkedAt", "checks", "note"}):
        ctx.warn("W_UNKNOWN_FIELD", label, f"verification.{key}", f"未知字段 {key}")


def evidence_grade(ctx: Ctx, label: str, value: Any, status: Any, verif: Any) -> None:
    if status == "published":
        got = verif.get("status") if isinstance(verif, dict) else None
        if got != "passed":
            ctx.err("PUB_NOT_VERIFIED", label, "verification.status",
                    f"published 要求 verification.status=passed，实际 {got or '缺失'}")
        if isinstance(verif, dict) and not (verif.get("checks") or []):
            ctx.err("PUB_NO_CHECKS", label, "verification.checks",
                    "published 要求 verification.checks 非空，核验记录必须可追溯")
    if value is None:
        if status == "published":
            ctx.err("PUB_NO_EVIDENCE_GRADE", label, "evidenceGrade",
                    "status=published 必须写 evidenceGrade（single_primary|multi_independent）")
        return
    if not isinstance(value, str):
        ctx.err("F_TYPE", label, "evidenceGrade", f"应为字符串，实际 {typename(value)}")
        return
    if value not in EVIDENCE_GRADES:
        ctx.err("F_ENUM", label, "evidenceGrade",
                f"evidenceGrade='{value}' 不在允许值 {'/'.join(EVIDENCE_GRADES)} 内")
        return
    if status != "published":
        return
    if value not in STRONG_EVIDENCE:
        ctx.err("PUB_WEAK_EVIDENCE", label, "evidenceGrade",
                f"published 要求 single_primary/multi_independent，实际 {value}")


def score_history(ctx: Ctx, label: str, items: Any) -> None:
    if items is None:
        return
    if not isinstance(items, list):
        ctx.err("F_TYPE", label, "scoreHistory", f"应为数组，实际 {typename(items)}")
        return
    if len(items) > 100:
        ctx.err("F_LEN", label, "scoreHistory", f"最多 100 条，实际 {len(items)}")
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            ctx.err("F_TYPE", label, f"scoreHistory[{i}]", "应为对象")
            continue
        h = Field(ctx, item, label, f"scoreHistory[{i}].")
        h.ts("at", want_tz=True, limit=False)
        h.text("by", min_len=1, max_len=40, required=False)
        h.text("note", min_len=2, max_len=200, required=False)
        scores = h.obj("scores", required=False)
        if isinstance(scores, dict):
            for key in sorted(set(scores) - set(DIMS) - {"total"}):
                ctx.warn("W_UNKNOWN_FIELD", label, f"scoreHistory[{i}].scores.{key}", f"未知字段 {key}")
        for key in sorted(set(item) - {"at", "by", "note", "scores"}):
            ctx.warn("W_UNKNOWN_FIELD", label, f"scoreHistory[{i}].{key}", f"未知字段 {key}")


def link_history(ctx: Ctx, label: str, items: Any) -> None:
    if items is None:
        return
    if not isinstance(items, list):
        ctx.err("F_TYPE", label, "linkHistory", f"应为数组，实际 {typename(items)}")
        return
    if len(items) > 200:
        ctx.err("F_LEN", label, "linkHistory", f"最多 200 条，实际 {len(items)}")
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            ctx.err("F_TYPE", label, f"linkHistory[{i}]", "应为对象")
            continue
        h = Field(ctx, item, label, f"linkHistory[{i}].")
        h.ts("at", want_tz=True, limit=False)
        h.text("trendId", min_len=6, max_len=64, pattern=ID_PATTERNS["trend"])
        h.text("direction", enum=DIRECTIONS)
        h.text("action", enum=("add", "remove", "change"))
        h.text("reason", min_len=2, max_len=200, required=False)
        for key in sorted(set(item) - {"at", "trendId", "direction", "action", "reason"}):
            ctx.warn("W_UNKNOWN_FIELD", label, f"linkHistory[{i}].{key}", f"未知字段 {key}")


def counter_search(ctx: Ctx, label: str, value: Any) -> None:
    if value is None:
        return
    if not isinstance(value, dict):
        ctx.err("F_TYPE", label, "counterSearch", f"应为对象，实际 {typename(value)}")
        return
    sub = Field(ctx, value, label, "counterSearch.")
    sub.ts("at", want_tz=True, limit=False)
    sub.arr("sources", min_items=0, max_items=20, required=False)
    sub.arr("keywords", min_items=0, max_items=20, required=False)
    sub.text("window", min_len=2, max_len=60, required=False)
    n = sub.number("hits", lo=0, hi=10000, required=False)
    sub.text("result", enum=("found", "not_found"))
    sub.text("note", min_len=2, max_len=400, required=False)
    if n is not None and n > 0 and value.get("result") == "not_found":
        ctx.warn("COUNTER_HITS_CONFLICT", label, "counterSearch.result",
                 f"hits={int(n)} 但 result=not_found，二者矛盾")
    for key in sorted(set(value) - {"at", "sources", "keywords", "window", "hits", "result", "note"}):
        ctx.warn("W_UNKNOWN_FIELD", label, f"counterSearch.{key}", f"未知字段 {key}")


def first_judgment(ctx: Ctx, label: str, value: Any, created: Any) -> None:
    if value is None:
        return
    if not isinstance(value, dict):
        ctx.err("F_TYPE", label, "firstJudgment", f"应为对象，实际 {typename(value)}")
        return
    sub = Field(ctx, value, label, "firstJudgment.")
    sub.text("statement", min_len=10, max_len=400)
    at = sub.ts("createdAt", want_tz=True, limit=False)
    sub.number("probability", lo=0, hi=1, required=False)
    if created and at and as_aware(at) > as_aware(created):
        ctx.err("TS_ORDER", label, "firstJudgment.createdAt",
                f"firstJudgment.createdAt={fmt_ts(at)} 不能晚于 createdAt={fmt_ts(created)}")
    for key in sorted(set(value) - {"statement", "createdAt", "probability"}):
        ctx.warn("W_UNKNOWN_FIELD", label, f"firstJudgment.{key}", f"未知字段 {key}")


# ---- 追加历史：与基线（git HEAD 或 --baseline 目录）逐项比对 ----
def _canon(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


HISTORY_ARRAYS = ("revisions", "scoreHistory", "linkHistory")


def _git_baseline(label: str) -> dict | None:
    try:
        out = subprocess.run(["git", "show", f"HEAD:{label}"], cwd=str(ERA_HOME),
                             capture_output=True, text=True, timeout=20)
    except Exception:
        return None
    if out.returncode != 0:
        return None
    try:
        data = json.loads(out.stdout)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _dir_baseline(label: str, root: Path) -> dict | None:
    p = Path(label)
    if p.is_absolute():
        cands = [root / p.name]
        try:
            cands.insert(0, root / p.relative_to(ERA_HOME))
        except ValueError:
            pass
    else:
        cands = [root / p]
    for c in cands:
        if c.is_file():
            try:
                data = json.loads(c.read_text(encoding="utf-8"))
            except Exception:
                return None
            return data if isinstance(data, dict) else None
    return None


def check_history(records: list[Record], ctx: Ctx, baseline_dir: str | None = None,
                  mode: str = "auto") -> None:
    if mode == "off":
        return
    root = Path(baseline_dir).expanduser().resolve() if baseline_dir else None
    checked = 0
    for rec in records:
        if mode == "baseline" or root is not None:
            old = _dir_baseline(rec.label, root) if root else None
        else:
            old = _git_baseline(rec.label)
        if not isinstance(old, dict):
            continue
        checked += 1
        for key in HISTORY_ARRAYS:
            prev, cur = old.get(key), rec.data.get(key)
            if not isinstance(prev, list) or not isinstance(cur, list):
                continue
            if len(cur) < len(prev):
                ctx.err("HISTORY_REWRITE", rec.label, key,
                        f"{key} 从 {len(prev)} 条减到 {len(cur)} 条：历史只能追加，不能删除")
                continue
            for i, (a, b) in enumerate(zip(prev, cur)):
                if _canon(a) != _canon(b):
                    ctx.err("HISTORY_REWRITE", rec.label, f"{key}[{i}]",
                            f"{key}[{i}] 与基线（git HEAD/基线目录）不同：已发布历史只能追加，不能改写")
                    break
        prev_fj, cur_fj = old.get("firstJudgment"), rec.data.get("firstJudgment")
        if isinstance(prev_fj, dict):
            if not isinstance(cur_fj, dict):
                ctx.err("HISTORY_FIRST_JUDGMENT", rec.label, "firstJudgment",
                        "首次判断不可删除（firstJudgment 缺失）")
            elif _canon(prev_fj) != _canon(cur_fj):
                ctx.err("HISTORY_FIRST_JUDGMENT", rec.label, "firstJudgment",
                        "首次判断不可覆盖：只能原样保留（新的判断写 currentJudgment/revisions）")
    if checked == 0 and mode in ("git", "baseline"):
        ctx.warn("HISTORY_UNCHECKED", "-", "-",
                 "没有可用基线（非 git 仓库且未给 --baseline），本次未检查追加性")


def validate_event(ctx: Ctx, label: str, data: dict) -> None:
    for key in sorted(set(data) - EVENT_KEYS):
        ctx.warn("W_UNKNOWN_FIELD", label, key, f"未知字段 {key}（契约外字段，前端会忽略）")

    f = Field(ctx, data, label)
    f.text("id", min_len=6, max_len=64, pattern=ID_PATTERNS["event"])
    f.text("dedupeKey", min_len=8, max_len=128, pattern=DEDUPE_KEY_RE)
    f.text("title", min_len=4, max_len=120)
    f.text("summary", min_len=10, max_len=120)
    facts(f, ctx)
    f.text("analysis", min_len=30, max_len=2000)
    f.text("category", enum=CATEGORIES)
    f.text("region", min_len=2, max_len=60)

    occurred = f.ts("occurredAt", want_tz=True, limit=True)
    win = active_window()
    if win is not None and occurred is not None and win.judge(occurred) == JUDGE_OLD:
        # 旧记录只降级为 warning：content/ 里保留，只是不参与窗口展示，
        # 不能因为历史数据把整库判负（docs/window-rolling.md）。
        ctx.warn("WINDOW_OUT", label, "occurredAt",
                 f"occurredAt={fmt_ts(occurred)} 早于窗口下界 {win.from_str}（历史记录保留，不参与窗口展示）")
    # 上界只约束「事件发生时间」（occurredAt）。firstReportedAt/collectedAt 是簿记时间：
    # CI 里 asOf 在采集前钉死，采集本身发生在 asOf 之后几秒，若也卡上界会把自己判成未来数据。
    reported = f.ts("firstReportedAt", want_tz=True, limit=False)
    collected = f.ts("collectedAt", want_tz=True, limit=False)
    if win is not None and collected is not None and win.upper is not None and collected > win.upper:
        # 簿记时间不判负（契约 §2），但窗口钳制失效时要有可观测信号。
        ctx.warn("WINDOW_BOOKKEEPING_FUTURE", label, "collectedAt",
                 f"collectedAt={fmt_ts(collected)} 晚于窗口上界 {fmt_ts(win.upper)}（簿记时间不判负，请确认窗口是否生效或回放 asOf 是否偏早）")
    if occurred and collected and occurred > collected + dt.timedelta(days=1):
        ctx.err("TS_ORDER", label, "occurredAt",
                f"occurredAt={fmt_ts(occurred)} 晚于 collectedAt={fmt_ts(collected)}")
    if reported and collected and reported > collected + dt.timedelta(hours=1):
        ctx.err("TS_ORDER", label, "firstReportedAt",
                f"firstReportedAt={fmt_ts(reported)} 晚于 collectedAt={fmt_ts(collected)}")
    if occurred and reported and occurred > reported + dt.timedelta(days=2):
        ctx.warn("TS_EVENT_AFTER_REPORT", label, "occurredAt",
                 f"occurredAt={fmt_ts(occurred)} 比首报 {fmt_ts(reported)} 晚 2 天以上（预告类事件请写清）")

    sources(ctx, label, data.get("sources"))
    validate_scores(ctx, label, data.get("scores"))
    credibility(ctx, label, data.get("credibility"), data.get("sources"), data.get("status"))
    verification(ctx, label, data.get("verification"))
    evidence_grade(ctx, label, data.get("evidenceGrade"), data.get("status"), data.get("verification"))
    score_history(ctx, label, data.get("scoreHistory"))
    link_history(ctx, label, data.get("linkHistory"))
    f.text("status", enum=STATUSES)
    trend_links(ctx, label, data.get("trendLinks"))
    revisions(ctx, label, data.get("revisions"), allow_stage=True)
    forecast_refs(ctx, label, data.get("forecastRefs"))
    retro(ctx, label, data.get("retro"), reported, collected)


def facts(f: Field, ctx: Ctx) -> None:
    items = f.arr("facts", min_items=1, max_items=30)
    if items is None:
        return
    seen: set[str] = set()
    for i, item in enumerate(items):
        if isinstance(item, str):
            Field(ctx, {"v": item}, f.label, f"facts[{i}].").text("v", min_len=4, max_len=240)
            norm = norm_title(item)
        elif isinstance(item, dict):
            sub = Field(ctx, item, f.label, f"facts[{i}].")
            sub.text("text", min_len=4, max_len=240)
            sub.text("sourceUrl", min_len=8, max_len=500, required=False, pattern=r"^https?://")
            norm = norm_title(str(item.get("text", "")))
        else:
            ctx.err("F_TYPE", f.label, f"facts[{i}]", f"应为字符串或 {{text,sourceUrl}}，实际 {typename(item)}")
            continue
        if norm and norm in seen:
            ctx.warn("W_FACT_DUP", f.label, f"facts[{i}]", f"与前面的条目重复：{short(item if isinstance(item, str) else item.get('text'), 30)}")
        seen.add(norm)


def sources(ctx: Ctx, label: str, items: Any) -> None:
    if items is None:
        return
    if not isinstance(items, list):
        ctx.err("F_TYPE", label, "sources", f"sources 应为数组，实际 {typename(items)}")
        return
    if len(items) < 1:
        ctx.err("F_LEN", label, "sources", "sources 至少 1 个来源")
    if len(items) > 30:
        ctx.err("F_LEN", label, "sources", f"sources 最多 30 个，实际 {len(items)}")
    urls: dict[str, int] = {}
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            ctx.err("F_TYPE", label, f"sources[{i}]", f"应为对象，实际 {typename(item)}")
            continue
        sub = Field(ctx, item, label, f"sources[{i}].")
        url = sub.text("url", min_len=10, max_len=600, pattern=r"^https?://")
        sub.text("title", min_len=2, max_len=200)
        sub.text("publisher", min_len=2, max_len=80)
        sub.ts("publishedAt", want_tz=False, limit=False)
        sub.text("type", enum=SOURCE_TYPES)
        sub.text("note", min_len=1, max_len=200, required=False)
        for key in sorted(set(item) - {"url", "title", "publisher", "publishedAt", "type", "note"}):
            ctx.warn("W_UNKNOWN_FIELD", label, f"sources[{i}].{key}", f"未知来源字段 {key}")
        if url:
            norm = url.strip().rstrip("/")
            if norm in urls:
                ctx.err("SRC_DUP_URL", label, f"sources[{i}].url",
                        f"与 sources[{urls[norm]}] 重复：{short(url, 60)}")
            else:
                urls[norm] = i


def credibility(ctx: Ctx, label: str, cred: Any, srcs: Any, status: Any) -> None:
    if cred is None:
        return
    if not isinstance(cred, dict):
        ctx.err("F_TYPE", label, "credibility", f"credibility 应为对象，实际 {typename(cred)}")
        return
    sub = Field(ctx, cred, label, "credibility.")
    level = sub.text("level", enum=CRED_LEVELS)
    sub.text("basis", min_len=10, max_len=400)
    for key in sorted(set(cred) - {"level", "basis"}):
        ctx.warn("W_UNKNOWN_FIELD", label, f"credibility.{key}", f"未知字段 {key}")

    list_src = srcs if isinstance(srcs, list) else []
    typed = [s for s in list_src if isinstance(s, dict) and s.get("type") in SOURCE_TYPES]
    primary = [s for s in typed if s.get("type") == "primary"]

    if status == "published" and not primary:
        ctx.err("PUB_NO_PRIMARY", label, "status",
                "status=published 但 sources 里没有 type=primary 的一手来源（发布必须有一手）")
    if level == "verified":
        if not primary and status != "published":
            ctx.err("CRED_NO_PRIMARY", label, "credibility.level",
                    "level=verified 但 sources 里没有 type=primary 的一手来源")
        if len(typed) < 2:
            ctx.err("CRED_TOO_THIN", label, "credibility.level",
                    f"level=verified 至少需要 2 个来源（1 个 primary + 1 个印证），实际 {len(typed)} 个")
    elif level == "partial":
        if not typed:
            ctx.warn("CRED_NO_SOURCE", label, "credibility.level",
                     "level=partial 建议至少 1 个可核查来源")
    if status == "published" and level == "unverified":
        ctx.err("PUB_UNVERIFIED", label, "status",
                "status=published 但 credibility.level=unverified（unverified 只能 pending）")


def trend_links(ctx: Ctx, label: str, items: Any) -> None:
    if items is None:
        return
    if not isinstance(items, list):
        ctx.err("F_TYPE", label, "trendLinks", f"trendLinks 应为数组，实际 {typename(items)}")
        return
    if len(items) > 20:
        ctx.err("F_LEN", label, "trendLinks", f"trendLinks 最多 20 条，实际 {len(items)}")
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            ctx.err("F_TYPE", label, f"trendLinks[{i}]", f"应为对象，实际 {typename(item)}")
            continue
        sub = Field(ctx, item, label, f"trendLinks[{i}].")
        sub.text("trendId", min_len=6, max_len=64, pattern=ID_PATTERNS["trend"])
        sub.text("direction", enum=DIRECTIONS)
        sub.text("reason", min_len=5, max_len=300)
        for key in sorted(set(item) - {"trendId", "direction", "reason"}):
            ctx.warn("W_UNKNOWN_FIELD", label, f"trendLinks[{i}].{key}", f"未知字段 {key}")


def revisions(ctx: Ctx, label: str, items: Any, allow_stage: bool = False) -> None:
    if items is None:
        return
    if not isinstance(items, list):
        ctx.err("F_TYPE", label, "revisions", f"revisions 应为数组，实际 {typename(items)}")
        return
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            ctx.err("F_TYPE", label, f"revisions[{i}]", f"应为对象，实际 {typename(item)}")
            continue
        sub = Field(ctx, item, label, f"revisions[{i}].")
        sub.ts("at", want_tz=True, limit=False)
        sub.text("by", min_len=1, max_len=40, required=False)
        sub.text("note", min_len=4, max_len=400)
        if allow_stage and "stageBefore" in item:
            sub.text("stageBefore", enum=STAGES, required=False)
        for key in sorted(set(item) - {"at", "by", "note"} - ({"stageBefore"} if allow_stage else set())):
            ctx.warn("W_UNKNOWN_FIELD", label, f"revisions[{i}].{key}", f"未知字段 {key}")


def forecast_refs(ctx: Ctx, label: str, items: Any) -> None:
    if items is None:
        return
    if not isinstance(items, list):
        ctx.err("F_TYPE", label, "forecastRefs", f"forecastRefs 应为数组，实际 {typename(items)}")
        return
    seen: set[str] = set()
    for i, item in enumerate(items):
        if not isinstance(item, str):
            ctx.err("F_TYPE", label, f"forecastRefs[{i}]", f"应为 forecast id 字符串，实际 {typename(item)}")
            continue
        if not re.search(ID_PATTERNS["forecast"], item):
            ctx.err("F_PATTERN", label, f"forecastRefs[{i}]", f"'{short(item, 40)}' 不符合 fc_<slug> 格式")
        if item in seen:
            ctx.err("REF_DUP", label, f"forecastRefs[{i}]", f"重复引用 {item}")
        seen.add(item)


def retro(ctx: Ctx, label: str, item: Any, reported: dt.datetime | None, collected: dt.datetime | None) -> None:
    if item is None:
        return
    if not isinstance(item, dict):
        ctx.err("F_TYPE", label, "retro", f"retro 应为对象 {{isRetro,...}}，实际 {typename(item)}")
        return
    sub = Field(ctx, item, label, "retro.")
    is_retro = sub.boolean("isRetro")
    as_of = sub.ts("asOf", required=False, want_tz=True, limit=False)
    note = sub.text("note", min_len=8, max_len=400, required=False)
    for key in sorted(set(item) - {"isRetro", "asOf", "note"}):
        ctx.warn("W_UNKNOWN_FIELD", label, f"retro.{key}", f"未知字段 {key}")

    if is_retro:
        if as_of is None:
            ctx.err("RETRO_NO_ASOF", label, "retro.asOf", "isRetro=true 必须写 asOf（补录时的时间点）")
        if not note:
            ctx.err("RETRO_NO_NOTE", label, "retro.note",
                    "isRetro=true 必须写 note：当时已知什么、哪些是事后才补的")
    else:
        if as_of is not None:
            ctx.warn("RETRO_ASOF_IGNORED", label, "retro.asOf", "isRetro=false 时 asOf 无意义，建议置 null")
        if reported and collected:
            gap = collected - reported
            if gap > dt.timedelta(days=30):
                ctx.warn("RETRO_SUSPECT", label, "retro.isRetro",
                         f"collectedAt 比 firstReportedAt 晚 {gap.days} 天却标 isRetro=false，确认是否事后补录")


def validate_trend(ctx: Ctx, label: str, data: dict) -> None:
    for key in sorted(set(data) - TREND_KEYS):
        ctx.warn("W_UNKNOWN_FIELD", label, key, f"未知字段 {key}（契约外字段，前端会忽略）")
    f = Field(ctx, data, label)
    f.text("id", min_len=6, max_len=64, pattern=ID_PATTERNS["trend"])
    f.text("thesis", min_len=10, max_len=200)
    first_seen = f.ts("firstSeenAt", want_tz=False, limit=False)
    f.text("stage", enum=STAGES)
    indicators(ctx, label, data.get("indicators"))
    evidence_refs(ctx, label, data.get("evidenceRefs"))
    counter_evidence(ctx, label, data.get("counterEvidence"))
    f.text("currentJudgment", min_len=20, max_len=2000)
    review_by = f.ts("reviewBy", want_tz=False, limit=False)
    if review_by and first_seen and as_aware(review_by) <= as_aware(first_seen):
        ctx.err("TS_ORDER", label, "reviewBy",
                f"reviewBy={fmt_ts(review_by)} 应晚于 firstSeenAt={fmt_ts(first_seen)}")
    if review_by and review_by.date() < now_ts().date():
        ctx.warn("TREND_REVIEW_OVERDUE", label, "reviewBy",
                 f"reviewBy={review_by.date()} 已过期，该复核了（复核后更新 stage/currentJudgment/revisions）")
    revisions(ctx, label, data.get("revisions"), allow_stage=True)


def indicators(ctx: Ctx, label: str, items: Any) -> None:
    if items is None:
        return
    if not isinstance(items, list):
        ctx.err("F_TYPE", label, "indicators", f"indicators 应为数组，实际 {typename(items)}")
        return
    if len(items) < 1:
        ctx.err("F_LEN", label, "indicators", "indicators 至少 1 项（否则无法判断趋势强弱）")
    if len(items) > 12:
        ctx.err("F_LEN", label, "indicators", f"indicators 最多 12 项，实际 {len(items)}")
    for i, item in enumerate(items):
        if isinstance(item, str):
            Field(ctx, {"v": item}, label, f"indicators[{i}].").text("v", min_len=4, max_len=200)
        elif isinstance(item, dict):
            sub = Field(ctx, item, label, f"indicators[{i}].")
            sub.text("name", min_len=4, max_len=120)
            sub.text("note", min_len=1, max_len=200, required=False)
            for key in sorted(set(item) - {"name", "note"}):
                ctx.warn("W_UNKNOWN_FIELD", label, f"indicators[{i}].{key}", f"未知字段 {key}")
        else:
            ctx.err("F_TYPE", label, f"indicators[{i}]", f"应为字符串或 {{name,note}}，实际 {typename(item)}")


def counter_evidence(ctx: Ctx, label: str, items: Any) -> None:
    if items is None:
        return
    if not isinstance(items, list):
        ctx.err("F_TYPE", label, "counterEvidence", f"counterEvidence 应为数组，实际 {typename(items)}")
        return
    if len(items) < 1:
        ctx.err("F_LEN", label, "counterEvidence", "反证必写：至少 1 条；确无明确反证也要写清检索范围与结果")
        return
    if len(items) > 20:
        ctx.err("F_LEN", label, "counterEvidence", f"counterEvidence 最多 20 条，实际 {len(items)}")
    weak = 0
    for i, item in enumerate(items):
        text = None
        if isinstance(item, str):
            text = item
            Field(ctx, {"v": item}, label, f"counterEvidence[{i}].").text("v", min_len=4, max_len=400)
        elif isinstance(item, dict):
            sub = Field(ctx, item, label, f"counterEvidence[{i}].")
            text = sub.text("text", min_len=4, max_len=400)
            sub.text("url", min_len=10, max_len=600, required=False, pattern=r"^https?://")
            for key in sorted(set(item) - {"text", "url"}):
                ctx.warn("W_UNKNOWN_FIELD", label, f"counterEvidence[{i}].{key}", f"未知字段 {key}")
        else:
            ctx.err("F_TYPE", label, f"counterEvidence[{i}]", f"应为字符串或 {{text,url}}，实际 {typename(item)}")
            continue
        if text and re.match(r"^(无|暂无|未发现|没有)", text.strip()):
            weak += 1
    if weak == len(items):
        ctx.warn("COUNTER_WEAK", label, "counterEvidence",
                 "反证全部写成'无/未发现'：请补充检索范围、关键词与时间窗，否则视为未做反证检查")


def evidence_refs(ctx: Ctx, label: str, items: Any) -> None:
    if items is None:
        return
    if not isinstance(items, list):
        ctx.err("F_TYPE", label, "evidenceRefs", f"evidenceRefs 应为数组，实际 {typename(items)}")
        return
    seen: set[str] = set()
    for i, item in enumerate(items):
        if not isinstance(item, str):
            ctx.err("F_TYPE", label, f"evidenceRefs[{i}]", f"应为 event id 字符串，实际 {typename(item)}")
            continue
        if not re.search(ID_PATTERNS["event"], item):
            ctx.err("F_PATTERN", label, f"evidenceRefs[{i}]", f"'{short(item, 40)}' 不符合 ev_<slug> 格式")
        if item in seen:
            ctx.err("REF_DUP", label, f"evidenceRefs[{i}]", f"重复引用 {item}")
        seen.add(item)


def validate_forecast(ctx: Ctx, label: str, data: dict) -> None:
    for key in sorted(set(data) - FORECAST_KEYS):
        ctx.warn("W_UNKNOWN_FIELD", label, key, f"未知字段 {key}（契约外字段，前端会忽略）")
    f = Field(ctx, data, label)
    f.text("id", min_len=6, max_len=64, pattern=ID_PATTERNS["forecast"])
    f.text("statement", min_len=10, max_len=200)
    created = f.ts("createdAt", want_tz=True, limit=False)
    resolve_by = f.ts("resolveBy", want_tz=False, limit=False)
    f.number("probability", lo=0, hi=1)
    f.text("rationale", min_len=20, max_len=2000)
    indicators(ctx, label, data.get("indicators"))
    linked_trends(ctx, label, data.get("linkedTrendIds"))
    evidence_refs(ctx, label, data.get("evidenceRefs"))
    status = f.text("status", enum=FC_STATUSES)
    resolution(ctx, label, data.get("resolution"), status, created, resolve_by)
    if resolve_by and created and as_aware(resolve_by) <= as_aware(created):
        ctx.err("TS_ORDER", label, "resolveBy",
                f"resolveBy={fmt_ts(resolve_by)} 应晚于 createdAt={fmt_ts(created)}")
    if status == "open" and resolve_by and resolve_by.date() < now_ts().date():
        ctx.warn("FORECAST_OVERDUE", label, "resolveBy",
                 f"已过判定截止日 {resolve_by.date()} 但 status=open；请判定并写 resolution")
    revisions(ctx, label, data.get("revisions"), allow_stage=True)
    first_judgment(ctx, label, data.get("firstJudgment"), created)


def linked_trends(ctx: Ctx, label: str, items: Any) -> None:
    if items is None:
        return
    if not isinstance(items, list):
        ctx.err("F_TYPE", label, "linkedTrendIds", f"linkedTrendIds 应为数组，实际 {typename(items)}")
        return
    seen: set[str] = set()
    for i, item in enumerate(items):
        if not isinstance(item, str):
            ctx.err("F_TYPE", label, f"linkedTrendIds[{i}]", f"应为 trend id 字符串，实际 {typename(item)}")
            continue
        if not re.search(ID_PATTERNS["trend"], item):
            ctx.err("F_PATTERN", label, f"linkedTrendIds[{i}]", f"'{short(item, 40)}' 不符合 tr_<slug> 格式")
        if item in seen:
            ctx.err("REF_DUP", label, f"linkedTrendIds[{i}]", f"重复引用 {item}")
        seen.add(item)


def resolution(ctx: Ctx, label: str, item: Any, status: str | None,
               created: dt.datetime | None, resolve_by: dt.datetime | None) -> None:
    if status == "resolved":
        if not isinstance(item, dict):
            ctx.err("F_MISSING", label, "resolution", "status=resolved 必须写 resolution 对象")
            return
        sub = Field(ctx, item, label, "resolution.")
        resolved_at = sub.ts("resolvedAt", want_tz=True, limit=False)
        outcome = sub.text("outcome", enum=OUTCOMES)
        sub.text("note", min_len=10, max_len=400)
        urls = sub.arr("evidenceUrls", min_items=0, max_items=20, required=False)
        if urls:
            for i, url in enumerate(urls):
                if not isinstance(url, str) or not re.search(r"^https?://", url):
                    ctx.err("F_PATTERN", label, f"resolution.evidenceUrls[{i}]", f"应为 http(s) 链接：{short(url, 50)}")
        for key in sorted(set(item) - {"resolvedAt", "outcome", "note", "evidenceUrls"}):
            ctx.warn("W_UNKNOWN_FIELD", label, f"resolution.{key}", f"未知字段 {key}")
        if resolved_at and created and resolved_at < as_aware(created):
            ctx.err("TS_ORDER", label, "resolution.resolvedAt",
                    f"resolvedAt={fmt_ts(resolved_at)} 早于 createdAt={fmt_ts(created)}")
        if resolved_at and resolve_by and resolved_at > as_aware(resolve_by) + dt.timedelta(days=1):
            ctx.warn("RESOLVE_LATE", label, "resolution.resolvedAt",
                     f"resolvedAt={fmt_ts(resolved_at)} 晚于 resolveBy={fmt_ts(resolve_by)}（迟到判定，请说明）")
        if outcome == "unresolved":
            ctx.warn("OUTCOME_UNRESOLVED", label, "resolution.outcome",
                     "到期仍无法判定：建议把 statement/indicators 写得更可判，避免下次重复")
    elif item is not None:
        ctx.err("RESOLUTION_NOT_NULL", label, "resolution", f"status={status or '(缺失)'} 时 resolution 必须为 null")


# --------------------------------------------------------------------------- #
# 顶层校验
# --------------------------------------------------------------------------- #
def validate_records(records: list[Record], strict_refs: bool | None = None) -> Ctx:
    if strict_refs is None:
        strict_refs = False
    ctx = Ctx(strict_refs=strict_refs)
    ctx._records = records  # type: ignore[attr-defined]

    # pass 1：id / dedupeKey 冲突（也供引用解析）
    for rec in records:
        ctx.stats["records"] += 1
        ctx.stats[rec.kind] += 1
        rid = rec.data.get("id")
        if isinstance(rid, str) and rid:
            if rid in ctx.ids:
                ctx.err("ID_DUP", rec.label, "id", f"id={rid} 与 {ctx.ids[rid]} 重复")
            else:
                ctx.ids[rid] = rec.label
                ctx.id_sets[rec.kind].add(rid)
        if rec.kind == "event":
            dkey = rec.data.get("dedupeKey")
            if isinstance(dkey, str) and dkey:
                if dkey in ctx.dedupe_keys:
                    ctx.err("DEDUPE_KEY_DUP", rec.label, "dedupeKey",
                            f"dedupeKey={dkey} 与 {ctx.dedupe_keys[dkey]} 重复（同一事件被收两次）")
                else:
                    ctx.dedupe_keys[dkey] = rec.label

    # pass 2：字段级
    for rec in records:
        if rec.kind == "event":
            validate_event(ctx, rec.label, rec.data)
        elif rec.kind == "trend":
            validate_trend(ctx, rec.label, rec.data)
        else:
            validate_forecast(ctx, rec.label, rec.data)

    # pass 3：跨记录引用
    known_event, known_trend, known_forecast = ctx.id_sets["event"], ctx.id_sets["trend"], ctx.id_sets["forecast"]
    level_fn = ctx.err if ctx.strict_refs else ctx.warn
    code_suffix = "" if ctx.strict_refs else "_UNVERIFIED"

    for rec in records:
        data = rec.data
        if rec.kind == "event":
            for i, link in enumerate(data.get("trendLinks") or []):
                if isinstance(link, dict) and isinstance(link.get("trendId"), str):
                    if link["trendId"] not in known_trend:
                        level_fn(f"REF{code_suffix or '_MISSING'}", rec.label, f"trendLinks[{i}].trendId",
                                 f"引用趋势 {link['trendId']} 不在本次数据集内")
            for i, ref in enumerate(data.get("forecastRefs") or []):
                if isinstance(ref, str) and ref not in known_forecast:
                    level_fn(f"REF{code_suffix or '_MISSING'}", rec.label, f"forecastRefs[{i}]",
                             f"引用预测 {ref} 不在本次数据集内")
        elif rec.kind == "trend":
            for i, ref in enumerate(data.get("evidenceRefs") or []):
                if isinstance(ref, str) and ref not in known_event:
                    level_fn(f"REF{code_suffix or '_MISSING'}", rec.label, f"evidenceRefs[{i}]",
                             f"引用事件 {ref} 不在本次数据集内")
        else:
            for i, ref in enumerate(data.get("linkedTrendIds") or []):
                if isinstance(ref, str) and ref not in known_trend:
                    level_fn(f"REF{code_suffix or '_MISSING'}", rec.label, f"linkedTrendIds[{i}]",
                             f"引用趋势 {ref} 不在本次数据集内")
            for i, ref in enumerate(data.get("evidenceRefs") or []):
                if isinstance(ref, str) and ref not in known_event:
                    level_fn(f"REF{code_suffix or '_MISSING'}", rec.label, f"evidenceRefs[{i}]",
                             f"引用事件 {ref} 不在本次数据集内")
    return ctx


def validate_paths(paths: Iterable[str], strict_refs: str = "auto",
                   baseline_dir: str | None = None,
                   history: str = "auto") -> tuple[Ctx, list[Path]]:
    records, load_findings, files = load_paths(paths)
    strict = {"error": True, "warn": False, "skip": False}.get(strict_refs)
    if strict is None:  # auto
        strict = len(files) > 1
    ctx = validate_records(records, strict_refs=strict)
    check_history(records, ctx, baseline_dir=baseline_dir, mode=history)
    ctx.findings = load_findings + ctx.findings
    return ctx, files


# --------------------------------------------------------------------------- #
# 评分（代码求和）
# --------------------------------------------------------------------------- #
def score_records(records: list[Record], write: bool = False) -> tuple[Ctx, list[dict], int]:
    ctx = Ctx()
    results: list[dict] = []
    changed_files: set[Path] = set()
    for rec in records:
        if rec.kind != "event":
            continue
        scores = rec.data.get("scores")
        info = {"id": rec.rid, "path": rec.label, "total": None, "expected": None, "status": "ok"}
        if not isinstance(scores, dict):
            ctx.err("SCORE_MISSING", rec.label, "scores", "缺少 scores 对象")
            info["status"] = "error"
            results.append(info)
            continue
        values = {}
        bad = False
        for dim in DIMS:
            raw = scores.get(dim)
            if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                ctx.err("SCORE_DIM_MISSING", rec.label, f"scores.{dim}", f"{dim} 缺失或不是数字")
                bad = True
                continue
            if raw < 0 or raw > DIM_CAPS[dim]:
                ctx.err("SCORE_DIM_RANGE", rec.label, f"scores.{dim}",
                        f"{dim}={raw:g} 超出 0-{DIM_CAPS[dim]}")
                bad = True
            values[dim] = float(raw)
        if bad or len(values) != len(DIMS):
            info["status"] = "error"
            results.append(info)
            continue
        expected = sum_dims(values)
        total = scores.get("total")
        info.update({"total": total, "expected": expected, "dims": {d: values[d] for d in DIMS}})
        if isinstance(total, bool) or not isinstance(total, (int, float)) or abs(float(total) - expected) > 1e-6:
            info["status"] = "mismatch"
            if not write:
                ctx.err("SCORE_TOTAL_MISMATCH", rec.label, "scores.total",
                        f"total={total!r} 应为 {expected:g}（{' + '.join(f'{values[d]:g}' for d in DIMS)}）")
            if write:
                scores["total"] = expected
                changed_files.add(rec.src)
                info["written"] = expected
        if not bool(scores.get("rationale")):
            ctx.err("SCORE_NO_RATIONALE", rec.label, "scores.rationale", "缺少逐维依据")
        sv = scores.get("scorerVersion")
        if not isinstance(sv, str) or not re.search(SCORER_VERSION_RE, sv):
            ctx.err("SCORE_NO_VERSION", rec.label, "scores.scorerVersion",
                    "scorerVersion 缺失或格式错（应形如 score/v1.0）")
        results.append(info)

    if write and changed_files:
        for path in sorted(changed_files):
            backup = path.with_suffix(path.suffix + ".bak")
            try:
                backup.write_bytes(path.read_bytes())
                doc = read_json(path)
                apply_totals_to_doc(doc)
                with path.open("w", encoding="utf-8") as fh:
                    json.dump(doc, fh, ensure_ascii=False, indent=2)
                    fh.write("\n")
            except OSError as exc:
                ctx.err("E_WRITE", rel_path(path), "", f"写回失败：{exc}")
    return ctx, results, len(changed_files)


def apply_totals_to_doc(doc: Any) -> None:
    """把 doc 里所有 event 的 scores.total 重算并写回（原地修改）。"""
    def fix(obj: Any) -> None:
        if not isinstance(obj, dict):
            return
        scores = obj.get("scores")
        if isinstance(scores, dict) and all(isinstance(scores.get(d), (int, float)) and not isinstance(scores.get(d), bool) for d in DIMS):
            scores["total"] = sum_dims({d: float(scores[d]) for d in DIMS})

    if isinstance(doc, dict) and any(k in doc for k in BUNDLE_KEYS.values()):
        for key in BUNDLE_KEYS.values():
            items = doc.get(key)
            if isinstance(items, list):
                for item in items:
                    fix(item)
    elif isinstance(doc, list):
        for item in doc:
            fix(item)
    else:
        fix(doc)


# --------------------------------------------------------------------------- #
# 去重
# --------------------------------------------------------------------------- #
HI_SIM = 0.90
NEAR_SIM = 0.70
SAME_WINDOW_DAYS = 10


def _event_urls(ev: dict) -> set[str]:
    out = set()
    for src in ev.get("sources") or []:
        if isinstance(src, dict) and isinstance(src.get("url"), str):
            out.add(src["url"].strip().rstrip("/"))
    return out


def _event_time(ev: dict) -> dt.datetime | None:
    for key in ("occurredAt", "firstReportedAt", "collectedAt"):
        parsed = parse_ts(ev.get(key))
        if parsed:
            return as_aware(parsed)
    return None


class _UF:
    def __init__(self, n: int):
        self.p = list(range(n))

    def find(self, x: int) -> int:
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra


def _source_map(data: dict) -> dict[str, str]:
    """url -> type，只收合法 http(s) 字符串。"""
    out: dict[str, str] = {}
    for s in data.get("sources") or []:
        if isinstance(s, dict) and isinstance(s.get("url"), str) and s["url"].startswith("http"):
            out[s["url"]] = str(s.get("type") or "")
    return out


def dedupe_records(records: list[Record], shared_out: list | None = None) -> list[dict]:
    """返回重复簇；shared_out 不为 None 时，把"只共享汇总页"的弱信号追加进去。同时把"只共享汇总页"的弱信号挂在簇外的 findings 里。

    判定优先级（强 → 弱）：
      1. dedupeKey 相同
      2. 一手来源 URL 相同（两边都标 primary）
      3. 标题高度相似（同分类 + 10 天窗口 + ≥HI_SIM）
      5. 标题相似 NEAR_SIM..HI_SIM（疑似，需人工确认）
    只共享 1 个 corroboration URL 不算重复：采集端常引用同一"每日汇总"页，
    这会把不相关的事件串成一片（真实样本里出现过 5 条模型发布被串成一簇）。
    """
    events = [r for r in records if r.kind == "event"]
    n = len(events)
    uf = _UF(n)
    reasons: dict[tuple[int, int], str] = {}
    shared_weak: list[dict] = []

    for i in range(n):
        for j in range(i + 1, n):
            a, b = events[i], events[j]
            da, db = a.data, b.data
            why = None
            ka, kb = da.get("dedupeKey"), db.get("dedupeKey")
            ma, mb = _source_map(da), _source_map(db)
            shared = set(ma) & set(mb)
            primary_shared = sorted(u for u in shared if ma[u] == "primary" and mb[u] == "primary")
            ta, tb = da.get("title") or "", db.get("title") or ""
            sim = similarity(ta, tb)
            same_cat = da.get("category") == db.get("category")
            t1, t2 = _event_time(da), _event_time(db)
            window_ok = True
            if t1 and t2:
                window_ok = abs((as_aware(t1) - as_aware(t2)).days) <= SAME_WINDOW_DAYS

            if isinstance(ka, str) and ka and ka == kb:
                why = f"dedupeKey 相同（{ka}）"
            elif primary_shared and sim >= NEAR_SIM:
                why = f"一手来源相同且标题相近 {sim:.2f}：{primary_shared[0]}"
            elif same_cat and window_ok and sim >= HI_SIM:
                why = f"标题高度相似 {sim:.2f} 且同分类、时间相近"
            elif same_cat and window_ok and sim >= NEAR_SIM:
                why = f"标题相似 {sim:.2f}（疑似同一事件，需人工确认）"

            if why:
                uf.union(i, j)
                reasons[(i, j)] = why
            elif primary_shared:
                shared_weak.append({"a": a.rid, "b": b.rid, "url": primary_shared[0], "n": len(shared),
                                    "kind": "same_primary",
                                    "why": "同一 URL 被两条不同事件当成一手来源，采集端需拆分或改标 corroboration"})
            elif shared:
                shared_weak.append({"a": a.rid, "b": b.rid, "url": sorted(shared)[0],
                                    "n": len(shared), "kind": "shared_source",
                                    "why": "只共享交叉印证来源（疑似汇总页），未计为重复"})

    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(uf.find(i), []).append(i)
    clusters = [g for g in groups.values() if len(g) > 1]

    out: list[dict] = []
    for g in sorted(clusters, key=lambda g: (-len(g), g[0])):
        members = [events[i] for i in g]

        def _rank(r: Record):
            total = float(((r.data.get("scores") or {}).get("total") or 0))
            first = as_aware(parse_ts(r.data.get("firstReportedAt")) or now_ts())
            return (-total, first)

        best = sorted(members, key=_rank)[0]
        earliest = min(members, key=lambda r: as_aware(parse_ts(r.data.get("firstReportedAt")) or now_ts()))
        pairs = []
        for i in g:
            for j in g:
                if i < j and (i, j) in reasons:
                    pairs.append({"a": events[i].rid, "b": events[j].rid, "why": reasons[(i, j)]})
        tie = all(float(((r.data.get("scores") or {}).get("total") or 0)) ==
                  float(((best.data.get("scores") or {}).get("total") or 0)) for r in members)
        suggestion = (f"保留 {best.rid}（同分，取首报最早）" if tie
                      else f"保留 {best.rid}（总分最高），其余合并 revisions 后删除")
        out.append({
            "members": [r.rid for r in members],
            "labels": [r.label for r in members],
            "pairs": pairs,
            "recommended_keep": best.rid,
            "earliest_reported": earliest.rid,
            "suggestion": suggestion,
        })

    if shared_out is not None:
        shared_out.extend(sorted(shared_weak, key=lambda w: (w["a"], w["b"])))
    return out


# --------------------------------------------------------------------------- #
# 报告
# --------------------------------------------------------------------------- #
def _bar(value: int, total: int, width: int = 16) -> str:
    if total <= 0:
        return "·" * width
    filled = round(width * value / total)
    return "█" * filled + "·" * (width - filled)


def build_report(records: list[Record], ctx: Ctx | None = None) -> str:
    events = [r for r in records if r.kind == "event"]
    trends = [r for r in records if r.kind == "trend"]
    forecasts = [r for r in records if r.kind == "forecast"]

    def ev(r: Record) -> dict:
        return r.data

    lines: list[str] = []
    lines.append("# ERA 数据体检报告")
    lines.append("")
    lines.append(f"- 生成时间：{fmt_ts(now_ts())}")
    lines.append(f"- 数据根：`{ERA_HOME}`")
    lines.append(f"- schema：`{SCHEMA_VERSION}`")
    lines.append(f"- 记录数：event {len(events)} / trend {len(trends)} / forecast {len(forecasts)}")
    if ctx is not None:
        lines.append(f"- 校验：{len(ctx.errors)} errors / {len(ctx.warnings)} warnings")
    lines.append("")

    if events:
        pub = [e for e in events if e.data.get("status") == "published"]
        pend = [e for e in events if e.data.get("status") == "pending"]
        tot = [float((e.data.get("scores") or {}).get("total") or 0) for e in events]
        tot_sorted = sorted(tot)
        median = tot_sorted[len(tot_sorted) // 2] if tot_sorted else 0
        lines.append("## 事件")
        lines.append("")
        lines.append(f"- 发布：{len(pub)} · 待审：{len(pend)}")
        lines.append(f"- 总分：最高 {max(tot):g} / 中位 {median:g} / 最低 {min(tot):g}；<50 分 {sum(1 for t in tot if t < 50)} 条（按发布门槛不进首页）")
        lines.append("")
        lines.append("| 分类 | 条数 |")
        lines.append("| --- | ---: |")
        for cat in CATEGORIES:
            lines.append(f"| {cat} | {sum(1 for e in events if e.data.get('category') == cat)} |")
        lines.append("")
        lines.append("| 可信度 | 条数 |")
        lines.append("| --- | ---: |")
        for lvl in CRED_LEVELS:
            lines.append(f"| {lvl} | {sum(1 for e in events if (e.data.get('credibility') or {}).get('level') == lvl)} |")
        lines.append("")

    if trends:
        lines.append("## 趋势")
        lines.append("")
        lines.append("| 阶段 | 条数 |")
        lines.append("| --- | ---: |")
        for st in STAGES:
            lines.append(f"| {st} | {sum(1 for t in trends if t.data.get('stage') == st)} |")
        overdue = []
        for t in trends:
            rb = parse_ts(t.data.get("reviewBy"))
            if rb and as_aware(rb).date() < now_ts().date():
                overdue.append((t.rid, t.data.get("reviewBy")))
        lines.append("")
        if overdue:
            lines.append(f"- 到期未复核 {len(overdue)} 条：" + "、".join(f"`{i}`（{d}）" for i, d in overdue))
        else:
            lines.append("- 无到期未复核趋势")
        lines.append("")

    if forecasts:
        lines.append("## 判断追踪")
        lines.append("")
        open_fc = [f for f in forecasts if f.data.get("status") == "open"]
        resolved = [f for f in forecasts if f.data.get("status") == "resolved"]
        hit = [f for f in resolved if (f.data.get("resolution") or {}).get("outcome") == "hit"]
        lines.append(f"- 待判定：{len(open_fc)} · 已判定：{len(resolved)}（hit {len(hit)}）")
        if resolved:
            lines.append(f"- 命中率：{len(hit) / len(resolved):.0%}（{len(hit)}/{len(resolved)}）")
        overdue_fc = [f.rid for f in open_fc if (parse_ts(f.data.get("resolveBy")) and as_aware(parse_ts(f.data.get("resolveBy"))).date() < now_ts().date())]
        if overdue_fc:
            lines.append(f"- 过期未判定 {len(overdue_fc)} 条：" + "、".join(f"`{i}`" for i in overdue_fc))
        lines.append("")

    bad = [e for e in events if e.data.get("status") == "published"
           and not any(isinstance(s, dict) and s.get("type") == "primary" for s in (e.data.get("sources") or []))]
    lines.append("## 发布门槛")
    lines.append("")
    lines.append(f"- published 缺 primary 来源：{len(bad)} 条" + ("（必须修）" if bad else ""))
    if bad:
        for e in bad:
            lines.append(f"  - `{e.rid}` {short(e.data.get('title'), 40)}")
    lines.append("")

    dups = dedupe_records(records)
    lines.append("## 去重")
    lines.append("")
    lines.append(f"- 疑似重复簇：{len(dups)} 组")
    for i, c in enumerate(dups, 1):
        lines.append(f"  - 簇 {i}：{'、'.join(f'`{m}`' for m in c['members'])} → 建议保留 `{c['recommended_keep']}`")
    lines.append("")

    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 输出辅助
# --------------------------------------------------------------------------- #
def print_findings(ctx: Ctx, limit: int = 200) -> None:
    ordered = sorted(ctx.findings, key=lambda f: (0 if f.level == "error" else 1, f.path, f.field))
    for f in ordered[:limit]:
        print(f.fmt())
    if len(ordered) > limit:
        print(f"... 还有 {len(ordered) - limit} 条未显示（--json 取全量）")


def stats_dict(ctx: Ctx, files: list[Path] | None = None) -> dict:
    stats = {
        "schema": SCHEMA_VERSION,
        "maxTs": fmt_ts(max_dt()),
        "files": len(files or []),
        "records": ctx.stats.get("records", 0),
        "events": ctx.stats.get("event", 0),
        "trends": ctx.stats.get("trend", 0),
        "forecasts": ctx.stats.get("forecast", 0),
        "errors": len(ctx.errors),
        "warnings": len(ctx.warnings),
        "ok": ctx.ok,
    }
    win = active_window()
    if win is not None:
        stats["window"] = win.as_report()
    return stats


def dump_json(payload: Any) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))
