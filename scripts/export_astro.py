#!/usr/bin/env python3
"""[二组前端] ERA 数据导出：content/ → Astro 用的 data/events.json + data/trends.json

只做"导出适配"，不做全量契约校验——逐字段强校验由 schema 校验器（validate.py）负责。
这里负责导出链路不能出错的部分：
  1. 读两种输入：纯 JSON（默认）/ Astro content collections 的 md frontmatter（--format collections）
  2. 六维分自洽（total == 六维之和，逐项不超上限），拒绝手填总分
  3. 发布门槛落成数据字段：unverified 不上头条，总分 < 50 不进首页
  4. 排序 / 去重 / 悬空 trendId / published 必须有 primary 来源
  5. 稳定输出（同输入同输出，不含随机），便于 CI diff 与回滚

用法：
  python3 scripts/export_astro.py                                  # content/ -> data/
  python3 scripts/export_astro.py --content content --out data
  python3 scripts/export_astro.py --format collections \
      --content src/content --out src/data
  python3 scripts/export_astro.py --check --json                   # 只校验不写盘
  python3 scripts/export_astro.py --selftest                       # 内置自检

退出码：0 通过；1 有硬错误（未写盘）；2 参数或 IO 错误。

依赖：Python 3.9+ 标准库。不依赖 PyYAML（自带 frontmatter 子集解析器）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _core as C  # noqa: E402 —— 窗口/时间上限与校验器共用一份口径（docs/window-rolling.md）

SCHEMA_VERSION = "1"

# ---- 契约常量（与 schema/event.json、schema/trend.json 对齐）----
EVENT_CATEGORIES = ("全球宏观", "中国经济与产业", "科技与社会", "未来趋势")
CREDIBILITY_LEVELS = ("verified", "partial", "unverified")
EVENT_STATUS = ("published", "pending")
TREND_STAGES = ("emerging", "strengthening", "stable", "weakening", "falsified")
LINK_DIRECTIONS = ("support", "weaken", "challenge")
SOURCE_TYPES = ("primary", "corroboration")

# 六维上限：structural 25 / scope 20 / signal 20 / delta 15 / timeliness 10 / crossDomain 10
DIMENSIONS = (
    ("structural", 25),
    ("scope", 20),
    ("signal", 20),
    ("delta", 15),
    ("timeliness", 10),
    ("crossDomain", 10),
)
TOTAL_MAX = sum(cap for _, cap in DIMENSIONS)  # 100

# 发布门槛（项目.md 内容口径：unverified 不上头条、总分 < 50 不进首页）
HEADLINE_MIN_TOTAL = 50
HOMEPAGE_MIN_TOTAL = 50

SUMMARY_MAX_CHARS = 120
SUMMARY_MIN_CHARS = 10
TITLE_MIN_CHARS, TITLE_MAX_CHARS = 4, 120

# 与 schema/*.schema.json 对齐：id 前缀 ev_/tr_/fc_，dedupeKey 全小写，scorerVersion 形如 score/v1.0
EVENT_ID_RE = r"^ev_[a-z0-9][a-z0-9_-]{2,63}$"
TREND_ID_RE = r"^tr_[a-z0-9][a-z0-9_-]{2,63}$"
FORECAST_ID_RE = r"^fc_[a-z0-9][a-z0-9_-]{2,63}$"
DEDUPE_KEY_RE = r"^[a-z0-9][a-z0-9._:-]{7,127}$"
SCORER_VERSION_RE = r"^score/v[0-9]+\.[0-9]+$"
FORECAST_STATUS = ("open", "resolved", "void")
FORECAST_OUTCOMES = ("hit", "partial", "miss", "unresolved")
RETRO_REQUIRED_NOTE = True  # 回溯事件必须写 note 与 asOf


class ExportError(Exception):
    """硬错误：数据不合格，不写盘。"""


# --------------------------------------------------------------------------
# 小型 YAML frontmatter 解析器（PyYAML 的可用子集）
# 支持：key: value / 嵌套 map / "- " 列表 / [a, b] / 引号 / 数字 / bool / | 与 > 块
# --------------------------------------------------------------------------
_YAML_ESCAPES = {
    "n": "\n",
    "t": "\t",
    "r": "\r",
    '"': '"',
    "\\": "\\",
    "/": "/",
    "0": "\0",
}


def _unquote_double(s: str) -> str:
    out, i = [], 1
    while i < len(s) - 1:
        ch = s[i]
        if ch == "\\" and i + 1 < len(s) - 1:
            nxt = s[i + 1]
            out.append(_YAML_ESCAPES.get(nxt, nxt))
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _split_inline(s: str) -> list[str]:
    """按逗号切分内联列表，跳过引号内的逗号。"""
    parts, buf, quote = [], [], None
    for ch in s:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            buf.append(ch)
            continue
        if ch == ",":
            parts.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    if buf:
        parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip() != ""]


def parse_scalar(raw: str):
    s = raw.strip()
    if s == "":
        return None
    if s in ("null", "Null", "NULL", "~"):
        return None
    if s in ("true", "True", "TRUE"):
        return True
    if s in ("false", "False", "FALSE"):
        return False
    if s.startswith("[") and s.endswith("]"):
        inner = s[1:-1].strip()
        return [] if inner == "" else [parse_scalar(x) for x in _split_inline(inner)]
    if s.startswith("{") and s.endswith("}"):
        inner = s[1:-1].strip()
        obj = {}
        if inner:
            for item in _split_inline(inner):
                if ":" in item:
                    k, v = item.split(":", 1)
                    obj[str(parse_scalar(k))] = parse_scalar(v)
        return obj
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        return _unquote_double(s)
    if len(s) >= 2 and s[0] == "'" and s[-1] == "'":
        return s[1:-1].replace("''", "'")
    if re.fullmatch(r"[-+]?\d+", s):
        return int(s)
    if re.fullmatch(r"[-+]?(\d+\.\d*|\.\d+)([eE][-+]?\d+)?", s):
        return float(s)
    return s


class _Line:
    __slots__ = ("indent", "text", "lineno")

    def __init__(self, indent: int, text: str, lineno: int):
        self.indent = indent
        self.text = text
        self.lineno = lineno


def _tokenize_yaml(frontmatter: str) -> list[_Line]:
    lines: list[_Line] = []
    for lineno, raw in enumerate(frontmatter.splitlines(), start=1):
        if raw.strip() == "" or raw.lstrip().startswith("#"):
            continue
        stripped = raw.lstrip(" ")
        if stripped.startswith("\t"):
            raise ExportError(f"frontmatter 第 {lineno} 行用了 Tab 缩进，请改成空格")
        lines.append(_Line(len(raw) - len(stripped), stripped.rstrip(), lineno))
    return lines


def _parse_block(lines: list[_Line], i: int, indent: int, where: str):
    """解析 indent 处的一个 block（dict 或 list），返回 (value, next_index)。"""
    if i >= len(lines):
        return None, i
    if lines[i].text.startswith("-"):
        return _parse_list(lines, i, indent, where)
    return _parse_map(lines, i, indent, where)


def _parse_block_scalar(lines: list[_Line], i: int, indent: int, style: str) -> tuple[str, int]:
    buf: list[str] = []
    while i < len(lines) and lines[i].indent > indent:
        buf.append(lines[i].text)
        i += 1
    if style.startswith(">"):
        return " ".join(buf), i
    return "\n".join(buf), i


def _parse_list(lines: list[_Line], i: int, indent: int, where: str):
    out: list = []
    while i < len(lines) and lines[i].indent == indent and lines[i].text.startswith("-"):
        item_text = lines[i].text[1:].lstrip()
        lineno = lines[i].lineno
        i += 1
        if item_text == "":
            if i < len(lines) and lines[i].indent > indent:
                value, i = _parse_block(lines, i, lines[i].indent, where)
            else:
                value = None
            out.append(value)
            continue
        # "- key: value" → 列表元素是 map，后续同缩进+2 的行都属于这个 map
        m = re.match(r"([^:\s][^:]*):(.*)$", item_text)
        if m and not item_text.startswith(("'", '"', "[")):
            key = str(parse_scalar(m.group(1)))
            rest = m.group(2).strip()
            node: dict = {}
            if rest.startswith(("|", ">")):
                value, i = _parse_block_scalar(lines, i, indent, rest)
                node[key] = value
            elif rest == "":
                if i < len(lines) and lines[i].indent > indent + 1:
                    value, i = _parse_block(lines, i, lines[i].indent, f"{where}.{key}")
                else:
                    value = None
                node[key] = value
            else:
                node[key] = parse_scalar(rest)
            # 其余同层级键（缩进 indent+2）继续并入这个 map
            if i < len(lines) and lines[i].indent > indent and not lines[i].text.startswith("-"):
                child_indent = lines[i].indent
                extra, i = _parse_map(lines, i, child_indent, where, stop_indent=child_indent)
                if isinstance(extra, dict):
                    node.update(extra)
            out.append(node)
            continue
        if item_text.startswith(("|", ">")):
            value, i = _parse_block_scalar(lines, i, indent, item_text)
            out.append(value)
            continue
        out.append(parse_scalar(item_text))
    if i < len(lines) and lines[i].indent == indent and not lines[i].text.startswith("-"):
        raise ExportError(
            f"{where} 第 {lines[i].lineno} 行：列表里混进了非 '-' 开头的行，检查缩进"
        )
    return out, i


def _parse_map(
    lines: list[_Line], i: int, indent: int, where: str, stop_indent: int | None = None
):
    out: dict = {}
    parent = stop_indent if stop_indent is not None else indent
    while i < len(lines):
        line = lines[i]
        if line.indent < parent:
            break
        if line.indent > parent:
            raise ExportError(
                f"{where} 第 {line.lineno} 行缩进异常（{line.indent} > {parent}）：{line.text[:40]}"
            )
        m = re.match(r"([^:]+?):(.*)$", line.text)
        if not m:
            raise ExportError(f"{where} 第 {line.lineno} 行不是 key: value 形式：{line.text[:40]}")
        key = str(parse_scalar(m.group(1)))
        rest = m.group(2).strip()
        i += 1
        if rest.startswith(("|", ">")):
            value, i = _parse_block_scalar(lines, i, indent, rest)
        elif rest == "":
            if i < len(lines) and lines[i].indent > indent:
                value, i = _parse_block(lines, i, lines[i].indent, f"{where}.{key}")
            else:
                value = None
        else:
            value = parse_scalar(rest)
        out[key] = value
        if i < len(lines) and lines[i].indent < parent:
            break
    return out, i


def parse_frontmatter(frontmatter: str, where: str = "<frontmatter>") -> dict:
    tokens = _tokenize_yaml(frontmatter)
    if not tokens:
        return {}
    value, idx = _parse_block(tokens, 0, tokens[0].indent, where)
    if idx != len(tokens):
        raise ExportError(f"{where} 第 {tokens[idx].lineno} 行没解析到，检查缩进")
    if not isinstance(value, dict):
        raise ExportError(f"{where} 顶层不是 map")
    return value


def split_markdown(text: str):
    """返回 (frontmatter_text 或 None, body)。"""
    if not text.startswith("---"):
        return None, text
    lines = text.splitlines()
    if not lines or lines[0].strip() not in ("---", "---\r"):
        return None, text
    for idx in range(1, len(lines)):
        if lines[idx].strip() in ("---", "..."):
            return "\n".join(lines[1:idx]), "\n".join(lines[idx + 1 :])
    raise ExportError("frontmatter 没有闭合的 ---")


# --------------------------------------------------------------------------
# 读取输入
# --------------------------------------------------------------------------
def _iter_files(root: Path, suffixes: tuple[str, ...]) -> list[Path]:
    if not root.exists():
        raise ExportError(f"输入目录不存在：{root}")
    if root.is_file():
        return [root] if root.suffix.lower() in suffixes else []
    files: list[Path] = []
    for p in root.rglob("*"):
        if not p.is_file() or p.suffix.lower() not in suffixes or p.name.startswith("."):
            continue
        parts = p.relative_to(root).parts
        # Astro 约定：_ 前缀目录（如 _intake）是暂存/私有，不参与导出
        if any(part.startswith("_") or part.startswith(".") for part in parts[:-1]):
            continue
        files.append(p)
    return sorted(files)


def _docs_from_json_value(value, path: Path) -> list[tuple[dict, Path]]:
    """一个 json 文件可以是一个对象、对象数组、或 {events:[], trends:[]}。"""
    out: list[tuple[dict, Path]] = []
    if isinstance(value, list):
        for item in value:
            if not isinstance(item, dict):
                raise ExportError(f"{path}：数组元素不是对象")
            out.append((item, path))
        return out
    if isinstance(value, dict):
        containers = [k for k in ("events", "trends", "items") if isinstance(value.get(k), list)]
        if containers and not _looks_like_doc(value):
            for key in containers:
                for item in value[key]:
                    if not isinstance(item, dict):
                        raise ExportError(f"{path}：{key} 数组元素不是对象")
                    out.append((item, path))
            return out
        return [(value, path)]
    raise ExportError(f"{path}：顶层必须是对象或数组")


def _looks_like_doc(d: dict) -> bool:
    return "id" in d and ("dedupeKey" in d or "thesis" in d)


def _kind_of(doc: dict, path: Path) -> str | None:
    lowered = str(path).lower()
    doc_id = as_text(doc.get("id")).strip()
    if "thesis" in doc or doc_id.startswith("tr_"):
        return "trend"
    if "resolveBy" in doc or "probability" in doc or doc_id.startswith("fc_"):
        return "forecast"
    if "dedupeKey" in doc or "scores" in doc or "occurredAt" in doc or doc_id.startswith("ev_"):
        return "event"
    if "/trends/" in lowered or "trend" in path.stem.lower():
        return "trend"
    if "/forecasts/" in lowered or "forecast" in path.stem.lower():
        return "forecast"
    if "/events/" in lowered or "event" in path.stem.lower():
        return "event"
    return None


def load_content(root: Path, fmt: str) -> tuple[list[dict], list[dict], list[dict], list[str]]:
    events: list[dict] = []
    trends: list[dict] = []
    forecasts: list[dict] = []
    notes: list[str] = []
    suffixes = (".json",) if fmt == "json" else (".md", ".mdx")
    files = _iter_files(root, suffixes)
    if not files:
        raise ExportError(f"{root} 下没找到 {fmt} 输入文件")
    for path in files:
        text = path.read_text(encoding="utf-8")
        try:
            if fmt == "json":
                if text.strip() == "":
                    notes.append(f"{_rel(path)} 是空文件，跳过")
                    continue
                docs = _docs_from_json_value(json.loads(text), path)
            else:
                fm, _body = split_markdown(text)
                if fm is None:
                    notes.append(f"{_rel(path)} 没有 frontmatter，跳过")
                    continue
                docs = [(parse_frontmatter(fm, _rel(path)), path)]
        except json.JSONDecodeError as exc:
            raise ExportError(f"{_rel(path)} JSON 解析失败：{exc}") from exc
        for doc, origin in docs:
            kind = _kind_of(doc, origin)
            if kind == "event":
                events.append(doc)
            elif kind == "trend":
                trends.append(doc)
            elif kind == "forecast":
                forecasts.append(doc)
            else:
                notes.append(
                    f"{_rel(origin)} 认不出是 event/trend/forecast"
                    f"（缺 id、thesis、scores、resolveBy），跳过"
                )
    return events, trends, forecasts, notes


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        return str(path)


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------
def as_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return " ".join(as_text(v) for v in value)
    return str(value)


def as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, (str, int, float)):
        return [value] if str(value).strip() != "" else []
    return []


def parse_time(value, ctx: str, errors: list[str], allow_none: bool = False):
    if value in (None, ""):
        if not allow_none:
            errors.append(f"{ctx}：缺少时间")
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    text = as_text(value).strip()
    normalized = text.replace("Z", "+00:00") if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        errors.append(f"{ctx}：时间格式不合法（要 ISO 8601）：{text!r}")
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def is_http(url: str) -> bool:
    return bool(re.match(r"^https?://\S+$", url.strip(), re.IGNORECASE))


# --------------------------------------------------------------------------
# 校验 + 归一化
# --------------------------------------------------------------------------
def validate_and_normalize_events(
    docs: list[dict], trend_ids: set[str], today: date, warnings: list[str]
):
    errors: list[str] = []
    events: list[dict] = []
    seen_ids: dict[str, str] = {}
    seen_dedupe: dict[str, str] = {}

    for idx, doc in enumerate(docs):
        origin = doc.get("__source__", f"#{idx}")
        eid = as_text(doc.get("id")).strip()
        ctx = f"event[{eid or origin}]"
        if not eid:
            errors.append(f"{ctx}：缺 id")
            continue
        if not re.fullmatch(EVENT_ID_RE, eid):
            errors.append(f"{ctx}：id 必须匹配 {EVENT_ID_RE}：{eid!r}")
        if eid in seen_ids:
            errors.append(f"{ctx}：id 重复（另一处在 {seen_ids[eid]}）")
            continue
        seen_ids[eid] = origin

        title = as_text(doc.get("title")).strip()
        summary = as_text(doc.get("summary")).strip()
        if not title:
            errors.append(f"{ctx}：缺 title")
        elif not (TITLE_MIN_CHARS <= C.char_len(title) <= TITLE_MAX_CHARS):
            # 与校验器同一口径：按「字」计，不计空白（_core.char_len）。
            # 用裸 len() 会把带空格的英文标题判长，出现「collect 通过、deploy 才炸」的错位。
            errors.append(f"{ctx}：title {C.char_len(title)} 字，需在 {TITLE_MIN_CHARS}–{TITLE_MAX_CHARS}")
        if not summary:
            errors.append(f"{ctx}：缺 summary")
        elif not (SUMMARY_MIN_CHARS <= C.char_len(summary) <= SUMMARY_MAX_CHARS):
            errors.append(
                f"{ctx}：summary {C.char_len(summary)} 字，需在 {SUMMARY_MIN_CHARS}–{SUMMARY_MAX_CHARS}"
            )

        dedupe_key = as_text(doc.get("dedupeKey")).strip()
        if not dedupe_key:
            warnings.append(f"{ctx}：缺 dedupeKey，导出时用 id 顶替（去重会变弱，请补）")
            dedupe_key = eid
        elif not re.fullmatch(DEDUPE_KEY_RE, dedupe_key):
            errors.append(
                f"{ctx}：dedupeKey 必须匹配 ^[a-z0-9][a-z0-9._:-]{{7,127}}$（小写、≥8 位）：{dedupe_key!r}"
            )
        if dedupe_key in seen_dedupe:
            errors.append(
                f"{ctx}：dedupeKey 与 {seen_dedupe[dedupe_key]} 重复 -> {dedupe_key!r}，应合并为同一事件"
            )
        else:
            seen_dedupe[dedupe_key] = eid

        category = as_text(doc.get("category")).strip()
        if category not in EVENT_CATEGORIES:
            errors.append(f"{ctx}：category={category!r} 不在 {list(EVENT_CATEGORIES)}")
        region = as_text(doc.get("region")).strip()
        if not region:
            errors.append(f"{ctx}：缺 region")

        occurred = parse_time(doc.get("occurredAt"), f"{ctx}.occurredAt", errors)
        first_reported = parse_time(doc.get("firstReportedAt"), f"{ctx}.firstReportedAt", errors)
        collected = parse_time(doc.get("collectedAt"), f"{ctx}.collectedAt", errors)
        for name, ts in (
            ("occurredAt", occurred),
            ("firstReportedAt", first_reported),
            ("collectedAt", collected),
        ):
            if ts and ts.date() > today:
                errors.append(f"{ctx}：{name}={ts.date()} 晚于今天 {today}，时间穿越")

        facts = as_list(doc.get("facts"))
        if not facts:
            errors.append(f"{ctx}：facts 为空（schema minItems=1）")
        for n, fact in enumerate(facts, 1):
            if not as_text(fact).strip():
                errors.append(f"{ctx}.facts[{n}] 是空字符串")

        analysis = as_text(doc.get("analysis")).strip()
        if not analysis:
            warnings.append(f"{ctx}：analysis 为空，事实/判断分离会不完整")

        # 来源
        sources = as_list(doc.get("sources"))
        clean_sources = []
        for n, src in enumerate(sources, 1):
            sctx = f"{ctx}.sources[{n}]"
            if not isinstance(src, dict):
                errors.append(f"{sctx}：不是对象")
                continue
            url = as_text(src.get("url")).strip()
            if not is_http(url):
                errors.append(f"{sctx}：url 不是 http(s) 链接：{url!r}")
            stype = as_text(src.get("type")).strip()
            if stype not in SOURCE_TYPES:
                errors.append(f"{sctx}：type={stype!r} 必须是 {list(SOURCE_TYPES)}")
            stitle = as_text(src.get("title")).strip()
            if not stitle:
                errors.append(f"{sctx}：缺 title（schema 必填）")
            elif not (2 <= C.char_len(stitle) <= 200):
                errors.append(f"{sctx}：title {C.char_len(stitle)} 字，需在 2–200")
            publisher = as_text(src.get("publisher")).strip()
            if not publisher:
                errors.append(f"{sctx}：缺 publisher（schema 必填）")
            elif not (2 <= C.char_len(publisher) <= 80):
                errors.append(f"{sctx}：publisher {C.char_len(publisher)} 字，需在 2–80")
            snote = as_text(src.get("note")).strip()
            published_at = parse_time(
                src.get("publishedAt"), f"{sctx}.publishedAt", errors, allow_none=True
            )
            if published_at and published_at.date() > today:
                errors.append(f"{sctx}：publishedAt={published_at.date()} 晚于今天")
            clean_source = {
                "url": url,
                "title": stitle,
                "publisher": publisher,
                "publishedAt": _iso(published_at),
                "type": stype,
            }
            if snote:
                clean_source["note"] = snote[:200]
            clean_sources.append(clean_source)
        if not clean_sources:
            errors.append(f"{ctx}：sources 为空，没有来源的事件不准入")

        # 分数：total 必须等于六维和，逐项不超上限
        scores_raw = doc.get("scores") or {}
        if not isinstance(scores_raw, dict):
            errors.append(f"{ctx}.scores：不是对象")
            scores_raw = {}
        clean_scores: dict = {}
        for name, cap in DIMENSIONS:
            raw = scores_raw.get(name)
            if raw is None:
                errors.append(f"{ctx}.scores：缺 {name}")
                continue
            try:
                val = int(raw)
            except (TypeError, ValueError):
                errors.append(f"{ctx}.scores.{name}={raw!r} 不是整数")
                continue
            if val < 0 or val > cap:
                errors.append(f"{ctx}.scores.{name}={val} 超出 0–{cap}")
            clean_scores[name] = val
        computed_total = sum(clean_scores.get(name, 0) for name, _ in DIMENSIONS)
        declared_total = scores_raw.get("total")
        if declared_total is None:
            errors.append(f"{ctx}.scores.total 缺失（必须由代码求和，不许手填缺口）")
        else:
            try:
                declared_int = int(declared_total)
            except (TypeError, ValueError):
                declared_int = None
                errors.append(f"{ctx}.scores.total={declared_total!r} 不是整数")
            if declared_int is not None and declared_int != computed_total:
                errors.append(
                    f"{ctx}.scores.total={declared_int} 与六维和 {computed_total} 不一致（拒绝手填总分）"
                )
        clean_scores["total"] = computed_total
        if computed_total > TOTAL_MAX:
            errors.append(f"{ctx}.scores.total={computed_total} 超过 {TOTAL_MAX}")
        rationale_raw = scores_raw.get("rationale")
        clean_rationale: dict = {}
        if rationale_raw is None:
            errors.append(f"{ctx}.scores.rationale 缺失（六维各要一句依据，是对象不是字符串）")
        elif isinstance(rationale_raw, str):
            errors.append(
                f"{ctx}.scores.rationale 必须是分维对象 "
                f"{{structural, scope, signal, delta, timeliness, crossDomain}}，不是一段字符串"
            )
        elif isinstance(rationale_raw, dict):
            for name, _cap in DIMENSIONS:
                dim_note = as_text(rationale_raw.get(name)).strip()
                if not dim_note:
                    warnings.append(f"{ctx}.scores.rationale.{name} 为空——每一维都要写依据")
                clean_rationale[name] = dim_note
        else:
            errors.append(f"{ctx}.scores.rationale 类型不对：{type(rationale_raw).__name__}")
        clean_scores["rationale"] = clean_rationale
        scorer_version = as_text(scores_raw.get("scorerVersion")).strip()
        if not scorer_version:
            warnings.append(f"{ctx}.scores.scorerVersion 为空，建议填 score/v1.0")
        elif not re.fullmatch(SCORER_VERSION_RE, scorer_version):
            errors.append(
                f"{ctx}.scores.scorerVersion 必须形如 score/v1.0（^score/v[0-9]+\\.[0-9]+$）：{scorer_version!r}"
            )
        clean_scores["scorerVersion"] = scorer_version

        # 可信度
        cred_raw = doc.get("credibility") or {}
        if not isinstance(cred_raw, dict):
            errors.append(f"{ctx}.credibility：不是对象")
            cred_raw = {}
        level = as_text(cred_raw.get("level")).strip()
        if level not in CREDIBILITY_LEVELS:
            errors.append(f"{ctx}.credibility.level={level!r} 必须是 {list(CREDIBILITY_LEVELS)}")
        basis = as_text(cred_raw.get("basis")).strip()
        if not basis:
            warnings.append(f"{ctx}.credibility.basis 为空，得写清凭什么是 {level or '这个等级'}")

        status_raw = doc.get("status")
        status = as_text(status_raw).strip() if not isinstance(status_raw, dict) else ""
        if status not in EVENT_STATUS:
            errors.append(f"{ctx}.status 必须是 {list(EVENT_STATUS)}，现在是 {status_raw!r}")
        if status == "published" and not any(s["type"] == "primary" for s in clean_sources):
            errors.append(f"{ctx}：status=published 但没有任何 primary 来源")

        # 趋势关联
        trend_links = []
        for n, link in enumerate(as_list(doc.get("trendLinks")), 1):
            lctx = f"{ctx}.trendLinks[{n}]"
            if not isinstance(link, dict):
                errors.append(f"{lctx}：不是对象")
                continue
            tid = as_text(link.get("trendId")).strip()
            direction = as_text(link.get("direction")).strip()
            if not tid:
                errors.append(f"{lctx}：缺 trendId")
            elif trend_ids and tid not in trend_ids:
                errors.append(f"{lctx}：trendId={tid!r} 在趋势库里不存在（悬空关联）")
            if direction not in LINK_DIRECTIONS:
                errors.append(f"{lctx}：direction={direction!r} 必须是 {list(LINK_DIRECTIONS)}")
            reason = as_text(link.get("reason")).strip()
            if not reason:
                errors.append(f"{lctx}：reason 为空（schema minLength=5）")
            elif len(reason) < 5:
                errors.append(f"{lctx}：reason 只有 {len(reason)} 字，至少要 5 字")
            trend_links.append({"trendId": tid, "direction": direction, "reason": reason})

        revisions = as_list(doc.get("revisions"))
        clean_revisions = []
        for n, rev in enumerate(revisions, 1):
            if not isinstance(rev, dict):
                errors.append(f"{ctx}.revisions[{n}]：不是对象")
                continue
            at = parse_time(rev.get("at"), f"{ctx}.revisions[{n}].at", errors)
            note = as_text(rev.get("note", rev.get("change", ""))).strip()
            if not note:
                errors.append(f"{ctx}.revisions[{n}]：缺 note（改了什么、为什么）")
            elif not (4 <= C.char_len(note) <= 400):
                errors.append(f"{ctx}.revisions[{n}]：note {C.char_len(note)} 字，需在 4–400")
            clean_revisions.append(
                {
                    "at": _iso(at),
                    "by": as_text(rev.get("by")).strip()[:40],
                    "note": note,
                }
            )

        retro = doc.get("retro")
        if isinstance(retro, dict):
            retro = {
                "isRetro": bool(retro.get("isRetro", False)),
                "asOf": as_text(retro.get("asOf")).strip(),
                "note": as_text(retro.get("note")).strip()[:400],
            }
        else:
            retro = {"isRetro": bool(retro), "asOf": "", "note": ""}
        if retro["isRetro"] and not retro["note"]:
            errors.append(f"{ctx}：retro.isRetro=true 但 note 为空，回溯必须解释清楚")
        if retro["isRetro"] and not retro["asOf"]:
            warnings.append(f"{ctx}：retro.asOf 为空，回溯事件要写清「以何时视角整理」")

        display = _display_flags(status, level, computed_total)
        grade = as_text(doc.get("evidenceGrade")).strip() or None
        if grade is not None and grade not in CONFIDENCE_RANK:
            warnings.append(
                f"{ctx}：evidenceGrade={grade!r} 不在 {list(CONFIDENCE_RANK)}，按未提供处理"
            )
            grade = None
        ver_raw = doc.get("verification") if isinstance(doc.get("verification"), dict) else {}
        verification = {
            "status": as_text(ver_raw.get("status")).strip() or None,
            "method": as_text(ver_raw.get("method")).strip() or None,
            "checkedAt": _iso(parse_time(ver_raw.get("checkedAt"), f"{ctx}.verification.checkedAt", warnings, allow_none=True)) if ver_raw.get("checkedAt") else None,
            "checks": as_list(ver_raw.get("checks")),
            "note": as_text(ver_raw.get("note")).strip()[:400],
        }
        events.append(
            {
                "id": eid,
                "dedupeKey": dedupe_key,
                "title": title,
                "summary": summary,
                "facts": [as_text(f).strip() for f in facts],
                "analysis": analysis,
                "category": category,
                "region": region,
                "occurredAt": _iso(occurred),
                "firstReportedAt": _iso(first_reported),
                "collectedAt": _iso(collected),
                "sources": clean_sources,
                "scores": clean_scores,
                "credibility": {"level": level, "basis": basis},
                "evidenceGrade": grade,
                "verification": verification,
                "status": status,
                "trendLinks": trend_links,
                "trendIds": [t["trendId"] for t in trend_links if t["trendId"]],
                "revisions": clean_revisions,
                "forecastRefs": [as_text(x).strip() for x in as_list(doc.get("forecastRefs"))],
                "retro": retro,
                "display": display,
                "metrics": doc.get("metrics"),
                "scoreHistory": doc.get("scoreHistory", []),
                "industryTable": doc.get("industryTable"),
            }
        )
    return events, errors


def _(text: str) -> str:
    return text


def _display_flags(status: str, level: str, total: int) -> dict:
    reasons: list[str] = []
    public = status == "published"
    if not public:
        reasons.append("status 不是 published")
    homepage = public and total >= HOMEPAGE_MIN_TOTAL
    if public and total < HOMEPAGE_MIN_TOTAL:
        reasons.append(f"总分 {total} < {HOMEPAGE_MIN_TOTAL}，不进首页")
    headline = homepage and level == "verified"
    if public and level != "verified":
        reasons.append(f"可信度 {level or '缺失'}，不上头条")
    return {
        "public": public,
        "homepage": homepage,
        "headline": headline,
        "reasons": reasons if not headline else [],
    }


def validate_and_normalize_trends(docs: list[dict], today: date, warnings: list[str]):
    errors: list[str] = []
    trends: list[dict] = []
    seen: dict[str, str] = {}
    for idx, doc in enumerate(docs):
        origin = doc.get("__source__", f"#{idx}")
        tid = as_text(doc.get("id")).strip()
        ctx = f"trend[{tid or origin}]"
        if not tid:
            errors.append(f"{ctx}：缺 id")
            continue
        if not re.fullmatch(TREND_ID_RE, tid):
            errors.append(f"{ctx}：id 必须匹配 {TREND_ID_RE}：{tid!r}")
        if tid in seen:
            errors.append(f"{ctx}：id 重复（另一处在 {seen[tid]}）")
            continue
        seen[tid] = origin

        thesis = as_text(doc.get("thesis")).strip()
        if not thesis:
            errors.append(f"{ctx}：缺 thesis（趋势就是一句可证伪的判断）")
        first_seen = parse_time(doc.get("firstSeenAt"), f"{ctx}.firstSeenAt", errors)
        if first_seen and first_seen.date() > today:
            errors.append(f"{ctx}.firstSeenAt={first_seen.date()} 晚于今天 {today}")

        stage = as_text(doc.get("stage")).strip()
        if stage not in TREND_STAGES:
            errors.append(f"{ctx}.stage={stage!r} 必须是 {list(TREND_STAGES)}")

        indicators = [x if isinstance(x, dict) else as_text(x).strip() for x in as_list(doc.get("indicators")) if x]
        if not indicators:
            errors.append(f"{ctx}：indicators 为空（schema minItems=1，趋势必须有可观察指标）")
        elif len(indicators) > 12:
            errors.append(f"{ctx}：indicators {len(indicators)} 条，上限 12")

        evidence = [as_text(x).strip() for x in as_list(doc.get("evidenceRefs")) if as_text(x).strip()]
        if not evidence:
            warnings.append(f"{ctx}：evidenceRefs 为空，趋势没有证据引用（schema 允许，但页面会空）")
        counter = [x if isinstance(x, dict) else as_text(x).strip() for x in as_list(doc.get("counterEvidence")) if x]
        if not counter:
            errors.append(
                f"{ctx}：counterEvidence 为空（schema minItems=1）——必须写反证，"
                f"确实搜过没有就写「暂无：已检索 X/Y，未有反证」"
            )

        judgment = as_text(doc.get("currentJudgment")).strip()
        if not judgment:
            errors.append(f"{ctx}：缺 currentJudgment")
        review_by = parse_time(doc.get("reviewBy"), f"{ctx}.reviewBy", errors, allow_none=True)
        if stage in ("emerging", "strengthening", "stable") and not review_by:
            warnings.append(f"{ctx}：reviewBy 为空，活着的趋势要有复核期限")

        revisions = []
        for n, rev in enumerate(as_list(doc.get("revisions")), 1):
            if not isinstance(rev, dict):
                errors.append(f"{ctx}.revisions[{n}]：不是对象")
                continue
            at = parse_time(rev.get("at"), f"{ctx}.revisions[{n}].at", errors)
            note = as_text(rev.get("note", rev.get("reason", ""))).strip()
            stage_before = as_text(rev.get("stageBefore", rev.get("from", ""))).strip()
            if not note:
                errors.append(f"{ctx}.revisions[{n}]：状态改动必须写 note（为什么改）")
            elif not (4 <= C.char_len(note) <= 400):
                errors.append(f"{ctx}.revisions[{n}]：note {C.char_len(note)} 字，需在 4–400")
            if stage_before and stage_before not in TREND_STAGES:
                errors.append(f"{ctx}.revisions[{n}].stageBefore={stage_before!r} 不是合法 stage")
            item = {"at": _iso(at), "by": as_text(rev.get("by")).strip()[:40], "note": note}
            if stage_before:
                item["stageBefore"] = stage_before
            revisions.append(item)

        trends.append(
            {
                "id": tid,
                "title": as_text(doc.get("title")).strip() or None,
                "thesis": thesis,
                "firstSeenAt": _iso(first_seen),
                "stage": stage,
                "indicators": indicators,
                "evidenceRefs": evidence,
                "counterEvidence": counter,
                "currentJudgment": judgment,
                "reviewBy": _iso(review_by),
                "revisions": revisions,
                "counterSearch": doc.get("counterSearch"),
                "retro": doc.get("retro"),
            }
        )
    return trends, errors


def _iso(ts: datetime | None) -> str:
    if ts is None:
        return ""
    return ts.isoformat()


def attach_event_refs(trends: list[dict], events: list[dict]) -> None:
    by_trend: dict[str, list[dict]] = {t["id"]: [] for t in trends}
    for ev in events:
        for link in ev["trendLinks"]:
            if link["trendId"] in by_trend:
                by_trend[link["trendId"]].append(
                    {
                        "eventId": ev["id"],
                        "direction": link["direction"],
                        "reason": link["reason"],
                        "occurredAt": ev["occurredAt"],
                        "title": ev["title"],
                    }
                )
    for t in trends:
        refs = sorted(by_trend.get(t["id"], []), key=lambda r: r["occurredAt"] or "", reverse=True)
        t["eventRefs"] = refs
        t["supportCount"] = sum(1 for r in refs if r["direction"] == "support")
        t["counterCount"] = sum(1 for r in refs if r["direction"] in ("weaken", "challenge"))


# --------------------------------------------------------------------------
# forecast（判断追踪）：schema/forecast.schema.json
# --------------------------------------------------------------------------
def validate_and_normalize_forecasts(
    docs: list[dict],
    trend_ids: set[str],
    today: date,
    warnings: list[str],
    event_ids: set[str] | None = None,
):
    errors: list[str] = []
    out: list[dict] = []
    seen: dict[str, str] = {}
    for idx, doc in enumerate(docs):
        origin = doc.get("__source__", f"#{idx}")
        fid = as_text(doc.get("id")).strip()
        ctx = f"forecast[{fid or origin}]"
        if not fid:
            errors.append(f"{ctx}：缺 id")
            continue
        if not re.fullmatch(FORECAST_ID_RE, fid):
            errors.append(f"{ctx}：id 必须匹配 {FORECAST_ID_RE}：{fid!r}")
        if fid in seen:
            errors.append(f"{ctx}：id 重复（另一处在 {seen[fid]}）")
            continue
        seen[fid] = origin

        statement = as_text(doc.get("statement")).strip()
        if not statement:
            errors.append(f"{ctx}：缺 statement（要能被判定对错）")
        elif not (10 <= C.char_len(statement) <= 200):
            errors.append(f"{ctx}.statement {C.char_len(statement)} 字，需 10–200 字且能被判定对错")
        rationale = as_text(doc.get("rationale")).strip()
        if not rationale:
            errors.append(f"{ctx}：缺 rationale（判断依据，至少 20 字）")
        elif len(rationale) < 20:
            errors.append(f"{ctx}.rationale {len(rationale)} 字，至少 20 字——概率从哪来的要说清")
        elif len(rationale) > 1000:
            warnings.append(f"{ctx}.rationale {len(rationale)} 字偏长，正文会被截断")

        created = parse_time(doc.get("createdAt"), f"{ctx}.createdAt", errors)
        resolve_by = parse_time(doc.get("resolveBy"), f"{ctx}.resolveBy", errors)
        if created and resolve_by and resolve_by <= created:
            errors.append(f"{ctx}：resolveBy 必须晚于 createdAt")
        if resolve_by and resolve_by.date() < today and as_text(doc.get("status")) == "open":
            warnings.append(f"{ctx}：已过 resolveBy（{resolve_by.date()}）状态还是 open，该核验了")

        prob_raw = doc.get("probability")
        try:
            prob = float(prob_raw)
        except (TypeError, ValueError):
            prob = None
            errors.append(f"{ctx}.probability={prob_raw!r} 不是数字")
        if prob is not None and not (0.0 <= prob <= 1.0):
            errors.append(f"{ctx}.probability={prob} 必须在 0–1")

        indicators = [as_text(x).strip() for x in as_list(doc.get("indicators")) if as_text(x).strip()]
        if not indicators:
            errors.append(f"{ctx}：indicators 为空（schema minItems=1）")
        elif len(indicators) > 12:
            errors.append(f"{ctx}：indicators {len(indicators)} 条，上限 12")

        linked = [as_text(x).strip() for x in as_list(doc.get("linkedTrendIds")) if as_text(x).strip()]
        for tid in linked:
            if trend_ids and tid not in trend_ids:
                errors.append(f"{ctx}.linkedTrendIds：{tid!r} 在趋势库里不存在")

        evidence = []
        for n, ref in enumerate(as_list(doc.get("evidenceRefs")), 1):
            ref_s = as_text(ref).strip()
            if not re.fullmatch(EVENT_ID_RE, ref_s):
                errors.append(
                    f"{ctx}.evidenceRefs[{n}]：必须是事件 id（{EVENT_ID_RE}），"
                    f"不是 URL：{ref_s!r}"
                )
                continue
            if event_ids is not None and ref_s not in event_ids:
                warnings.append(f"{ctx}.evidenceRefs[{n}]：{ref_s} 不在已导出事件里，页面会链空")
            evidence.append(ref_s)

        status = as_text(doc.get("status")).strip()
        if status not in FORECAST_STATUS:
            errors.append(f"{ctx}.status={status!r} 必须是 {list(FORECAST_STATUS)}")

        resolution_raw = doc.get("resolution")
        resolution = None
        if status == "resolved":
            if not isinstance(resolution_raw, dict):
                errors.append(f"{ctx}：status=resolved 时 resolution 必填")
            else:
                outcome = as_text(resolution_raw.get("outcome")).strip()
                if outcome not in FORECAST_OUTCOMES:
                    errors.append(f"{ctx}.resolution.outcome={outcome!r} 必须是 {list(FORECAST_OUTCOMES)}")
                rnote = as_text(resolution_raw.get("note")).strip()
                if len(rnote) < 10:
                    errors.append(f"{ctx}.resolution.note 太短（至少 10 字，写清凭什么算兑现/未兑现）")
                resolution = {
                    "resolvedAt": _iso(
                        parse_time(
                            resolution_raw.get("resolvedAt"),
                            f"{ctx}.resolution.resolvedAt",
                            errors,
                        )
                    ),
                    "outcome": outcome,
                    "note": rnote,
                    "evidenceUrls": [
                        as_text(u).strip()
                        for u in as_list(resolution_raw.get("evidenceUrls"))
                        if is_http(as_text(u))
                    ],
                }
        elif isinstance(resolution_raw, dict):
            warnings.append(f"{ctx}：status={status} 但带了 resolution，页面可能显示成已判定")

        revisions = []
        for n, rev in enumerate(as_list(doc.get("revisions")), 1):
            if not isinstance(rev, dict):
                errors.append(f"{ctx}.revisions[{n}]：不是对象")
                continue
            rnote = as_text(rev.get("note", rev.get("reason", ""))).strip()
            if not rnote:
                errors.append(f"{ctx}.revisions[{n}]：缺 note")
            revisions.append(
                {
                    "at": _iso(parse_time(rev.get("at"), f"{ctx}.revisions[{n}].at", errors)),
                    "by": as_text(rev.get("by")).strip()[:40],
                    "note": rnote,
                }
            )

        out.append(
            {
                "id": fid,
                "statement": statement,
                "createdAt": _iso(created),
                "resolveBy": _iso(resolve_by),
                "probability": prob,
                "rationale": rationale,
                "indicators": indicators,
                "linkedTrendIds": linked,
                "evidenceRefs": evidence,
                "status": status,
                "resolution": resolution,
                "revisions": revisions,
                "overdue": bool(resolve_by and resolve_by.date() < today and status == "open"),
            }
        )
    # open 的判断按到期日近的排前面，已判定的沉底
    out.sort(key=lambda f: (f["status"] != "open", f["resolveBy"] or "", f["id"]))
    return out, errors


# --------------------------------------------------------------------------
# schema 对齐自检：导出脚本的常量必须和 schema/*.schema.json 一致
# --------------------------------------------------------------------------
def check_schema_alignment(schema_dir: Path) -> list[str]:
    problems: list[str] = []
    files = {
        "event": schema_dir / "event.schema.json",
        "trend": schema_dir / "trend.schema.json",
        "forecast": schema_dir / "forecast.schema.json",
    }
    loaded: dict[str, dict] = {}
    for name, path in files.items():
        if not path.exists():
            problems.append(f"缺 {path}")
            continue
        try:
            loaded[name] = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            problems.append(f"{path} 不是合法 JSON：{exc}")
    if problems:
        return problems

    def props(doc: dict) -> dict:
        return doc.get("properties") or {}

    ev = props(loaded["event"])
    tr = props(loaded["trend"])
    fc = props(loaded["forecast"])

    def cmp(label: str, schema_value, ours, normalize=lambda x: x) -> None:
        if normalize(schema_value) != normalize(ours):
            problems.append(f"{label}: schema={normalize(schema_value)!r} 脚本={normalize(ours)!r}")

    cmp("event.id.pattern", ev.get("id", {}).get("pattern"), EVENT_ID_RE)
    cmp("event.dedupeKey.pattern", ev.get("dedupeKey", {}).get("pattern"), DEDUPE_KEY_RE)
    cmp("event.category.enum", ev.get("category", {}).get("enum"), list(EVENT_CATEGORIES), sorted)
    cmp(
        "event.credibility.level.enum",
        ev.get("credibility", {}).get("properties", {}).get("level", {}).get("enum"),
        list(CREDIBILITY_LEVELS),
        sorted,
    )
    cmp("event.status.enum", ev.get("status", {}).get("enum"), list(EVENT_STATUS), sorted)
    cmp(
        "event.scores.scorerVersion.pattern",
        ev.get("scores", {}).get("properties", {}).get("scorerVersion", {}).get("pattern"),
        SCORER_VERSION_RE,
    )
    cmp(
        "event.trendLinks.trendId.pattern",
        ev.get("trendLinks", {}).get("items", {}).get("properties", {}).get("trendId", {}).get("pattern"),
        TREND_ID_RE,
    )
    cmp(
        "event.sources.type.enum",
        ev.get("sources", {}).get("items", {}).get("properties", {}).get("type", {}).get("enum"),
        list(SOURCE_TYPES),
        sorted,
    )
    for name, cap in DIMENSIONS:
        cmp(
            f"event.scores.{name}.maximum",
            ev.get("scores", {}).get("properties", {}).get(name, {}).get("maximum"),
            cap,
        )
    cmp("trend.id.pattern", tr.get("id", {}).get("pattern"), TREND_ID_RE)
    cmp("trend.stage.enum", tr.get("stage", {}).get("enum"), list(TREND_STAGES), sorted)
    cmp("forecast.id.pattern", fc.get("id", {}).get("pattern"), FORECAST_ID_RE)
    cmp("forecast.status.enum", fc.get("status", {}).get("enum"), list(FORECAST_STATUS), sorted)
    return problems


# --------------------------------------------------------------------------
# 写出
# --------------------------------------------------------------------------
def _filter_trend_refs(trends: list[dict], public_ids: set[str]) -> list[dict]:
    """公开导出：趋势只保留指向 published 事件的 evidenceRefs，避免链到内部记录。"""
    out = []
    for t in trends:
        refs = [r for r in (t.get("evidenceRefs") or []) if r in public_ids]
        evrefs = [r for r in (t.get("eventRefs") or [])
                  if isinstance(r, dict) and r.get("eventId") in public_ids]
        cev = [r for r in (t.get("counterEvidence") or [])
               if not isinstance(r, dict) or not r.get("eventId") or r.get("eventId") in public_ids]
        out.append({**t, "evidenceRefs": refs, "eventRefs": evrefs, "counterEvidence": cev})
    return out


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


# ---- 可视化派生字段（只读派生，不改源数据；口径见 schema/README.md）----
CONFIDENCE_RANK = {
    "multi_independent": (4, "多源独立"),
    "single_primary": (3, "单一官方"),
    "secondary_only": (2, "二手转述"),
    "unverified": (1, "未核验"),
}


def _domain(url: str) -> str | None:
    if not isinstance(url, str) or "://" not in url:
        return None
    from urllib.parse import urlsplit

    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return None
    if host.startswith("www."):
        host = host[4:]
    return host or None


def _event_viz(e: dict) -> dict:
    """置信度=等级序数（非概率）；热度=去重独立域名数（窗口=事件发生→采集）。"""
    grade = e.get("evidenceGrade") or (e.get("credibility") or {}).get("level")
    rank, label = CONFIDENCE_RANK.get(grade, (None, None))
    domains: list[str] = []
    for s in e.get("sources") or []:
        d = _domain(s.get("url", ""))
        if d and d not in domains:
            domains.append(d)
    heat = len(domains) if domains else None
    return {
        "importance": (e.get("scores") or {}).get("total"),
        "confidenceRank": rank,
        "confidenceLabel": label,
        "evidenceGrade": grade,
        "heat": heat,
        "heatBasis": "distinct_domains" if heat is not None else None,
        "heatWindow": (
            {"from": e.get("occurredAt"), "to": e.get("collectedAt")} if heat is not None else None
        ),
        "sourceDomains": domains,
    }


def _rank_ids(events: list[dict], key: str) -> list[str]:
    """降序排名；缺值恒排末位；同值按 id 稳定排序（与筛选组合时前端只需过滤）。"""
    return [
        e["id"]
        for e in sorted(
            events,
            key=lambda e: (e["viz"].get(key) is None, -(e["viz"].get(key) or 0), e["id"]),
        )
    ]


def _stats(events: list[dict], trends: list[dict]) -> dict:
    from collections import Counter

    by_cat = Counter(e.get("category") or "unknown" for e in events)
    by_month = Counter((e.get("occurredAt") or "")[:7] for e in events if e.get("occurredAt"))
    by_grade = Counter(e["viz"]["evidenceGrade"] or "unknown" for e in events)
    buckets: Counter = Counter()
    for e in events:
        t = e["viz"]["importance"]
        buckets["missing" if t is None else f"{(t // 10) * 10}-{(t // 10) * 10 + 9}"] += 1
    dirs: Counter = Counter()
    for e in events:
        for link in e.get("trendLinks") or []:
            dirs[link.get("direction") or "unknown"] += 1
    return {
        "byCategory": [{"label": k, "value": v} for k, v in sorted(by_cat.items())],
        "byMonth": [{"label": k, "value": v} for k, v in sorted(by_month.items())],
        "byEvidenceGrade": [{"label": k, "value": v} for k, v in sorted(by_grade.items())],
        "scoreBuckets": [{"label": k, "value": v} for k, v in sorted(buckets.items())],
        "trendDirections": [{"label": k, "value": v} for k, v in sorted(dirs.items())],
        "trendStages": [
            {"label": k, "value": v}
            for k, v in sorted(Counter(t.get("stage") or "unknown" for t in trends).items())
        ],
    }


def _trend_viz(t: dict, events_by_id: dict[str, dict]) -> dict:
    refs = [events_by_id[i] for i in (t.get("evidenceRefs") or []) if i in events_by_id]
    dirs = {"support": 0, "weaken": 0, "challenge": 0}
    for e in refs:
        for link in e.get("trendLinks") or []:
            if link.get("trendId") == t["id"] and link.get("direction") in dirs:
                dirs[link["direction"]] += 1
    return {
        "title": t.get("title"),
        "evidenceCount": len(refs),
        "directionCounts": dirs,
        "counterEvidenceCount": len(t.get("counterEvidence") or []),
        "stage": t.get("stage"),
    }


def _trend_series(t: dict, events_by_id: dict[str, dict]) -> list[dict]:
    """真实时间序列：只取已存在的证据事件，缺失日期的不补点、不插值。"""
    pts = []
    for eid in t.get("evidenceRefs") or []:
        e = events_by_id.get(eid)
        if not e or not e.get("occurredAt"):
            continue
        direction = None
        for link in e.get("trendLinks") or []:
            if link.get("trendId") == t["id"]:
                direction = link.get("direction")
                break
        pts.append({"date": e["occurredAt"][:10], "eventId": eid, "direction": direction})
    return sorted(pts, key=lambda p: (p["date"], p["eventId"]))


def build_payload(
    events,
    trends,
    generated_at: str,
    notes: list[str],
    warnings: list[str],
    forecasts: list[dict] | None = None,
    window_meta: dict | None = None,
):
    events_sorted = sorted(
        events, key=lambda e: (e["occurredAt"], e["collectedAt"], e["id"]), reverse=True
    )
    for e in events_sorted:
        e["viz"] = _event_viz(e)
    events_by_id = {e["id"]: e for e in events_sorted}
    trends_sorted = sorted(trends, key=lambda t: (t["firstSeenAt"], t["id"]), reverse=True)
    for t in trends_sorted:
        t["viz"] = _trend_viz(t, events_by_id)
        t["evidenceSeries"] = _trend_series(t, events_by_id)
    gates = {
        "homepageMinTotal": HOMEPAGE_MIN_TOTAL,
        "headlineMinTotal": HEADLINE_MIN_TOTAL,
        "headlineRequiresVerified": True,
    }
    counts = {
        "events": len(events_sorted),
        "trends": len(trends_sorted),
        "published": sum(1 for e in events_sorted if e["status"] == "published"),
        "pending": sum(1 for e in events_sorted if e["status"] == "pending"),
        "headline": sum(1 for e in events_sorted if e["display"]["headline"]),
        "homepage": sum(1 for e in events_sorted if e["display"]["homepage"]),
    }
    events_payload = {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAt": generated_at,
        "gates": gates,
        "counts": counts,
        "rankings": {
            "importance": _rank_ids(events_sorted, "importance"),
            "confidence": _rank_ids(events_sorted, "confidenceRank"),
            "heat": _rank_ids(events_sorted, "heat"),
            "time": [e["id"] for e in events_sorted],
        },
        "stats": _stats(events_sorted, trends_sorted),
        "events": events_sorted,
    }
    trends_payload = {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAt": generated_at,
        "gates": gates,
        "counts": {"trends": len(trends_sorted)},
        "trends": trends_sorted,
    }
    if window_meta:
        # 站点侧（from-export.mjs）直接沿用这里，不再自己写死日期
        events_payload["meta"] = {"window": window_meta}
    if notes:
        events_payload["notes"] = notes
    if warnings:
        events_payload["warnings"] = warnings

    forecasts_payload = None
    if forecasts:
        forecasts_sorted = sorted(
            forecasts,
            key=lambda f: (0 if f.get("status") == "open" else 1, f.get("resolveBy", ""), f["id"]),
        )
        counts["forecasts"] = len(forecasts_sorted)
        counts["forecastsOpen"] = sum(1 for f in forecasts_sorted if f.get("status") == "open")
        forecasts_payload = {
            "schemaVersion": SCHEMA_VERSION,
            "generatedAt": generated_at,
            "counts": {
                "forecasts": len(forecasts_sorted),
                "open": counts["forecastsOpen"],
            },
            "forecasts": forecasts_sorted,
        }
    return events_payload, trends_payload, forecasts_payload, counts


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def run(args) -> int:
    content_root = Path(args.content)
    out_root = Path(args.out)
    # 一次运行只解析一次窗口：--as-of/ERA_AS_OF > --max-ts/ERA_MAX_TS > 配置 > 时钟
    win = C.apply_window(
        C.load_pipeline_config(getattr(args, "config", None)),
        as_of=getattr(args, "as_of", None),
        max_ts=getattr(args, "max_ts", None),
    )
    today = win.to_date
    warnings: list[str] = []

    if not args.no_schema_check:
        schema_dir = Path(args.schema)
        if not schema_dir.exists():
            warnings.append(f"schema 目录不存在：{schema_dir}（跳过一致性检查）")
        else:
            problems = check_schema_alignment(schema_dir)
            if problems:
                raise ExportError(
                    "导出脚本与 schema 不同步（schema 是权威：改脚本常量，别改 schema 迁就代码）：\n  - "
                    + "\n  - ".join(problems)
                )

    events_raw, trends_raw, forecasts_raw, notes = load_content(content_root, args.format)
    for doc in events_raw + trends_raw + forecasts_raw:
        doc.setdefault("__source__", "")
    trend_ids = {as_text(t.get("id")).strip() for t in trends_raw if as_text(t.get("id")).strip()}

    events, ev_errors = validate_and_normalize_events(events_raw, trend_ids, today, warnings)
    trends, tr_errors = validate_and_normalize_trends(trends_raw, today, warnings)
    forecasts, fc_errors = validate_and_normalize_forecasts(
        forecasts_raw, trend_ids, today, warnings, event_ids={e["id"] for e in events}
    )
    attach_event_refs(trends, events)

    # 窗口过滤：content/ 保留全部历史，但展示数组只留窗口内（旧记录不参与导出，不删源）
    def _in_window(doc) -> bool:
        return win.judge(doc.get("occurredAt")) not in (C.JUDGE_OLD, C.JUDGE_FUTURE)

    dropped = [e for e in events if not _in_window(e)]
    if dropped:
        old = sum(1 for e in dropped if win.judge(e.get("occurredAt")) == C.JUDGE_OLD)
        warnings.append(
            f"窗口 {win.from_str} ~ {win.to_str} 之外 {len(dropped)} 条事件不参与导出"
            f"（历史 {old} 条 / 超出上界 {len(dropped) - old} 条）；content/ 原件保留"
        )
        events = [e for e in events if _in_window(e)]
    dropped_ids = {e["id"] for e in dropped}

    # 趋势引用了不在导出集里的事件：页面会链空，提前说清楚
    event_id_set = {e["id"] for e in events}
    for t in trends:
        for ref in t.get("evidenceRefs", []):
            if ref not in event_id_set and ref not in dropped_ids:
                warnings.append(
                    f"trend[{t['id']}]：evidenceRefs 里的 {ref} 不在已导出事件里，趋势页会链空"
                )

    errors = ev_errors + tr_errors + fc_errors
    generated_at = datetime.now().astimezone().replace(microsecond=0).isoformat()

    # 公开产物只收 published：pending 属于内部预览，不得进入公开导出与排行
    public_events = [e for e in events if e.get("status") == "published"]
    # 趋势的 evidenceRefs 由事件侧 trendLinks 派生，避免两处手写漂移
    linked: dict[str, set] = {}
    for e in events:
        for lk in (e.get("trendLinks") or []):
            tid = lk.get("trendId")
            if tid:
                linked.setdefault(tid, set()).add(e["id"])
    for t in trends:
        t["evidenceRefs"] = sorted(set(t.get("evidenceRefs") or []) | linked.get(t["id"], set()))
    public_ids = {e["id"] for e in public_events}
    public_trends = [t for t in _filter_trend_refs(trends, public_ids) if t["evidenceRefs"]]
    public_forecasts = ([f for f in forecasts if set(f.get("eventRefs") or []) & public_ids]
                        if public_ids else []) if forecasts else forecasts

    events_payload, trends_payload, forecasts_payload, counts = build_payload(
        public_events, public_trends, generated_at, notes, warnings, public_forecasts,
        window_meta=win.as_meta(),
    )
    counts["contentTotal"] = len(events) + len(dropped)
    counts["publishedTotal"] = sum(1 for e in events if e.get("status") == "published")
    counts["publicExported"] = len(public_events)
    counts["publicTrends"] = len(public_trends)
    counts["droppedOutOfWindow"] = len(dropped)
    if not public_events:
        warnings.append(
            f"公开导出 0 条：content 有 {len(events)} 条但 published=0；"
            "pending 只写 preview/，不会出现在公开产物里"
        )
    # 内部预览：全部记录（含 pending），只写到 out/preview/，不参与线上
    preview_events_payload, preview_trends_payload, preview_forecasts_payload, _pc = build_payload(
        events, trends, generated_at, list(notes), [], forecasts, window_meta=win.as_meta()
    )

    if args.as_json:
        report = {
            "ok": not errors,
            "format": args.format,
            "content": str(content_root),
            "check": bool(args.check),
            "out": str(out_root),
            "window": win.as_report(),
            "counts": counts,
            "errors": errors,
            "warnings": warnings,
            "notes": notes,
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"[export] 输入 {args.format}: {content_root}")
        print(f"[export] 窗口 {win.from_str} → {win.to_str}（{win.mode}，asOf {C.fmt_ts(win.as_of)} via {win.as_of_source}/{win.as_of_from}）"
              f" · 窗口外 {counts['droppedOutOfWindow']} 条不参与导出")
        print(f"[export] mode={args.mode} · content {counts['contentTotal']} 条 · published {counts['published']} 条 · "
              f"公开导出 {counts['publicExported']} 条（pending 只进 preview/）")
        print(f"[export] events {counts['events']}（published {counts['published']} / "
              f"pending {counts['pending']}）· trends {counts['trends']} · "
              f"头条 {counts['headline']} · 首页 {counts['homepage']}")
        if counts.get("forecasts"):
            print(f"[export] forecasts {counts['forecasts']}（open {counts['forecastsOpen']}）")
        for w in warnings:
            print(f"  WARN  {w}")
        for e in errors:
            print(f"  ERROR {e}")

    if errors:
        if not args.lenient:
            print(f"[export] 硬错误 {len(errors)} 条，未写盘。修完再来（或用 --lenient 只导不拦）。",
                  file=sys.stderr)
            return 1
        print(f"[export] --lenient：带 {len(errors)} 条硬错误继续导出，线上数据有风险。",
              file=sys.stderr)

    if args.check:
        say(args, "[export] --check：只校验，不写盘。")
        return 0

    if args.mode in ("public", "both"):
        events_path = out_root / "events.json"
        trends_path = out_root / "trends.json"
        _write_json(events_path, events_payload)
        _write_json(trends_path, trends_payload)
        say(args, f"[export] 公开写出 {events_path}（{len(public_events)} 条 published）")
        say(args, f"[export] 公开写出 {trends_path}（{len(public_trends)} 条）")
        if forecasts_payload is not None:
            forecasts_path = out_root / "forecasts.json"
            _write_json(forecasts_path, forecasts_payload)
            say(args, f"[export] 公开写出 {forecasts_path}（{len(public_forecasts or [])} 条）")

    if args.mode in ("preview", "both"):
        preview_root = out_root / "preview"
        _write_json(preview_root / "events.json", preview_events_payload)
        _write_json(preview_root / "trends.json", preview_trends_payload)
        if preview_forecasts_payload is not None:
            _write_json(preview_root / "forecasts.json", preview_forecasts_payload)
        say(args, f"[export] 内部预览写出 {preview_root}（{len(events)} 条，含 pending，禁止上线）")
    return 0


def say(args, message: str) -> None:
    """--json 时正文只留 JSON，人看的提示走 stderr。"""
    print(message, file=sys.stderr if args.as_json else sys.stdout)


# --------------------------------------------------------------------------
# 自检：内置样例 + frontmatter 解析器用例
# --------------------------------------------------------------------------
def selftest() -> int:
    failures: list[str] = []

    def check(name: str, got, want) -> None:
        if got != want:
            failures.append(f"{name}: got {got!r}, want {want!r}")

    fm = """id: ev-sample
