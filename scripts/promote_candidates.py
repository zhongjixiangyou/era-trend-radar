#!/usr/bin/env python3
"""把 collect.py 采到的候选（inbox/imported/events/*.jsonl）提升为 content/events/*.json。

只做三件事：去重、按规则评分、核验来源可达且标题一致。
不做事实核验——analysis 里写清楚这一点，页面按证据等级展示。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _core as C  # noqa: E402 —— 窗口口径与校验器共用一份（docs/window-rolling.md）

ROOT = Path(__file__).resolve().parent.parent
EVENTS = ROOT / "content" / "events"
INBOX = ROOT / "inbox" / "imported" / "events"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"

SIGNAL_WORDS = [
    "加息", "降息", "通胀", "CPI", "GDP", "PMI", "出口", "进口", "失业", "利率", "央行",
    "芯片", "半导体", "AI", "人工智能", "机器人", "算力", "产能", "并购", "关税", "制裁",
    "选举", "停火", "战争", "能源", "油价", "房价", "债务", "评级",
]
DELTA_WORDS = ["首次", "新高", "新低", "突破", "超预期", "低于预期", "上调", "下调", "创纪录", "record", "surge", "plunge", "first", "highest", "lowest"]

CATEGORY_MAP = {"中国": "中国经济与产业", "科技": "科技与社会", "地缘政治": "全球宏观", "市场": "全球宏观"}

TREND_RULES = [
    ("tr_global-rates-rehike", ["加息", "利率", "美联储", "Fed", "rate hike", "central bank", "ECB", "BOJ"]),
    ("tr_ai-power-constraint", ["AI", "算力", "数据中心", "data center", "GPU", "电力", "power"]),
    ("tr_ai-platform-expansion", ["OpenAI", "模型", "大模型", "AI model", "Anthropic", "Google AI"]),
    ("tr_cn-industrial-margin-repair", ["工业利润", "利润", "PPI", "margin"]),
    ("tr_cn-demand-rebalancing", ["消费", "社零", "零售", "retail", "consumption"]),
    ("tr_cn-realestate-contraction", ["房地产", "地产", "房价", "property", "housing"]),
    ("tr_cn-nev-demand-shift", ["新能源", "电动车", "EV", "battery", "电池"]),
    ("tr_geopolitics-energy-premium", ["制裁", "战争", "停火", "地缘", "sanctions", "war", "Middle East"]),
    ("tr_global-growth-frictions", ["衰退", "recession", "增长放缓", "slowdown", "IMF", "World Bank"]),
    ("tr_climate-food-inflation", ["气候", "粮食", "化肥", "climate", "food", "fertilizer"]),
    ("tr_space-observation-network", ["卫星", "航天", "space", "NASA", "satellite"]),
    ("tr_cn-policy-power-system", ["电力", "电网", "power grid", "电价"]),
    ("tr_advanced-energy-validation", ["核能", "聚变", "fusion", "nuclear", "氢能"]),
    ("tr_labour-demographic-adjustment", ["就业", "失业", "人口", "labor", "jobs", "demographic"]),
    ("tr_health-translation", ["医疗", "药物", "clinical", "FDA", "health"]),
    ("tr_science-manufacturing-transfer", ["制造", "供应链", "supply chain", "factory"]),
    ("tr_cn-demographic-decline", ["出生率", "老龄化", "人口"]),
]


def now_iso() -> str:
    """本轮「现在」的 ISO 串；窗口存在时钳到 asOf（同 collect.now，理由见 docs/window-rolling.md §2）。"""
    dtv = datetime.now(timezone.utc).astimezone()
    win = C.active_window()
    limit = None
    if win is not None:
        limit = win.as_of
        # fixed 模式下 asOf 可以晚于 upper，钳制目标是「数据上限」，不是 asOf。
        if win.upper is not None and (limit is None or win.upper < limit):
            limit = win.upper
    if limit is not None and dtv > limit:
        dtv = limit
    return dtv.isoformat(timespec="seconds")


def to_iso(value: str) -> str:
    if not value:
        return ""
    v = value.strip()
    try:
        dt = parsedate_to_datetime(v)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone().isoformat(timespec="seconds")
    except Exception:
        pass
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone().isoformat(timespec="seconds")
    except Exception:
        return ""


def norm(s: str) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", (s or "").lower())


def title_probe(title: str) -> str:
    t = norm(title)
    return t[:12] if len(t) >= 12 else t[:6]


def verify(url: str, title: str, timeout: int = 8) -> tuple[bool, str]:
    if not url.startswith("http"):
        return False, "无可用链接"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,*/*"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read(400_000).decode("utf-8", "ignore")
        probe = title_probe(title)
        if probe and norm(probe) in norm(raw):
            return True, f"原文可达，标题片段「{probe}」在页面中出现"
        return False, f"原文可达但未匹配到标题片段「{probe}」"
    except Exception as e:  # noqa: BLE001
        return False, f"取原文失败：{type(e).__name__}"


def days_old(iso: str) -> float:
    try:
        dt = datetime.fromisoformat(iso)
        return max(0.0, (datetime.now(dt.tzinfo) - dt).total_seconds() / 86400)
    except Exception:
        return 30.0


def score(item: dict) -> dict:
    text = f"{item.get('title','')} {item.get('summary','')}"
    age = days_old(item.get("occurredAt") or now_iso())
    cat = item.get("category", "")
    hits = [w for w in SIGNAL_WORDS if w.lower() in text.lower()]
    delta_hits = [w for w in DELTA_WORDS if w.lower() in text.lower()]

    structural = 18 if cat in ("全球宏观", "中国", "市场") else 15 if cat == "科技" else 12
    scope = 16 if item.get("region") in ("全球", "中国") else 12
    signal = min(20, 8 + 3 * len(hits))
    delta = min(15, 5 + 4 * len(delta_hits))
    timeliness = 10 if age <= 1 else 8 if age <= 3 else 6 if age <= 7 else 4 if age <= 30 else 2
    cross = min(10, 3 + len(hits) // 2)
    total = structural + scope + signal + delta + timeliness + cross
    return {
        "structural": structural,
        "scope": scope,
        "signal": signal,
        "delta": delta,
        "timeliness": timeliness,
        "crossDomain": cross,
        "total": total,
        "rationale": {
            "structural": f"{cat}类事件，按类别基线给分",
            "scope": f"影响范围按地区口径（{item.get('region','全球')}）",
            "signal": f"命中 {len(hits)} 个信号词" + (f"：{'、'.join(hits[:4])}" if hits else ""),
            "delta": f"命中 {len(delta_hits)} 个变化词" + (f"：{'、'.join(delta_hits[:3])}" if delta_hits else ""),
            "timeliness": f"距采集约 {age:.0f} 天",
            "crossDomain": "关键词跨类别命中" if len(hits) >= 4 else "单一类别为主",
        },
        "scorerVersion": "score/v1.0",
    }


def trend_links(item: dict) -> list[dict]:
    text = f"{item.get('title','')} {item.get('summary','')}".lower()
    out = []
    for tid, words in TREND_RULES:
        hit = [w for w in words if w.lower() in text]
        if hit:
            out.append({"trendId": tid, "direction": "support", "reason": f"关键词命中：{'、'.join(hit[:3])}（规则关联，非人工判定）"})
        if len(out) >= 2:
            break
    return out


def make_event(item: dict, ok: bool, why: str) -> dict:
    raw_title = (item.get("title") or "").strip()
    # 采集端先按契约上限截断：长英文标题（如美联储公告）不截就会让 CI 的 validate 判 F_LEN、整轮 collect 作废。
    title = C.clip(raw_title, C.TITLE_MAX)
    summary = C.clip((item.get("summary") or "").strip(), C.SUMMARY_MAX)
    if C.char_len(summary) < 10:  # summary 契约下限 10 字，不足时退回标题
        summary = title
    url = (item.get("url") or (item.get("sources") or [{}])[0].get("url") or "").strip()
    occurred = to_iso(item.get("occurredAt") or item.get("firstReportedAt") or "") or now_iso()
    publisher = (item.get("sources") or [{}])[0].get("publisher") or item.get("_sourceId") or "媒体"
    eid = "ev_" + hashlib.sha1((item.get("dedupeKey") or title).encode()).hexdigest()[:12]
    s = score(item)
    return {
        "id": eid,
        "dedupeKey": item.get("dedupeKey") or ("ev:" + hashlib.sha256(url.encode()).hexdigest()[:12]),
        "title": title,
        "summary": summary or title,
        "facts": [summary] if summary else [title],
        "analysis": f"{item.get('category','全球宏观')}口径的单源报道。本条只完成来源可达与标题一致核验，未逐字核对正文，也没有第二来源印证；把它当作线索，不当作定论。",
        "category": CATEGORY_MAP.get(item.get("category"), item.get("category") or "全球宏观"),
        "region": item.get("region") or "全球",
        "occurredAt": occurred,
        "firstReportedAt": occurred,
        "collectedAt": now_iso(),
        "sources": [{
            "url": url,
            "title": C.clip(raw_title, 200),  # 来源标题上限更宽，保留完整原题
            "publisher": publisher,
            "publishedAt": occurred,
            "type": "primary",
        }],
        "scores": s,
        "credibility": {
            "level": "partial",
            "basis": "媒体自有 RSS 原文，链接可达且标题一致；未逐字核对正文，未获独立来源印证",
        },
        "evidenceGrade": "single_primary",
        "verification": {
            "status": "passed" if ok else "pending",
            "method": "rss+http/title-match",
            "checkedAt": now_iso(),
            "note": why[:280],
            "checks": [{"kind": "source_text", "result": "pass" if ok else "unknown", "evidence": why[:280]}],
        },
        "status": "published" if ok else "pending",
        "trendLinks": trend_links(item),
        "revisions": [],
        "forecastRefs": [],
        "retro": {"isRetro": False},
        "scoreHistory": [{
            "at": occurred,
            "by": "rule/v1",
            "note": "规则评分初值（未人工复核）",
            "scores": {k: s[k] for k in ("structural", "scope", "signal", "delta", "timeliness", "crossDomain", "total")},
        }],
        "linkHistory": [],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--workers", type=int, default=10)
    ap.add_argument("--as-of", dest="as_of", default=None,
                    help="窗口基准时刻（默认 ERA_AS_OF，再退 --max-ts / pipeline.config.json / 时钟）")
    ap.add_argument("--max-ts", dest="max_ts", default=None, help="数据时间上限（等价 as-of）")
    ap.add_argument("--config", default=None, help="pipeline.config.json 路径（默认仓库根）")
    a = ap.parse_args()

    win = C.apply_window(C.load_pipeline_config(a.config), as_of=a.as_of, max_ts=a.max_ts)

    existing = {}
    for p in EVENTS.glob("*.json"):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            existing[d.get("dedupeKey", "")] = p.name
        except Exception:
            continue

    candidates, seen, skipped = [], set(), 0
    for f in sorted(INBOX.glob("*.jsonl")):
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except Exception:
                continue
            key = item.get("dedupeKey", "")
            if not key or key in seen or key in existing:
                continue
            if len((item.get("title") or "").strip()) < 8:
                continue
            # 窗口外的候选跳过但不中断整批：计数进 skippedOutOfWindow。
            occurred = to_iso(item.get("occurredAt") or item.get("firstReportedAt") or "")
            if occurred and win.judge(occurred) in (C.JUDGE_OLD, C.JUDGE_FUTURE):
                skipped += 1
                continue
            seen.add(key)
            candidates.append(item)

    if a.limit:
        candidates = candidates[: a.limit]

    def job(it):
        url = (it.get("url") or (it.get("sources") or [{}])[0].get("url") or "")
        return verify(url, it.get("title", ""))

    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        results = list(ex.map(job, candidates))

    written = 0
    passed = 0
    for it, (ok, why) in zip(candidates, results):
        ev = make_event(it, ok, why)
        passed += 1 if ok else 0
        if not a.dry_run:
            (EVENTS / f"{ev['id']}.json").write_text(json.dumps(ev, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        written += 1

    print(json.dumps({
        "candidates": len(candidates),
        "written": written,
        "verified": passed,
        "pending": written - passed,
        "skippedOutOfWindow": skipped,
        "window": win.as_report(),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
