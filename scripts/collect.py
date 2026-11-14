#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ERA 采集入口：从 RSS/JSON 源抓候选事件（真实联网，非导入）。

用法:
  python3 scripts/collect.py --sources config/sources.json --out inbox
  python3 scripts/collect.py --sources config/sources.json --fixture-dir fixtures/collect
  python3 scripts/collect.py --help

退出码:
  0 = healthy（有合格新增）或 no_change（抓取成功但 0 条新增）
  1 = degraded（部分来源失败）
  2 = 用法错误
  3 = failed（全部来源失败，SOURCE_FAILED，不产新发布数据）

输出:
  inbox/candidates-<runId>.jsonl   status=candidate
  reports/collect-<runId>.json     {status, sources:{ok,failed,new}, reason}
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _core as C  # noqa: E402 —— 窗口口径与校验器共用一份（docs/window-rolling.md）
from email.utils import parsedate_to_datetime  # noqa: E402

TZ = timezone(timedelta(hours=8))
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")
EXIT_OK, EXIT_DEGRADED, EXIT_USAGE, EXIT_FAILED = 0, 1, 2, 3
MAX_ITEMS = 40


def now() -> datetime:
    """本轮「现在」。

    asOf 在采集前就被钉死（CI 第一步跑 window.mjs），采集本身发生在 asOf 之后几秒；
    若这里返回墙钟，无发布时间条目会把 occurredAt 写成「未来」，整库被 TS_FUTURE 判负
    （run 43559563839）。所以窗口存在时，把 now 钳到 asOf。
    """
    dtv = datetime.now(TZ)
    win = C.active_window()
    if win is not None and win.as_of is not None and dtv > win.as_of:
        return win.as_of
    return dtv


def when(text: str) -> datetime | None:
    """把源里的时间归一成带时区 datetime；认 ISO 8601 与 RSS 的 RFC 822。"""
    text = (text or "").strip()
    if not text:
        return None
    parsed = C.parse_ts(text)
    if parsed is not None:
        return C.as_aware(parsed)
    try:
        dtv = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if dtv is None:
        return None
    return dtv if dtv.tzinfo else dtv.replace(tzinfo=timezone.utc)


def strip_tags(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s or "")
    return re.sub(r"\s+", " ", s).strip()


def fetch(url: str, timeout: int = 20) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def parse_rss(raw: bytes) -> list[dict]:
    root = ET.fromstring(raw)
    items = []
    for it in root.iter():
        tag = it.tag.split("}")[-1]
        if tag not in ("item", "entry"):
            continue
        d = {}
        for ch in it:
            name = ch.tag.split("}")[-1]
            if name in ("title", "link", "pubDate", "published", "updated", "description", "summary"):
                val = (ch.text or ch.attrib.get("href") or "").strip()
                d.setdefault(name, val)
        link = d.get("link", "")
        title = strip_tags(d.get("title", ""))
        if title:
            items.append({"title": title, "url": link,
                          "publishedAt": d.get("pubDate") or d.get("published") or d.get("updated", ""),
                          "summary": strip_tags(d.get("description") or d.get("summary", ""))[:400]})
    return items[:MAX_ITEMS]


def parse_json(raw: bytes) -> list[dict]:
    d = json.loads(raw)
    arr = d.get("items") or d.get("data") or (d if isinstance(d, list) else [])
    out = []
    for x in arr[:MAX_ITEMS]:
        if isinstance(x, dict) and x.get("title"):
            out.append({"title": strip_tags(x["title"]), "url": x.get("url") or x.get("link", ""),
                        "publishedAt": x.get("publishedAt") or x.get("date", ""),
                        "summary": strip_tags(x.get("summary") or x.get("description", ""))[:400]})
    return out


def ckey(url: str, title: str) -> str:
    return "ev:" + hashlib.sha256((url or title).encode("utf-8")).hexdigest()[:12]


