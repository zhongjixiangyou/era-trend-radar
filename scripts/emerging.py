#!/usr/bin/env python3
"""ERA「异军突起」检测：哪些领域在近期突然加速，且加速有真实事件支撑。

只做可解释的计数比较，不做预测、不编排行：
  1. 窗口：以真实采集时刻 asOf 为锚，recent=(asOf-30d, asOf]，prior=(asOf-60d, asOf-30d]；
     另出 7d/7d 短窗。窗口边界写进输出，前端必须显示。
  2. 单位：同一 dedupeKey 只算 1 条；retro=true 的回溯补录**两个窗口都排除**，
     因为它反映整理时间而不是发生时间。
  3. 领域：按事件 trendLinks 归到趋势（一个事件可属多个领域），另按 category 兜底汇总。
  4. 状态判定（缺一不可，宁判 insufficient 不猜）：
     - insufficient：recent<3 或 recent 独立来源域名<2，或窗口内无 prior 数据可比
     - emerging_candidate：prior<3（低基数）且 recent>=3 → 标 lowBase，**不换算成增长率**
     - accelerating：prior>=3 且 recent/prior>=2
     - cooling：prior>=3 且 recent/prior<=0.5
     - stable：其余
  5. 突破信号：delta（变化幅度维度）>=12 的近期事件数，阈值写进输出，不靠关键词猜。
  6. 缺数据一律 null + reason，不补 0、不补估值。

用法：
  python3 scripts/emerging.py --content content --out data/emerging.json
  python3 scripts/emerging.py --content content --json          # 只打印不写盘
退出码：0 正常（含全部 insufficient）；1 输入有硬错误；2 参数/IO 错误。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

MIN_RECENT = 3
MIN_SOURCES = 2
LOW_BASE_PRIOR = 3
ACCEL_RATIO = 2.0
COOL_RATIO = 0.5
SCORE_REVISION_DELTA = 12
WINDOW_DAYS = 30
SHORT_WINDOW_DAYS = 7


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    s = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def domain_of(url: str) -> str | None:
    if not url or "://" not in url:
        return None
    host = (urlsplit(url).hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host or None


def load_events(content: Path) -> list[dict]:
    out = []
    for p in sorted((content / "events").glob("*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SystemExit(f"[emerging] {p} 不是合法 JSON：{exc}")
        if isinstance(d, dict) and d.get("id"):
            d["_file"] = p.name
            out.append(d)
    return out



def _domains_of(event: dict) -> set[str]:
    return {d for d in (domain_of(s.get("url", "")) for s in (event.get("sources") or [])) if d}


def _occurred_at_reliable(event: dict) -> bool | None:
    """发生日是否可靠：可解析、不晚于采集时刻、且至少有一条来源可回溯。"""
    occurred = parse_ts(event.get("occurredAt"))
    collected = parse_ts(event.get("collectedAt"))
    if occurred is None:
        return None
    if collected is not None and occurred > collected:
        return False
    return bool(_domains_of(event))


def _coverage_comparable(event: dict, median_domains: int) -> bool | None:
    """来源覆盖是否可比：用「独立域名数 >= 非回溯事件中位数」作可测代理，不是真值。"""
    domains = _domains_of(event)
    if not domains:
        return None
    return len(domains) >= max(median_domains, 1)


def _hist_reasons(event: dict, rel: bool | None, cov: bool | None, median_domains: int) -> list[str]:
    out = []
    if rel is None:
        out.append("occurredAt 缺失或无法解析")
    elif rel is False:
        out.append("occurredAt 晚于 collectedAt 或没有任何可回溯来源")
    if cov is None:
        out.append("无来源，无法判断覆盖可比性")
    elif cov is False:
        out.append(f"独立域名 {len(_domains_of(event))} < 非回溯中位数 {max(median_domains, 1)}，覆盖不可比，不计算增速")
    if rel is True and cov is True:
        out.append("发生日可靠且覆盖可比：可单列历史回溯观察，仍不计实时增速")
    return out


def dedupe(events: list[dict]) -> list[dict]:
    seen = {}
    for e in sorted(events, key=lambda x: (x.get("collectedAt") or "", x["id"])):
        key = e.get("dedupeKey") or e["id"]
        seen.setdefault(key, e)
    return list(seen.values())


def in_window(events, start, end) -> list[dict]:
    picked = []
    for e in events:
        ts = parse_ts(e.get("occurredAt"))
        if ts and start < ts <= end:
            picked.append(e)
    return picked


def summarize(bucket: list[dict]) -> dict:
    domains = set()
    for e in bucket:
        for s in e.get("sources") or []:
            d = domain_of(s.get("url", ""))
            if d:
                domains.add(d)
    # 评分上调只是"判断被上调"，不是突破；突破必须有带出处的里程碑事实。
    score_rev = [e["id"] for e in bucket if ((e.get("scores") or {}).get("delta") or 0) >= SCORE_REVISION_DELTA]
    breakthroughs = []
    for e in bucket:
        for m in e.get("milestones") or []:
            if isinstance(m, dict) and (m.get("sourceUrl") or m.get("url")):
                breakthroughs.append(e["id"])
                break
    return {
        "events": len(bucket),
        "distinctSources": len(domains),
        "scoreRevisionSignal": len(score_rev),
        "scoreRevisionIds": score_rev,
        "breakthroughEvents": len(breakthroughs),
        "breakthroughIds": breakthroughs,
        "breakthroughAvailable": any("milestones" in e for e in bucket),
        "eventIds": [e["id"] for e in bucket],
    }


def judge(recent: dict, prior: dict) -> tuple[str, float | None, bool, str | None]:
    if recent["events"] < MIN_RECENT:
        return "insufficient", None, False, f"近期事件 {recent['events']} < {MIN_RECENT}"
    if recent["distinctSources"] < MIN_SOURCES:
        return "insufficient", None, False, f"近期独立来源 {recent['distinctSources']} < {MIN_SOURCES}"
    if prior["events"] < LOW_BASE_PRIOR:
        return "emerging_candidate", None, True, "低基数：前窗不足，不换算增长率"
    ratio = recent["events"] / prior["events"]
    if ratio >= ACCEL_RATIO:
        return "accelerating", round(ratio, 2), False, None
    if ratio <= COOL_RATIO:
        return "cooling", round(ratio, 2), False, None
    return "stable", round(ratio, 2), False, None


def load_trend_titles(content: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for f in sorted((content / "trends").glob("*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if d.get("id"):
            out[d["id"]] = d.get("title") or d["id"]
    return out


def load_trend_ids(content: Path) -> list[str]:
    ids = []
    for p in sorted((content / "trends").glob("*.json")):
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if isinstance(d, dict) and d.get("id"):
            ids.append(d["id"])
    return ids


def build(
    events: list[dict],
    trend_ids: list[str] | None = None,
    as_of: datetime | None = None,
    as_of_source: str = "run_time",
    content_dir: Path | None = None,
) -> dict:
    real = dedupe([e for e in events if not (e.get("retro") or {}).get("isRetro")])
    retro = [e for e in events if (e.get("retro") or {}).get("isRetro")]
    _med = sorted(len(_domains_of(e)) for e in real)
    median_domains = _med[len(_med) // 2] if _med else 1
    if as_of is None:
        stamps = [parse_ts(e.get("collectedAt")) for e in real]
        stamps = [t for t in stamps if t]
        if not stamps:
            raise SystemExit("[emerging] 没有可用的 collectedAt，无法回放")
        as_of = max(stamps)
        as_of_source = "max(collectedAt)"
    as_of = as_of if as_of.tzinfo else as_of.replace(tzinfo=timezone.utc)
    r0 = as_of - timedelta(days=WINDOW_DAYS)
    p0 = as_of - timedelta(days=WINDOW_DAYS * 2)
    r7 = as_of - timedelta(days=SHORT_WINDOW_DAYS)
    p7 = as_of - timedelta(days=SHORT_WINDOW_DAYS * 2)

    by_trend: dict[str, list[dict]] = defaultdict(list)
    for tid in trend_ids or []:
        by_trend[tid] = []
    for e in real:
        for link in e.get("trendLinks") or []:
            tid = link.get("trendId")
            if tid:
                by_trend[tid].append(e)
    by_cat: dict[str, list[dict]] = defaultdict(list)
    for e in real:
        if e.get("category"):
            by_cat[e["category"]].append(e)

    trend_titles = load_trend_titles(content_dir) if content_dir else {}

    def rows(groups, kind):
        out = []
        for name, bucket in sorted(groups.items()):
            recent = summarize(in_window(bucket, r0, as_of))
            prior = summarize(in_window(bucket, p0, r0))
            short_recent = summarize(in_window(bucket, r7, as_of))
            short_prior = summarize(in_window(bucket, p7, r7))
            state, ratio, low_base, reason = judge(recent, prior)
            out.append(
                {
                    "id": name,
                    "title": trend_titles.get(name) or name,
                    "kind": kind,
                    "state": state,
                    "growthRatio": ratio,
                    "lowBase": low_base,
                    "reason": reason,
                    "recent": recent,
                    "prior": prior,
                    "shortRecent": short_recent,
                    "shortPrior": short_prior,
                }
            )
        order = {"accelerating": 0, "emerging_candidate": 1, "cooling": 2, "stable": 3, "insufficient": 4}
        out.sort(key=lambda r: (order.get(r["state"], 9), -(r["recent"]["events"]), r["id"]))
        return out

    return {
        "schemaVersion": "1",
        "asOf": as_of.isoformat(),
        "windows": {
            "recent": {"from": r0.isoformat(), "to": as_of.isoformat(), "days": WINDOW_DAYS},
            "prior": {"from": p0.isoformat(), "to": r0.isoformat(), "days": WINDOW_DAYS},
            "shortRecent": {"from": r7.isoformat(), "to": as_of.isoformat(), "days": SHORT_WINDOW_DAYS},
            "shortPrior": {"from": p7.isoformat(), "to": r7.isoformat(), "days": SHORT_WINDOW_DAYS},
        },
        "rules": {
            "minRecentEvents": MIN_RECENT,
            "minDistinctSources": MIN_SOURCES,
            "lowBasePrior": LOW_BASE_PRIOR,
            "acceleratingRatio": ACCEL_RATIO,
            "coolingRatio": COOL_RATIO,
            "scoreRevisionDelta": SCORE_REVISION_DELTA,
            "dedupe": "dedupeKey",
            "retroExcluded": True,
            "ratioOnLowBase": "not_computed",
        },
        "excluded": {
            "retroEvents": len(retro),
            "retroIds": [e["id"] for e in retro],
            "duplicateEvents": len(events) - len(real) - len(retro),
        },
        "totals": {
            "events": len(real),
            "asOfSource": as_of_source,
            "breakthroughAvailable": any("milestones" in e for e in events),
            "breakthroughNote": "突破判定需 milestones[] 带出处；schema 当前无此字段，故所有突破计数为 0 且不可展示。",
            "scoreRevisionSignalNote": "scoreRevisionSignal 仅表示评分被上调，不得当技术/成本/采用突破。",
        },
        "historical": {
            "note": "回溯补录：不参与实时增速。occurredAtReliable 与 coverageComparable 都成立才进「历史回溯观察」；否则只作历史事实展示。",
            "count": len(retro),
            "medianDomains": median_domains,
            "items": [
                {
                    "id": e["id"],
                    "occurredAt": e.get("occurredAt"),
                    "title": e.get("title"),
                    "sourceDomains": _domains_of(e) and len(_domains_of(e)),
                    "occurredAtReliable": rel,
                    "coverageComparable": cov,
                    "eligibleForHistoricalObservation": bool(rel is True and cov is True),
                    "growthComputed": False,
                    "reasons": _hist_reasons(e, rel, cov, median_domains),
                }
                for e in sorted(retro, key=lambda x: x.get("occurredAt") or "")
                for rel, cov in [(_occurred_at_reliable(e), _coverage_comparable(e, median_domains))]
            ],
        },
        "limitations": [
            "来源配置变更（新增 RSS/API）会让计数上升，事件数据本身无法区分；需人工核对采集报告里的首抓日期。",
            "asOf 生产取本轮显式 UTC 运行时间；max(collectedAt) 只用于历史回放/fixture，否则停采后数据永远显得新。",
            "scoreRevisionSignal 只表示评分被上调，不等于技术/成本/采用突破；突破需 milestones[] 带出处，当前 schema 无此字段故 breakthroughAvailable=false。",
            "事件数少时全部判 insufficient 是预期行为，不生成排行。",
        ],
        "domains": rows(by_trend, "trend"),
        "categories": rows(by_cat, "category"),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="ERA 异军突起检测（窗口计数，不预测）")
    ap.add_argument("--content", default="content")
    ap.add_argument("--out", default="data/emerging.json")
    ap.add_argument("--json", action="store_true", help="只打印，不写盘")
    ap.add_argument("--as-of", default=None, help="显式锚点时间（ISO8601，生产用本轮 UTC 运行时间）")
    ap.add_argument(
        "--as-of-from-data",
        action="store_true",
        help="用 max(collectedAt) 作锚点：仅历史回放/fixture，生产禁用",
    )
    args = ap.parse_args(argv)

    root = Path(args.content)
    if not (root / "events").is_dir():
        print(f"[emerging] 找不到 {root}/events", file=sys.stderr)
        return 2
    as_of = None
    source = "run_time"
    if args.as_of:
        as_of = parse_ts(args.as_of)
        if as_of is None:
            print(f"[emerging] --as-of 无法解析：{args.as_of}", file=sys.stderr)
            return 2
        source = "explicit"
    elif args.as_of_from_data:
        source = "max(collectedAt)"
    else:
        as_of = datetime.now(timezone.utc)
    payload = build(load_events(root), load_trend_ids(root), as_of, source, root)
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if args.json:
        print(text)
        return 0
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    states = [d["state"] for d in payload["domains"]]
    print(
        f"[emerging] asOf={payload['asOf'][:19]}({payload['totals']['asOfSource']}) · 领域 {len(states)} · "
        f"accelerating {states.count('accelerating')} / candidate {states.count('emerging_candidate')} / "
        f"insufficient {states.count('insufficient')} → {out}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
