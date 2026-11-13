#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ERA 数据层查错：来源核验 + 一致性校验（仅标准库）。

用法：
  python3 scripts/verify_sources.py --content content/ --report reports/source-check.md
  python3 scripts/verify_sources.py --offline          # 只做一致性，不发 HTTP
  python3 scripts/verify_sources.py --json             # 结果打到 stdout（供 CI/其他脚本）
  python3 scripts/verify_sources.py --only-http        # 只核来源
  python3 scripts/verify_sources.py --apply            # 回写 verification/evidenceGrade

退出码：0 = 无 ERROR；1 = 有 ERROR（必须剔除）；2 = 输入/用法错误。
默认只读 content/，只写 --report；加 --apply 才回写 content/（原子替换、保留原缩进）。
"""

import argparse
import concurrent.futures
import html as html_mod
import json
import os
import re
import sys
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

TZ = timezone(timedelta(hours=8))
DEFAULT_DEADLINE = "2026-11-13"
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

SCORE_SPEC = [
    ("structural", 25),
    ("scope", 20),
    ("signal", 20),
    ("delta", 15),
    ("timeliness", 10),
    ("crossDomain", 10),
]

TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
OG_TITLE_PROP_FIRST = re.compile(
    r'<meta[^>]+property=["\']og:title["\'][^>]*content=["\']([^"\']*)', re.I
)
OG_TITLE_CONTENT_FIRST = re.compile(
    r'<meta[^>]+content=["\']([^"\']*)["\'][^>]*property=["\']og:title', re.I
)
DATE_META_PATTERNS = [
    re.compile(
        r'<meta[^>]+(?:property|name|itemprop)=["\']'
        r'(?:article:published_time|article:published|publishdate|pubdate|'
        r'datepublished|published_time|parsely-pub-date|og:published_time|'
        r'date|pub_time)["\'][^>]+content=["\']([^"\']+)',
        re.I,
    ),
    re.compile(
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\']'
        r'(?:article:published_time|publishdate|pubdate|datepublished|date)["\']',
        re.I,
    ),
]
JSONLD_DATE = re.compile(r'"datePublished"\s*:\s*"([^"]+)"', re.I)
CHARSET_RE = re.compile(r'charset=["\']?([\w-]+)', re.I)


# ---------------------------------------------------------------- 基础工具


def parse_dt(value):
    """把常见时间戳解析成带时区的 datetime；失败返回 None。"""
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        dt = None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y/%m/%d"):
            try:
                dt = datetime.strptime(raw[:19], fmt)
                break
            except ValueError:
                continue
        if dt is None:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt


def norm_text(s):
    """标题归一：去掉空白/标点，全角转半角，小写。"""
    if not isinstance(s, str):
        return ""
    s = unicodedata.normalize("NFKC", s).lower()
    return re.sub(r"[\s\W_]+", "", s, flags=re.UNICODE)


def bigrams(s):
    return {s[i : i + 2] for i in range(max(len(s) - 1, 0))}


def title_score(expected, actual):
    """0~1。包含关系直接 1.0，否则用二元组 Dice。"""
    a, b = norm_text(expected), norm_text(actual)
    if not a or not b:
        return 0.0
    if a in b or b in a:
        return 1.0
    ga, gb = bigrams(a), bigrams(b)
    if not ga or not gb:
        return 0.0
    return 2 * len(ga & gb) / (len(ga) + len(gb))


def decode_body(raw, content_type):
    charset = None
    if content_type:
        m = CHARSET_RE.search(content_type)
        if m:
            charset = m.group(1)
    if not charset:
        m = CHARSET_RE.search(raw[:4096].decode("latin-1", "ignore"))
        if m:
            charset = m.group(1)
    for enc in filter(None, [charset, "utf-8", "gb18030"]):
        try:
            return raw.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", "replace")


def extract_page_meta(html):
    title = ""
    m = TITLE_RE.search(html)
    if m:
        title = html_mod.unescape(re.sub(r"\s+", " ", m.group(1))).strip()
    if not title:
        for rx in (OG_TITLE_PROP_FIRST, OG_TITLE_CONTENT_FIRST):
            m = rx.search(html)
            if m:
                title = html_mod.unescape(m.group(1)).strip()
                break
    published = ""
    for rx in DATE_META_PATTERNS:
        m = rx.search(html)
        if m:
            published = m.group(1).strip()
            break
    if not published:
        m = JSONLD_DATE.search(html)
        if m:
            published = m.group(1).strip()
    return title, published


CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
TAG_STRIP_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.S | re.I)


def cjk_ratio(s):
    if not s:
        return 0.0
    return len(CJK_RE.findall(s)) / len(s)


def extract_text(html):
    s = TAG_STRIP_RE.sub(" ", html)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html_mod.unescape(s)
    return re.sub(r"\s+", " ", s).strip()[:8000]


def bigram_cover(expected, body):
    ga = bigrams(norm_text(expected))
    b = norm_text(body)
    if not ga or not b:
        return 0.0
    return sum(1 for g in ga if g in b) / len(ga)


# ---------------------------------------------------------------- 输入


def iter_records(content_dir, with_meta=False):
    """递归读 content/*.json，产出 (相对路径, 记录, 类型)。

    with_meta=True 时多带 (path, idx)，供 --apply 精准回写。
    """
    root = Path(content_dir)
    files = sorted(p for p in root.rglob("*.json") if not p.name.startswith("."))
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            item = {"__load_error__": str(exc)}
            if with_meta:
                yield str(path.relative_to(root)), item, "error", path, None
            else:
                yield str(path.relative_to(root)), item, "error"
            continue
        items = []
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            for key in ("events", "items", "records", "trends", "forecasts"):
                if isinstance(data.get(key), list):
                    items = data[key]
                    break
            else:
                items = [data]
        for idx, obj in enumerate(items):
            if not isinstance(obj, dict):
                continue
            if "statement" in obj and (
                "probability" in obj or "resolveBy" in obj or "firstJudgment" in obj
            ):
                kind = "forecast"
            elif "thesis" in obj or "stage" in obj or "indicators" in obj:
                kind = "trend"
            elif "sources" in obj or "scores" in obj or "dedupeKey" in obj:
                kind = "event"
            elif "statement" in obj or "predictions" in obj or "forecastRefs" in obj:
                kind = "forecast"
            else:
                kind = "unknown"
            rel = str(path.relative_to(root))
            if len(items) > 1:
                rel = f"{rel}[{idx}]"
            if with_meta:
                yield rel, obj, kind, path, idx
            else:
                yield rel, obj, kind


# ---------------------------------------------------------------- HTTP 核验


def check_url(url, timeout, retries):
    """返回 {status, final_url, page_title, page_published, error}。"""
    result = {
        "url": url,
        "status": None,
        "final_url": url,
        "page_title": "",
        "page_published": "",
        "page_text": "",
        "error": "",
    }
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        result["error"] = "非法 URL（只接受 http/https 绝对地址）"
        return result
    last_err = ""
    for attempt in range(retries + 1):
        try:
            req = Request(
                url,
                headers={
                    "User-Agent": UA,
                    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                    "Accept-Encoding": "identity",
                },
            )
            with urlopen(req, timeout=timeout) as resp:
                raw = resp.read(262144)
                result["status"] = resp.status
                result["final_url"] = resp.geturl() or url
                ctype = resp.headers.get("Content-Type", "")
                if "html" in ctype.lower() or not ctype:
                    html = decode_body(raw, ctype)
                    result["page_title"], result["page_published"] = extract_page_meta(
                        html
                    )
                    result["page_text"] = extract_text(html)
                else:
                    result["page_title"] = ""
                result["error"] = ""
                return result
        except HTTPError as exc:
            result["status"] = exc.code
            result["final_url"] = getattr(exc, "url", url) or url
            # 4xx/5xx 不重试（除 5xx）
            if 500 <= exc.code < 600 and attempt < retries:
                last_err = f"HTTP {exc.code}"
                continue
            result["error"] = f"HTTP {exc.code} {exc.reason}"
            return result
        except (URLError, TimeoutError, OSError) as exc:
            last_err = str(getattr(exc, "reason", exc))
            if attempt < retries:
                continue
            result["error"] = f"请求失败：{last_err}"
            return result
    result["error"] = last_err or "请求失败"
    return result


def judge_source(src, fetched, deadline_dt, title_gate=True):
    """单个来源 → (verdict, http, title_ratio, note)。verdict ∈ PASS/WARN/FAIL。

    title_gate=False 用于原始候选：标题可能已被改写，相似度只作提示不判错。
    """
    status = fetched.get("status")
    err = fetched.get("error", "")
    final_url = fetched.get("final_url") or src.get("url", "")
    page_title = fetched.get("page_title", "")
    expected_title = src.get("title", "") if isinstance(src.get("title"), str) else ""
    ratio = title_score(expected_title, page_title) if page_title else None

    if err and status is None:
        low = err.lower()
        dns_fail = any(
            k in low
            for k in (
                "name or service not known",
                "nodename nor servname",
                "getaddrinfo",
                "name resolution",
                "no address associated",
                "no such host",
            )
        )
        if dns_fail:
            return "FAIL", "-", ratio, err
        return "WARN", "-", ratio, f"{err}（网络层失败，不等于死链，可重试）"
    if status in (404, 410):
        return "FAIL", str(status), ratio, "死链（404/410）"
    if status in (401, 403, 429) or (status and 500 <= status < 600):
        return "WARN", str(status), ratio, "被拦截或服务端错误，无法核验，需人工确认"
    if status and status >= 400:
        return "FAIL", str(status), ratio, f"HTTP {status}"

    info, block = [], []
    if title_gate:
        cross_lang = (
            bool(page_title)
            and cjk_ratio(expected_title) >= 0.3
            and cjk_ratio(page_title) < 0.05
        )
        if cross_lang:
            info.append("跨语种页面（英文标题），标题相似度不适用，仅核 HTTP")
        elif not page_title:
            block.append("页面无标题可核")
        elif ratio is not None and ratio < 0.12:
            cover = bigram_cover(expected_title, fetched.get("page_text", ""))
            if cover >= 0.3:
                block.append(
                    f"标题不匹配但正文相关（标题 {ratio:.2f}／正文覆盖 {cover:.2f}），需人工确认"
                )
            else:
                return "FAIL", str(status), ratio, "标题与正文均无有效重叠，疑似错链"
        elif ratio < 0.4:
            block.append("标题相似度低，需人工确认")
        elif ratio < 0.75:
            block.append("标题部分匹配")
    else:
        if page_title and ratio is not None:
            hint = "（偏低，候选标题可能改写）" if ratio < 0.3 else ""
            info.append(f"标题相似度 {ratio:.2f}{hint}")
        else:
            info.append("页面无标题可核")
    if urlparse(final_url).netloc != urlparse(src.get("url", "")).netloc:
        block.append(f"跨域跳转 → {urlparse(final_url).netloc}")
    page_date = fetched.get("page_published", "")
    if not page_date:
        info.append("页面无发布日期（日期未核）")
    else:
        pdt = parse_dt(page_date)
        if pdt and pdt > deadline_dt:
            block.append("页面发布日期晚于截止日")
        src_dt = parse_dt(src.get("publishedAt"))
        if (
            title_gate
            and pdt
            and src_dt
            and abs((pdt - src_dt).total_seconds()) > 3 * 86400
        ):
            block.append(f"记录日期 {src_dt.date()} 与页面 {pdt.date()} 相差>3天")
    if block:
        return "WARN", str(status), ratio, "；".join(block + info)
    return "PASS", str(status), ratio, "；".join(info) if info else "OK"


# ---------------------------------------------------------------- 一致性


def is_number(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def check_consistency(events, trends, forecasts, deadline_dt):
    """返回 {record_key: [ {level, field, msg}, ... ]}。"""
    issues = {}
    events = [e for e in events if isinstance(e, dict)]
    seen_ids = {}
    seen_keys = {}
    trend_ids = {t.get("id") for t in trends if isinstance(t, dict)}
    forecast_ids = {f.get("id") for f in forecasts if isinstance(f, dict)}

    def add(rec, level, field, msg):
        issues.setdefault(rec.get("id") or rec.get("_file") or "?", []).append(
            {"level": level, "field": field, "msg": msg}
        )

    for ev in events:
        if "__load_error__" in ev:
            add(ev, "ERROR", "file", f"JSON 解析失败：{ev['__load_error__']}")
            continue

        eid = ev.get("id")
        if not eid:
            add(ev, "ERROR", "id", "缺 id")
        else:
            if eid in seen_ids:
                add(ev, "ERROR", "id", f"id 重复（与 {seen_ids[eid]} 冲突）")
            else:
                seen_ids[eid] = ev.get("_file", "?")

        dk = ev.get("dedupeKey")
        if not dk:
            add(ev, "ERROR", "dedupeKey", "缺 dedupeKey")
        else:
            if dk in seen_keys:
                add(ev, "ERROR", "dedupeKey", f"dedupeKey 重复（与 {seen_keys[dk]} 冲突）")
            else:
                seen_keys[dk] = ev.get("_file", "?")

        status = ev.get("status")
        if status not in ("published", "pending"):
            add(ev, "ERROR", "status", f"status 非法：{status!r}（published|pending）")

        # 来源
        sources = ev.get("sources")
        if not isinstance(sources, list) or not sources:
            add(ev, "ERROR", "sources", "无 sources")
            sources = []
        primary = 0
        for i, src in enumerate(sources):
            if not isinstance(src, dict):
                add(ev, "ERROR", f"sources[{i}]", "不是对象")
                continue
            stype = src.get("type")
            if stype == "primary":
                primary += 1
            elif stype != "corroboration":
                add(
                    ev,
                    "ERROR",
                    f"sources[{i}].type",
                    f"type 非法：{stype!r}（primary|corroboration）",
                )
            if not isinstance(src.get("url"), str) or not src.get("url"):
                add(ev, "ERROR", f"sources[{i}].url", "缺 url")
            if not isinstance(src.get("title"), str) or not src.get("title"):
                add(ev, "WARN", f"sources[{i}].title", "缺标题，无法做标题匹配")
        if status == "published" and primary == 0:
            add(ev, "ERROR", "sources", "published 但没有任何 primary 来源")
        cred = ev.get("credibility")
        if (
            status == "published"
            and isinstance(cred, dict)
            and cred.get("level") == "unverified"
        ):
            add(
                ev,
                "ERROR",
                "credibility.level",
                "published 但 credibility=unverified（schema 要求降为 pending）",
            )

        # 分数
        scores = ev.get("scores")
        if not isinstance(scores, dict):
            add(ev, "ERROR", "scores", "缺 scores")
        else:
            dims_sum = 0.0
            dims_ok = True
            for name, cap in SCORE_SPEC:
                v = scores.get(name)
                if not is_number(v):
                    add(ev, "ERROR", f"scores.{name}", f"缺失或非数字：{v!r}")
                    dims_ok = False
                    continue
                if v < 0:
                    add(ev, "ERROR", f"scores.{name}", f"为负：{v}")
                if v > cap:
                    add(ev, "ERROR", f"scores.{name}", f"{v} 超过上限 {cap}")
                dims_sum += float(v)
            total = scores.get("total")
            if not is_number(total):
                add(ev, "ERROR", "scores.total", f"缺失或非数字：{total!r}")
            elif dims_ok and abs(float(total) - dims_sum) > 1e-6:
                add(
                    ev,
                    "ERROR",
                    "scores.total",
                    f"total={total} ≠ 六维和 {dims_sum:g}（禁止手填总分）",
                )
            rationale = scores.get("rationale")
            if isinstance(rationale, dict):
                missing_dims = [
                    n for n, _ in SCORE_SPEC
                    if not isinstance(rationale.get(n), str)
                    or len(rationale.get(n, "").strip()) < 6
                ]
                if missing_dims:
                    add(
                        ev,
                        "WARN",
                        "scores.rationale",
                        "逐维依据缺失或过短：" + "、".join(missing_dims),
                    )
            elif isinstance(rationale, str) and rationale.strip():
                add(
                    ev,
                    "WARN",
                    "scores.rationale",
                    "schema 要求六维对象，当前是字符串",
                )
            else:
                add(ev, "WARN", "scores.rationale", "缺逐维依据")
            if not scores.get("scorerVersion"):
                add(ev, "WARN", "scores.scorerVersion", "缺 scorerVersion")

        # 时间戳
        ts = {}
        for field in ("occurredAt", "firstReportedAt", "collectedAt"):
            raw = ev.get(field)
            dt = parse_dt(raw)
            ts[field] = dt
            if raw in (None, ""):
                add(ev, "ERROR", field, "缺时间戳")
            elif dt is None:
                add(ev, "ERROR", field, f"时间戳不合法：{raw!r}")
            elif dt > deadline_dt:
                add(ev, "ERROR", field, f"{raw} 晚于截止日 {deadline_dt.date()}")
        for i, src in enumerate(sources):
            if not isinstance(src, dict):
                continue
            raw = src.get("publishedAt")
            if raw in (None, ""):
                continue
            dt = parse_dt(raw)
            if dt is None:
                add(ev, "WARN", f"sources[{i}].publishedAt", f"日期不合法：{raw!r}")
            elif dt > deadline_dt:
                add(
                    ev,
                    "WARN",
                    f"sources[{i}].publishedAt",
                    f"{raw} 晚于截止日，确认是否笔误",
                )
        occ, rep, col = ts["occurredAt"], ts["firstReportedAt"], ts["collectedAt"]
        if occ and rep and occ > rep:
            add(ev, "ERROR", "occurredAt", "事件发生时间晚于首次报道时间")
        if rep and col and rep > col:
            add(ev, "ERROR", "firstReportedAt", "首次报道时间晚于采集时间")

        # retro：schema 为对象 {isRetro, asOf, note}
        retro = ev.get("retro")
        if not isinstance(retro, dict):
            add(ev, "ERROR", "retro", "retro 应为对象 {isRetro, asOf, note}")
            retro = {}
        retro_on = retro.get("isRetro") is True
        if retro_on:
            as_of_raw = retro.get("asOf")
            as_of = parse_dt(as_of_raw)
            if not as_of_raw:
                add(ev, "ERROR", "retro.asOf", "isRetro=true 必须写 asOf")
            elif as_of is None:
                add(ev, "ERROR", "retro.asOf", f"asOf 时间不合法：{as_of_raw!r}")
            if not isinstance(retro.get("note"), str) or not retro.get("note", "").strip():
                add(ev, "WARN", "retro.note", "isRetro=true 建议写 note 说明补录口径")
            if as_of and col and as_of > col:
                add(ev, "ERROR", "retro.asOf", "asOf 晚于 collectedAt（回溯点不能晚于入库时间）")
            if as_of and rep and rep > as_of:
                add(
                    ev,
                    "WARN",
                    "retro.asOf",
                    "firstReportedAt 晚于 asOf（回溯基准时还不知道这件事，确认口径）",
                )
            if ev.get("forecastRefs"):
                add(
                    ev,
                    "WARN",
                    "forecastRefs",
                    "回溯条目引用了预测，确认不是事后诸葛亮",
                )

        # 引用
        for i, link in enumerate(ev.get("trendLinks") or []):
            if not isinstance(link, dict):
                add(ev, "ERROR", f"trendLinks[{i}]", "不是对象")
                continue
            tid = link.get("trendId")
            if trend_ids and tid not in trend_ids:
                add(ev, "WARN", f"trendLinks[{i}].trendId", f"引用不存在的趋势 {tid!r}")
            if link.get("direction") not in ("support", "weaken", "challenge"):
                add(
                    ev,
                    "ERROR",
                    f"trendLinks[{i}].direction",
                    f"direction 非法：{link.get('direction')!r}",
                )
        for i, ref in enumerate(ev.get("forecastRefs") or []):
            fid = ref.get("id") if isinstance(ref, dict) else ref
            if forecast_ids and fid not in forecast_ids:
                add(ev, "WARN", f"forecastRefs[{i}]", f"引用不存在的预测 {fid!r}")

    for tr in trends:
        if not isinstance(tr, dict) or "__load_error__" in tr:
            continue
        if not tr.get("id"):
            add(tr, "ERROR", "id", "趋势缺 id")
        if tr.get("stage") not in (
            "emerging",
            "strengthening",
            "stable",
            "weakening",
            "falsified",
        ):
            add(tr, "ERROR", "stage", f"stage 非法：{tr.get('stage')!r}")
        if not tr.get("counterEvidence"):
            add(tr, "WARN", "counterEvidence", "趋势无任何反证")
        rb = parse_dt(tr.get("reviewBy"))
        if tr.get("reviewBy") and rb is None:
            add(tr, "WARN", "reviewBy", "reviewBy 不是合法日期")

    return issues


# ---------------------------------------------------------------- 报告


# ---------------------------------------------------------------- 回写（--apply）

APPLY_METHOD = "verify_sources/http"


def detect_indent(text):
    """沿用文件原有缩进，避免回写产生整文件 diff。"""
    for line in text.splitlines()[1:]:
        stripped = line.lstrip(" ")
        if stripped and len(line) > len(stripped):
            return len(line) - len(stripped)
    return 2


def compute_verification(rows, previous=None):
    """由来源核验结果推导 verification.status 与 evidenceGrade。

    规则：
    - primary 有 FAIL（404/410/错链）→ failed
    - primary 全部 PASS → passed
    - primary 被拦截/超时（WARN）或无 primary → pending
    - 上次 passed、本次只是被拦截（无 FAIL）→ 保留 passed，checks 记 warn
    """
    checks = []
    for r in rows:
        # schema 只允许 pass/fail/unknown；WARN 归为 unknown（无法判定）
        result = {"PASS": "pass", "WARN": "unknown", "FAIL": "fail"}.get(
            r["verdict"], "unknown"
        )
        evidence = f'HTTP {r["http"]} {r["url"]}'
        if r.get("note"):
            evidence += f'（{r["note"]}）'
        checks.append({"kind": "http", "result": result, "evidence": evidence})

    prim = [r for r in rows if r.get("type") == "primary"]
    corr = [r for r in rows if r.get("type") != "primary"]
    ok_prim = [r for r in prim if r["verdict"] == "PASS"]
    ok_corr = [r for r in corr if r["verdict"] == "PASS"]
    failed_prim = [r for r in prim if r["verdict"] == "FAIL"]
    checks.append(
        {
            "kind": "primary_present",
            "result": "pass" if prim else "fail",
            "evidence": f"primary {len(prim)} 条" if prim else "无 type=primary 来源",
        }
    )

    domains = []
    for r in ok_prim + ok_corr:
        host = urlparse(r.get("final_url") or r["url"]).netloc.lower()
        if host and host not in domains:
            domains.append(host)
    if ok_prim and len(domains) >= 2:
        grade = "multi_independent"
    elif ok_prim:
        grade = "single_primary"
    elif ok_corr:
        grade = "secondary_only"
    else:
        grade = "unverified"

    if failed_prim:
        status = "failed"
    elif prim and all(r["verdict"] == "PASS" for r in prim):
        status = "passed"
    else:
        status = "pending"

    prev = previous or {}
    if (
        status == "pending"
        and prev.get("status") == "passed"
        and not failed_prim
        and prev.get("checks")
    ):
        if not prim:
            grade = "secondary_only" if ok_corr else prev.get("grade") or grade
        else:
            grade = prev.get("grade") or grade
        status = "passed"
        checks.append(
            {
                "kind": "regression",
                "result": "unknown",
                "evidence": "本次有来源被拦截/超时，保留上次 passed，需复查",
            }
        )
    return status, grade, checks


def update_record(path, idx, mutate):
    """原子更新一条记录（保留原缩进）；返回 (是否写盘, 错误说明)。"""
    path = Path(path)
    raw = path.read_text(encoding="utf-8")
    data = json.loads(raw)
    target = None
    if isinstance(data, list):
        if idx is None or idx >= len(data):
            return False, "索引越界"
        target = data[idx]
    elif isinstance(data, dict):
        list_key = next(
            (
                k
                for k in ("events", "items", "records", "trends", "forecasts")
                if isinstance(data.get(k), list)
            ),
            None,
        )
        if list_key:
            items = data[list_key]
            if idx is None or idx >= len(items):
                return False, "索引越界"
            target = items[idx]
        else:
            target = data
    else:
        return False, "顶层不是 JSON 对象"

    if not isinstance(target, dict):
        return False, "记录不是对象"
    mutate(target)
    text = json.dumps(data, ensure_ascii=False, indent=detect_indent(raw))
    if raw.endswith("\n"):
        text += "\n"
    if text == raw:
        return False, ""
    tmp = path.with_name(path.name + ".applytmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    return True, ""


def apply_verification(path, idx, rec, rows, now_iso):
    """把本次核验结果写回一条 event，返回报告行。"""
    prev = rec.get("verification")
    prev = dict(prev) if isinstance(prev, dict) else {}
    prev_status = prev.get("status") or ""
    prev["grade"] = rec.get("evidenceGrade") or ""
    status, grade, checks = compute_verification(rows, previous=prev)

    state_changed = (
        prev.get("status") != status
        or rec.get("evidenceGrade") != grade
        or prev.get("checks") != checks
    )

    def mutate(obj):
        obj["evidenceGrade"] = grade
        v = obj.get("verification")
        v = dict(v) if isinstance(v, dict) else {}
        v["method"] = APPLY_METHOD
        v["status"] = status
        v["checks"] = checks
        note = v.get("note")
        if isinstance(note, str) and "未做 HTTP" in note:
            v["note"] = "verify_sources.py 自动核验（HTTP），以 checks 为准"
        if state_changed or not v.get("checkedAt"):
            v["checkedAt"] = now_iso
        obj["verification"] = v

    changed, err = update_record(path, idx, mutate)
    return {
        "id": rec.get("id") or rec.get("_file") or "?",
        "prevStatus": prev_status,
        "status": status,
        "grade": grade,
        "changed": changed,
        "error": err,
    }


def fmt_ratio(r):
    return "-" if r is None else f"{r:.2f}"


def build_report(args, events, sources_results, issues, counts, applied=None):
    lines = []
    lines.append("# ERA 来源核验 & 一致性报告")
    lines.append("")
    lines.append(f"- 生成时间：{datetime.now(TZ).strftime('%Y-%m-%d %H:%M:%S %z')}")
    lines.append(f"- 输入：`{args.content}`（event {len(events)} 条）")
    if args.offline:
        lines.append("- 模式：仅一致性（未发 HTTP）")
    lines.append(
        f"- 来源：PASS {counts['pass']} / WARN {counts['warn']} / FAIL {counts['fail']}"
    )
    if args.only_http:
        lines.append("- 一致性：跳过（--only-http）")
    else:
        lines.append(
            f"- 一致性：ERROR {counts['error']} / WARN {counts['iwarn']}"
        )
    lines.append(f"- **必须剔除：{len(counts['must_drop'])} 条**")
    if args.apply:
        upd = sum(1 for a in (applied or []) if a["changed"])
        lines.append(
            f"- 回写（--apply）：更新 {upd} 条 / 不变 {len(applied or []) - upd} 条"
        )
    lines.append("")

    lines.append("## 一、必须剔除（阻断发布）")
    if counts["must_drop"]:
        for item in counts["must_drop"]:
            lines.append(f"- `{item['id']}` — {item['reason']}")
    else:
        lines.append("- 无。")
    lines.append("")

    if not args.only_consistency and not args.offline:
        lines.append("## 二、来源核验")
        lines.append("")
        lines.append("| 事件 | PASS | WARN | FAIL | 结论 |")
        lines.append("| --- | --- | --- | --- | --- |")
        for ev in events:
            eid = ev.get("id") or ev.get("_file") or "?"
            rows = sources_results.get(eid, [])
            p = sum(1 for r in rows if r["verdict"] == "PASS")
            w = sum(1 for r in rows if r["verdict"] == "WARN")
            f = sum(1 for r in rows if r["verdict"] == "FAIL")
            verdict = "FAIL" if f else ("WARN" if w else "PASS")
            lines.append(f"| `{eid}` | {p} | {w} | {f} | {verdict} |")
        lines.append("")
        lines.append("### 非 PASS 明细")
        lines.append("")
        lines.append("| 事件 | 来源 URL | 结果 | HTTP | 标题匹配 | 说明 |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        any_row = False
        for eid, rows in sources_results.items():
            for r in rows:
                if r["verdict"] == "PASS":
                    continue
                any_row = True
                url = r["url"].replace("|", "%7C")
                note = r["note"].replace("|", "/")
                lines.append(
                    f"| `{eid}` | {url} | {r['verdict']} | {r['http']} | "
                    f"{fmt_ratio(r['title_ratio'])} | {note} |"
                )
        if not any_row:
            lines.append("| — | — | — | — | — | 全部通过 |")
        lines.append("")

    lines.append("## 三、一致性校验")
    lines.append("")
    if args.only_http:
        lines.append("- 已跳过（--only-http）。")
    elif issues:
        lines.append("| 记录 | 级别 | 字段 | 问题 |")
        lines.append("| --- | --- | --- | --- |")
        for rid, rows in issues.items():
            for r in rows:
                msg = r["msg"].replace("|", "/")
                lines.append(f"| `{rid}` | {r['level']} | `{r['field']}` | {msg} |")
    else:
        lines.append("- 无问题。")
    lines.append("")

    passed = [
        ev.get("id")
        for ev in events
        if ev.get("id")
        and not any(
            r["level"] == "ERROR" for r in issues.get(ev.get("id"), [])
        )
        and not any(
            r["verdict"] == "FAIL" for r in sources_results.get(ev.get("id"), [])
        )
    ]
    lines.append("## 四、未阻断清单（无 ERROR / FAIL）")
    lines.append("")
    lines.append(
        "- " + ("、".join(f"`{x}`" for x in passed) if passed else "（无）")
    )
    lines.append("")

    if args.apply:
        lines.append("## 五、回写结果（--apply）")
        lines.append("")
        lines.append("| 事件 | 原状态 | 新状态 | evidenceGrade | 写盘 |")
        lines.append("| --- | --- | --- | --- | --- |")
        for item in applied or []:
            if item["error"]:
                lines.append(
                    f"| `{item['id']}` | {item['prevStatus'] or '-'} | - | - | "
                    f"失败：{item['error']} |"
                )
                continue
            flag = "已更新" if item["changed"] else "不变"
            lines.append(
                f"| `{item['id']}` | {item['prevStatus'] or '-'} | {item['status']} | "
                f"{item['grade']} | {flag} |"
            )
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------- 候选批次


def load_candidates(path):
    """读原始候选批次：jsonl/ndjson（一行一条）或 json 数组。"""
    p = Path(path)
    raw = p.read_text(encoding="utf-8-sig")
    if p.suffix.lower() in (".jsonl", ".ndjson"):
        objs = []
        for i, line in enumerate(raw.splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            try:
                objs.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"{p}:{i} JSON 解析失败：{exc}") from exc
        return objs
    data = json.loads(raw)
    if isinstance(data, list):
        return data
    for key in ("candidates", "items", "records", "events"):
        if isinstance(data.get(key), list):
            return data[key]
    raise ValueError(f"{p}: 找不到候选数组（list 或 candidates/items/records）")


def candidate_sources(cand):
    out = []
    url = cand.get("url")
    if isinstance(url, str) and url.startswith(("http://", "https://")):
        out.append((url, "primary"))
    for u in cand.get("corroboration") or []:
        if isinstance(u, str) and u.startswith(("http://", "https://")):
            out.append((u, "corroboration"))
    return out


def run_candidates(args, deadline_dt):
    """核验原始候选批次的 URL（标题只作提示，不判错）。"""
    try:
        cands = load_candidates(args.candidates)
    except (OSError, ValueError) as exc:
        print(f"读取候选失败：{exc}", file=sys.stderr)
        return 2
    if not cands:
        print(f"{args.candidates} 为空。", file=sys.stderr)
        return 2

    url_set = []
    for cand in cands:
        for u, _ in candidate_sources(cand):
            if u not in url_set:
                url_set.append(u)

    cache = {}
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=max(1, args.workers)
    ) as pool:
        futures = {
            pool.submit(check_url, u, args.timeout, args.retries): u for u in url_set
        }
        for fut in concurrent.futures.as_completed(futures):
            u = futures[fut]
            try:
                cache[u] = fut.result()
            except Exception as exc:
                cache[u] = {
                    "url": u,
                    "status": None,
                    "final_url": u,
                    "page_title": "",
                    "page_published": "",
                    "error": f"核验异常：{exc}",
                }

    rows = []
    fail_primary = []
    counts = {"PASS": 0, "WARN": 0, "FAIL": 0}
    for cand in cands:
        cid = cand.get("id") or "?"
        title = cand.get("title", "") if isinstance(cand.get("title"), str) else ""
        results = []
        for u, stype in candidate_sources(cand):
            fetched = cache.get(u, {"status": None, "error": "未核验"})
            src = {
                "url": u,
                "title": title,
                "publishedAt": cand.get("occurredAt", ""),
                "type": stype,
            }
            verdict, http, ratio, note = judge_source(
                src, fetched, deadline_dt, title_gate=False
            )
            counts[verdict] += 1
            results.append(
                {
                    "url": u,
                    "type": stype,
                    "verdict": verdict,
                    "http": http,
                    "ratio": ratio,
                    "note": note,
                    "page_title": fetched.get("page_title", ""),
                }
            )
        drop = any(
            r["type"] == "primary" and r["verdict"] == "FAIL" for r in results
        )
        if drop:
            fail_primary.append(cid)
        rows.append(
            {
                "id": cid,
                "title": title,
                "results": results,
                "drop": drop,
                "only_corroboration": not any(
                    r["type"] == "primary" for r in results
                ),
            }
        )

    report_path = Path(args.report)
    if args.report == "reports/source-check.md":
        report_path = Path("reports/source-check-candidates.md")
    report_path.parent.mkdir(parents=True, exist_ok=True)

    lines = []
    lines.append("# ERA 候选来源核验报告（采集批次）")
    lines.append("")
    lines.append(f"- 生成时间：{datetime.now(TZ).strftime('%Y-%m-%d %H:%M:%S %z')}")
    lines.append(
        f"- 输入：`{args.candidates}`（候选 {len(cands)} 条，URL {len(url_set)} 个）"
    )
    lines.append(
        f"- 来源记录结果（同一 URL 跨候选会重复计）：PASS {counts['PASS']} / "
        f"WARN {counts['WARN']} / FAIL {counts['FAIL']}"
    )
    fix_rows = [
        row
        for row in rows
        if not row["drop"] and any(r["verdict"] == "FAIL" for r in row["results"])
    ]
    lines.append(f"- **必须剔除：{len(fail_primary)} 条（primary 死链）**")
    lines.append(f"- **必须修补：{len(fix_rows)} 条（交叉印证里有死链）**")
    lines.append("")
    lines.append("## 一、必须剔除（primary 死链）")
    if fail_primary:
        for row in rows:
            if not row["drop"]:
                continue
            bad = [
                r
                for r in row["results"]
                if r["type"] == "primary" and r["verdict"] == "FAIL"
            ]
            reason = "；".join(f"{r['http']} {r['note']}（{r['url']}）" for r in bad)
            lines.append(f"- `{row['id']}`《{row['title']}》— {reason}")
    else:
        lines.append("- 无。")
    lines.append("")
    lines.append("## 二、必须修补（非 primary 死链，别删条目，换链接或降级）")
    if fix_rows:
        for row in fix_rows:
            bad = [r for r in row["results"] if r["verdict"] == "FAIL"]
            reason = "；".join(
                f"{r['type']} {r['http']} {r['note']}（{r['url']}）" for r in bad
            )
            lines.append(f"- `{row['id']}`《{row['title']}》— {reason}")
    else:
        lines.append("- 无。")
    lines.append("")
    lines.append("## 三、逐条结果")
    lines.append("")
    lines.append("| 候选 | 标题 | primary | 交叉印证 | 结论 |")
    lines.append("| --- | --- | --- | --- | --- |")
    for row in rows:
        prim = [r for r in row["results"] if r["type"] == "primary"]
        corr = [r for r in row["results"] if r["type"] == "corroboration"]
        if prim:
            p = prim[0]
            p_txt = f"{p['verdict']}({p['http']})"
        else:
            p_txt = "缺"
        if corr:
            cp = sum(1 for r in corr if r["verdict"] == "PASS")
            cw = sum(1 for r in corr if r["verdict"] == "WARN")
            cf = sum(1 for r in corr if r["verdict"] == "FAIL")
            c_txt = f"P{cp}/W{cw}/F{cf}"
        else:
            c_txt = "-"
        if row["drop"]:
            verdict = "剔除"
        elif any(r["verdict"] == "FAIL" for r in row["results"]):
            verdict = "修补来源"
        elif any(r["verdict"] == "WARN" for r in row["results"]):
            verdict = "待确认"
        elif row["only_corroboration"]:
            verdict = "缺 primary"
        else:
            verdict = "通过"
        title = row["title"].replace("|", "／")
        lines.append(f"| `{row['id']}` | {title} | {p_txt} | {c_txt} | {verdict} |")
    lines.append("")

    low_title = []
    for row in rows:
        for r in row["results"]:
            if r["verdict"] == "PASS" and r["ratio"] is not None and r["ratio"] < 0.3:
                low_title.append((row["id"], row["title"], r["page_title"], r["ratio"], r["url"]))
    lines.append("## 四、标题相似度提示（<0.3，只供人工看，不判错）")
    lines.append("")
    if low_title:
        lines.append("| 候选 | 候选标题 | 页面标题 | 相似度 | URL |")
        lines.append("| --- | --- | --- | --- | --- |")
        for cid, ctitle, ptitle, ratio, url in low_title:
            lines.append(
                f"| `{cid}` | {ctitle.replace('|', '／')} | "
                f"{ptitle.replace('|', '／')[:60]} | {ratio:.2f} | {url} |"
            )
    else:
        lines.append("- 无。")
    lines.append("")

    report_path.write_text("\n".join(lines), encoding="utf-8")

    if args.json:
        print(
            json.dumps(
                {
                    "generatedAt": datetime.now(TZ).isoformat(),
                    "candidates": len(cands),
                    "urls": len(url_set),
                    "counts": counts,
                    "mustDrop": fail_primary,
                    "mustFix": [r["id"] for r in fix_rows],
                    "rows": [
                        {
                            "id": r["id"],
                            "drop": r["drop"],
                            "results": r["results"],
                        }
                        for r in rows
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(
            f"候选={len(cands)} URL={len(url_set)} "
            f"PASS={counts['PASS']} WARN={counts['WARN']} FAIL={counts['FAIL']} "
            f"| 必须剔除={len(fail_primary)} 必须修补={len(fix_rows)}"
        )
        print(f"报告：{report_path}")
    return 1 if fail_primary else 0


# ---------------------------------------------------------------- 主流程


def main(argv=None):
    ap = argparse.ArgumentParser(description="ERA 来源核验 + 一致性校验")
    ap.add_argument("--content", default="content", help="内容目录（默认 content/）")
    ap.add_argument(
        "--candidates", default="", help="直接核验原始候选批次（.jsonl/.json）"
    )
    ap.add_argument("--report", default="reports/source-check.md", help="报告输出路径")
    ap.add_argument("--deadline", default=DEFAULT_DEADLINE, help="时间戳截止日 YYYY-MM-DD")
    ap.add_argument("--timeout", type=float, default=12.0, help="单请求超时秒")
    ap.add_argument("--workers", type=int, default=8, help="并发数")
    ap.add_argument("--retries", type=int, default=1, help="瞬时失败重试次数")
    ap.add_argument("--offline", action="store_true", help="跳过 HTTP")
    ap.add_argument("--only-http", action="store_true", help="跳过一致性")
    ap.add_argument("--only-consistency", action="store_true", help="同 --offline")
    ap.add_argument(
        "--apply",
        action="store_true",
        help="把核验结果回写 content/ 的 verification/evidenceGrade（默认只读）",
    )
    ap.add_argument("--json", action="store_true", help="把结构化结果打到 stdout")
    ap.add_argument(
        "--json-out", default="", help="另存结构化结果 JSON 的路径"
    )
    args = ap.parse_args(argv)

    if args.apply:
        if args.candidates:
            print("--apply 只支持 --content，不用于候选批次。", file=sys.stderr)
            return 2
        if args.offline or args.only_consistency:
            print(
                "--apply 需要 HTTP 核验，不能配合 --offline/--only-consistency。",
                file=sys.stderr,
            )
            return 2

    deadline_dt = parse_dt(args.deadline + "T23:59:59+08:00")
    if deadline_dt is None:
        print(f"截止日不合法：{args.deadline}", file=sys.stderr)
        return 2

    if args.candidates:
        return run_candidates(args, deadline_dt)

    root = Path(args.content)
    if not root.exists() or not root.is_dir():
        print(f"内容目录不存在：{root}", file=sys.stderr)
        return 2

    events, trends, forecasts = [], [], []
    load_errors = []
    ev_meta = []
    for rel, obj, kind, rec_path, rec_idx in iter_records(root, with_meta=True):
        obj = dict(obj)
        obj.setdefault("_file", rel)
        if kind == "error":
            load_errors.append(obj)
            continue
        if kind == "event":
            events.append(obj)
            ev_meta.append((rec_path, rec_idx))
        elif kind == "trend":
            trends.append(obj)
        elif kind == "forecast":
            forecasts.append(obj)

    if not events and not trends and not forecasts:
        print(f"{root} 下没有可校验的 JSON 记录（等待采集）。", file=sys.stderr)
        return 2

    # HTTP
    sources_results = {}
    ev_rows = []
    counts = {"pass": 0, "warn": 0, "fail": 0, "error": 0, "iwarn": 0, "must_drop": []}
    if not (args.offline or args.only_consistency):
        url_set = []
        for ev in events:
            for src in ev.get("sources") or []:
                if isinstance(src, dict) and isinstance(src.get("url"), str):
                    if src["url"] not in url_set:
                        url_set.append(src["url"])
        cache = {}
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=max(1, args.workers)
        ) as pool:
            futures = {
                pool.submit(check_url, u, args.timeout, args.retries): u for u in url_set
            }
            for fut in concurrent.futures.as_completed(futures):
                u = futures[fut]
                try:
                    cache[u] = fut.result()
                except Exception as exc:  # 兜底，单条失败不影响整体
                    cache[u] = {
                        "url": u,
                        "status": None,
                        "final_url": u,
                        "page_title": "",
                        "page_published": "",
                        "error": f"核验异常：{exc}",
                    }
        for ev in events:
            eid = ev.get("id") or ev.get("_file") or "?"
            rows = []
            for src in ev.get("sources") or []:
                if not isinstance(src, dict):
                    continue
                url = src.get("url")
                if not isinstance(url, str) or not url:
                    continue
                fetched = cache.get(url) or {
                    "url": url,
                    "status": None,
                    "final_url": url,
                    "page_title": "",
                    "page_published": "",
                    "error": "未核验",
                }
                verdict, http, ratio, note = judge_source(src, fetched, deadline_dt)
                rows.append(
                    {
                        "url": url,
                        "type": src.get("type", ""),
                        "verdict": verdict,
                        "http": http,
                        "final_url": fetched.get("final_url", url),
                        "title_ratio": ratio,
                        "note": note,
                    }
                )
                counts[verdict.lower()] += 1
            sources_results[eid] = rows
            ev_rows.append(rows)

    # 一致性
    issues = {} if args.only_http else check_consistency(
        events, trends, forecasts, deadline_dt
    )
    if not args.only_http:
        for err in load_errors:
            issues.setdefault(err.get("_file", "?"), []).append(
                {
                    "level": "ERROR",
                    "field": "file",
                    "msg": f"JSON 解析失败：{err.get('__load_error__', '')}",
                }
            )
    for rows in issues.values():
        for r in rows:
            if r["level"] == "ERROR":
                counts["error"] += 1
            else:
                counts["iwarn"] += 1

    # 必须剔除判定：死链/坏数据优先展示，最多列 5 条
    for ev in events:
        eid = ev.get("id") or ev.get("_file") or "?"
        source_reasons, data_reasons = [], []
        rows = sources_results.get(eid, [])
        if rows:
            fails = [r for r in rows if r["verdict"] == "FAIL"]
            primaries = [r for r in rows if r["type"] == "primary"]
            if fails and all(r["verdict"] == "FAIL" for r in rows):
                source_reasons.append("全部来源核验 FAIL（无可用出处）")
            elif fails and primaries and all(
                r["verdict"] == "FAIL" for r in primaries
            ):
                source_reasons.append("primary 来源全部 FAIL")
        for r in issues.get(eid, []):
            if r["level"] == "ERROR":
                data_reasons.append(f"{r['field']}: {r['msg']}")
        reasons = source_reasons + data_reasons
        if reasons:
            more = len(reasons) - 5
            reason = "；".join(reasons[:5]) + (f"；等 {more} 项" if more > 0 else "")
            counts["must_drop"].append({"id": eid, "reason": reason})

    applied = []
    if args.apply:
        now_iso = datetime.now(TZ).isoformat(timespec="seconds")
        for ev, rows, meta in zip(events, ev_rows, ev_meta):
            rec_path, rec_idx = meta
            if rec_path is None:
                continue
            res = apply_verification(rec_path, rec_idx, ev, rows, now_iso)
            applied.append(res)
            if res["error"]:
                issues.setdefault(res["id"], []).append(
                    {
                        "level": "WARN",
                        "field": "verification",
                        "msg": f"回写失败：{res['error']}",
                    }
                )

    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        build_report(args, events, sources_results, issues, counts, applied),
        encoding="utf-8",
    )

    payload = {
        "generatedAt": datetime.now(TZ).isoformat(),
        "content": str(root),
        "counts": {
            "events": len(events),
            "trends": len(trends),
            "forecasts": len(forecasts),
            "sourcePass": counts["pass"],
            "sourceWarn": counts["warn"],
            "sourceFail": counts["fail"],
            "consistencyError": counts["error"],
            "consistencyWarn": counts["iwarn"],
        },
        "applied": applied,
        "mustDrop": counts["must_drop"],
        "sources": sources_results,
        "issues": issues,
        "loadErrors": load_errors,
    }
    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(
            f"event={len(events)} source PASS={counts['pass']} WARN={counts['warn']} "
            f"FAIL={counts['fail']} | consistency ERROR={counts['error']} "
            f"WARN={counts['iwarn']} | 必须剔除={len(counts['must_drop'])}"
        )
        print(f"报告：{report_path}")
    return 1 if (counts["error"] or counts["must_drop"]) else 0


if __name__ == "__main__":
    sys.exit(main())