def main() -> int:
    ap = argparse.ArgumentParser(prog="collect.py", description="ERA 采集入口（RSS/JSON → 候选）")
    ap.add_argument("--sources", default="config/sources.json")
    ap.add_argument("--out", default="inbox")
    ap.add_argument("--fixture-dir", default=None, help="离线夹具目录：按 <source-id>.xml|.json 取本地文件")
    ap.add_argument("--timeout", type=int, default=20)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--as-of", dest="as_of", default=None,
                    help="窗口基准时刻（默认 ERA_AS_OF，再退 --max-ts / pipeline.config.json / 时钟）")
    ap.add_argument("--max-ts", dest="max_ts", default=None, help="数据时间上限（等价 as-of）")
    ap.add_argument("--config", default=None, help="pipeline.config.json 路径（默认仓库根）")
    a = ap.parse_args()

    win = C.apply_window(C.load_pipeline_config(a.config), as_of=a.as_of, max_ts=a.max_ts)

    sp = Path(a.sources)
    if not sp.is_file():
        print(f"[collect] 来源配置不存在: {sp}", file=sys.stderr)
        return EXIT_USAGE
    cfg = json.loads(sp.read_text(encoding="utf-8"))
    sources = [s for s in cfg.get("sources", []) if s.get("enabled", True)]
    if not sources:
        print("[collect] 没有启用的来源", file=sys.stderr)
        return EXIT_USAGE

    run = now()
    run_id = run.strftime("%Y%m%dT%H%M%S")
    outdir = Path(a.out)
    outdir.mkdir(parents=True, exist_ok=True)
    report_path = Path("reports") / f"collect-{run_id}.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)

    seen, new, ok, bad, per = set(), [], 0, 0, {}
    out_of_window = {"old": 0, "future": 0}
    for s in sources:
        sid = s.get("id", "?")
        try:
            if a.fixture_dir:
                fx = Path(a.fixture_dir) / f"{sid}.{'xml' if s.get('type') == 'rss' else 'json'}"
                if not fx.is_file():
                    raise FileNotFoundError(f"fixture 缺失 {fx}")
                raw = fx.read_bytes()
            else:
                raw = fetch(s["url"], a.timeout)
            items = parse_rss(raw) if s.get("type") == "rss" else parse_json(raw)
            n = 0
            for it in items:
                k = ckey(it.get("url", ""), it["title"])
                if k in seen:
                    continue
                # 窗口外条目丢弃但不中断：越界数进报告，不写进候选文件。
                published = when(it.get("publishedAt", ""))
                if published is not None:
                    verdict = win.judge(published)
                    if verdict in (C.JUDGE_OLD, C.JUDGE_FUTURE):
                        out_of_window[verdict] += 1
                        continue
                seen.add(k)
                new.append({"id": k, "dedupeKey": k, "title": it["title"], "summary": it["summary"][:120],
                            "facts": [it["summary"][:200]] if it["summary"] else [],
                            "category": s.get("category", "全球宏观"), "region": s.get("region", "全球"),
                            "occurredAt": it.get("publishedAt", ""), "firstReportedAt": it.get("publishedAt", ""),
                            "collectedAt": run.isoformat(timespec="seconds"),
                            "sources": [{"url": it.get("url", ""), "title": it["title"],
                                         "publisher": s.get("name", sid), "publishedAt": it.get("publishedAt", ""),
                                         "type": "primary"}],
                            "status": "candidate", "trendLinks": [], "revisions": [], "forecastRefs": [],
                            "retro": {"isRetro": False}, "_sourceId": sid})
                n += 1
            ok += 1
            per[sid] = {"ok": True, "items": len(items), "new": n}
        except Exception as e:  # noqa: BLE001 —— 采集层任何异常都记为该源失败
            bad += 1
            per[sid] = {"ok": False, "error": f"{type(e).__name__}: {e}"[:200]}
            print(f"[collect] 来源失败 {sid}: {type(e).__name__}", file=sys.stderr)

    if bad == len(sources):
        status, code = "failed", EXIT_FAILED
        reason = "all_sources_failed"
    elif bad:
        status, code = "degraded", EXIT_DEGRADED
        reason = "partial_sources_failed"
    elif new:
        status, code = "healthy", EXIT_OK
        reason = "new_items"
    else:
        status, code = "no_change", EXIT_OK
        reason = "no_new_items"

    if code == EXIT_FAILED:
        # 全源失败：不写候选文件，只写报告
        rep = {"status": status, "reason": reason, "runId": run_id, "at": run.isoformat(timespec="seconds"),
               "window": win.as_report(), "sources": per, "newItems": 0, "outOfWindow": out_of_window,
               "published": False}
        report_path.write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
        print(json.dumps(rep, ensure_ascii=False, indent=1) if a.json else f"[collect] failed：全部来源失败，未写候选")
        return code

    if new:
        cp = outdir / f"candidates-{run_id}.jsonl"
        with cp.open("w", encoding="utf-8") as f:
            for x in new:
                f.write(json.dumps(x, ensure_ascii=False) + "\n")
    rep = {"status": status, "reason": reason, "runId": run_id, "at": run.isoformat(timespec="seconds"),
           "window": win.as_report(), "sources": per, "newItems": len(new),
           "outOfWindow": out_of_window, "published": False}
    report_path.write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(rep, ensure_ascii=False, indent=1) if a.json
          else f"[collect] {status}：{len(new)} 条候选，{bad}/{len(sources)} 源失败")
    return code


if __name__ == "__main__":
    sys.exit(main())