title: 样例事件
tags: [ai, chip]
list:
  - a
  - b
nested:
  url: https://example.com/x
  publishedAt: 2026-05-01
sources:
  - url: https://example.com/a
    title: 来源A
    type: primary
  - url: https://example.com/b
    title: 来源B
    type: corroboration
block: |
  第一行
  第二行
"""
    parsed = parse_frontmatter(fm, "selftest")
    check("fm.id", parsed.get("id"), "ev-sample")
    check("fm.tags", parsed.get("tags"), ["ai", "chip"])
    check("fm.list", parsed.get("list"), ["a", "b"])
    check("fm.nested.url", parsed.get("nested", {}).get("url"), "https://example.com/x")
    check("fm.sources.len", len(parsed.get("sources") or []), 2)
    check("fm.sources[1].type", (parsed.get("sources") or [{}])[1].get("type"), "corroboration")
    check("fm.block", parsed.get("block"), "第一行\n第二行")

    md = "---\nid: x\ntitle: t\n---\n正文"
    body_fm, body = split_markdown(md)
    check("split.fm", body_fm, "id: x\ntitle: t")
    check("split.body", body, "正文")

    flags = _display_flags("published", "verified", 72)
    check("flags.headline", flags["headline"], True)
    check("flags.unverified_headline", _display_flags("published", "unverified", 90)["headline"], False)
    check("flags.low_total_homepage", _display_flags("published", "verified", 49)["homepage"], False)
    check("flags.pending_public", _display_flags("pending", "verified", 90)["public"], False)

    if failures:
        print("SELFTEST FAILED")
        for f in failures:
            print("  " + f)
        return 1
    print("SELFTEST OK：frontmatter 解析 / 分块 / 发布门槛 12 项通过")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="ERA 数据导出：content/ → data/{events,trends,forecasts}.json（跳过 _ 暂存目录）"
    )
    parser.add_argument("--content", default="content", help="输入目录（默认 content/）")
    parser.add_argument("--out", default="data", help="输出目录（默认 data/）")
    parser.add_argument(
        "--schema", default="schema", help="schema 目录，用于一致性检查（默认 schema/）"
    )
    parser.add_argument(
        "--no-schema-check", action="store_true", help="跳过 schema 一致性检查（不建议）"
    )
    parser.add_argument(
        "--format",
        default="json",
        choices=("json", "collections"),
        help="json=纯 JSON（默认）；collections=Astro content collections 的 md frontmatter",
    )
    parser.add_argument("--check", action="store_true", help="只校验，不写盘")
    parser.add_argument(
        "--mode", choices=("public", "preview", "both"), default="public",
        help="public=只写公开产物（仅 published，CI/Pages 只能用它）；"
             "preview=只写内部预览（含 pending）；both=两者都写（本地调试）",
    )
    parser.add_argument("--as-of", default=None,
                        help="窗口基准时刻（ISO 8601）；默认读 ERA_AS_OF，再退到 --max-ts/配置/时钟")
    parser.add_argument("--max-ts", default=None, help="等价 as-of 的数据时间上限")
    parser.add_argument("--config", default=None, help="pipeline.config.json 路径（默认仓库根）")
    parser.add_argument("--lenient", action="store_true", help="有硬错误也照样导出（不推荐）")
    parser.add_argument("--json", action="store_true", dest="as_json", help="机器可读输出")
    parser.add_argument("--selftest", action="store_true", help="跑内置自检")
    args = parser.parse_args(argv)
    if args.selftest:
        return selftest()
    try:
        return run(args)
    except ExportError as exc:
        print(f"[export] {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"[export] IO 错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
