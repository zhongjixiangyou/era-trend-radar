#!/usr/bin/env python3
"""把采集端的候选 JSONL 转成 era-schema/v1 的 event 骨架。

采集端格式（research/candidates-*.jsonl，一行一条）：
  {id, occurredAt, category, region, title, summary, facts, url, publisher,
   corroboration[], status="candidate"}

转换只做确定性的字段搬运，不编造判断：
  - 事实拆成 facts[]；来源拆成 primary + corroboration
  - scores 全部 0 分、rationale 写「待评分」，等评稿人填（total 由 score.py 重算）
  - credibility=unverified、status=pending：没核验过的条目进不了首页
  - retro.isRetro=true：这批事件都是事后补录，必须留 asOf + note
缺失/降级的字段在末尾打印清单，交给采集端补。

用法：
  python3 scripts/import_candidates.py research/candidates-2026H2.jsonl \
      --out content/_intake/events [--collected-at ISO] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _core as C  # noqa: E402

PLACEHOLDER_ANALYSIS = "（待补：本条由候选批次导入，尚未撰写分析；facts 为采集原始要点，未做二次改写）"
PLACEHOLDER_CRED = "仅采集到原始链接，尚未逐条核验来源真实性与关键数字（候选导入占位）"
PLACEHOLDER_RATIONALE = {
    "structural": "待评分：导入占位，未评估结构性",
    "scope": "待评分：导入占位，未评估影响范围",
    "signal": "待评分：导入占位，未评估信号强度",
    "delta": "待评分：导入占位，未评估变化幅度",
    "timeliness": "待评分：导入占位，未评估时效性",
    "crossDomain": "待评分：导入占位，未评估跨域外溢",
}
MAX_FACT = 240


def split_facts(raw: str) -> list[str]:
    """把 '；' 分隔的事实串拆成数组，过短的并回上一条，过长的按逗号再切。"""
    if not raw:
        return []
    pieces = [p.strip(" 。.") for p in re.split(r"[；;\n]+", raw) if p.strip(" 。.")]
    out: list[str] = []
    for p in pieces:
        while len(p) > MAX_FACT:
            cut = p.rfind("，", 0, MAX_FACT)
            if cut < 40:
                cut = MAX_FACT
            out.append(p[:cut])
            p = p[cut:].lstrip("，, ")
        if len(p) < 4 and out:
            out[-1] = f"{out[-1]}；{p}"[:MAX_FACT]
        elif p:
            out.append(p)
    return out or [raw[:MAX_FACT]]


def host_of(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        host = ""
    return host[4:] if host.startswith("www.") else host


def to_event(cand: dict, collected_at: str, source_file: str) -> dict:
    cid = str(cand.get("id") or "").strip()
    title = str(cand.get("title") or "").strip()
    occurred = str(cand.get("occurredAt") or "").strip()
    region = str(cand.get("region") or "").strip()
    facts = split_facts(str(cand.get("facts") or ""))

    sources = []
    primary_url = str(cand.get("url") or "").strip()
    if primary_url:
        sources.append({
            "url": primary_url,
            "title": title[:200],
            "publisher": str(cand.get("publisher") or host_of(primary_url))[:80],
            "publishedAt": occurred or collected_at,
            "type": "primary",
            "note": "采集端记录的一手来源",
        })
    for url in cand.get("corroboration") or []:
        url = str(url).strip()
        if not url or url == primary_url:
            continue
        host = host_of(url) or "交叉印证来源"
        sources.append({
            "url": url,
            "title": f"{host} 报道",
            "publisher": host[:80],
            "publishedAt": occurred or collected_at,
            "type": "corroboration",
        })

    return {
        "id": f"ev_{cid}",
        "dedupeKey": C.make_dedupe_key(title, region, occurred),
        "title": title,
        "summary": str(cand.get("summary") or "").strip(),
        "facts": facts,
        "analysis": PLACEHOLDER_ANALYSIS,
        "category": str(cand.get("category") or "").strip(),
        "region": region,
        "occurredAt": occurred,
        "firstReportedAt": occurred,          # 近似：采集端未记首报时间，取事件日期
        "collectedAt": collected_at,
        "sources": sources,
        "scores": {
            **{d: 0 for d in C.DIMS},
            "total": 0,
            "rationale": dict(PLACEHOLDER_RATIONALE),
            "scorerVersion": "score/v1.0",
        },
        "credibility": {"level": "unverified", "basis": PLACEHOLDER_CRED},
        # v2：证据等级与核验状态先置空档，核验脚本通过后再改 single_primary/multi_independent
        "evidenceGrade": "unverified",
        "verification": {"status": "not_started", "checks": []},
        "scoreHistory": [],
        "linkHistory": [],
        "status": "pending",
        "trendLinks": [],
        "revisions": [{
            "at": collected_at,
            "by": "import_candidates.py",
            "note": f"从 {source_file} 导入候选 {cid}；firstReportedAt 取事件日期近似，待采集端补首报时间",
        }],
        "forecastRefs": [],
        "retro": {
            "isRetro": True,
            "asOf": occurred or collected_at,
            "note": "候选批次事后补录；当时尚未逐条核验，勿当作当时已发布",
        },
    }


def gaps(ev: dict) -> list[str]:
    out = []
    if len(ev["summary"]) < 10:
        out.append("summary 过短")
    if ev["scores"]["total"] == 0:
        out.append("未评分")
    if ev["status"] == "pending":
        out.append("待发布")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="候选 JSONL → event 骨架")
    ap.add_argument("src", help="candidates-*.jsonl")
    ap.add_argument("--out", required=True, help="输出目录（建议 content/_intake/events）")
    ap.add_argument("--collected-at", default=None, help="采集时间，默认当前时间 +08:00")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    src = Path(a.src)
    if not src.is_file():
        print(f"[E_INPUT] 找不到输入文件：{src}", file=sys.stderr)
        return C.EXIT_USAGE

    collected_at = a.collected_at or datetime.now().astimezone().replace(microsecond=0).isoformat()
    out_dir = Path(a.out)
    if not a.dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)

    rows, bad = [], 0
    for lineno, line in enumerate(src.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            bad += 1
            print(f"[E_JSON] 第 {lineno} 行解析失败：{exc}", file=sys.stderr)

    written, seen, res = 0, {}, []
    for cand in rows:
        ev = to_event(cand, collected_at, src.name)
        cid = ev["id"]
        if cid in seen:
            print(f"[W_DUP_ID] {cid} 重复，跳过第 {seen[cid]} 行之后的重复项", file=sys.stderr)
            continue
        seen[cid] = written
        if not a.dry_run:
            (out_dir / f"{cid}.json").write_text(
                json.dumps(ev, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        written += 1
        res.append({"id": cid, "title": ev["title"], "facts": len(ev["facts"]),
                    "sources": len(ev["sources"]), "gaps": gaps(ev)})

    stats = {
        "input": str(src), "output": None if a.dry_run else str(out_dir),
        "read": len(rows), "json_errors": bad, "written": written,
        "by_category": {},
    }
    # 分类计数
    cats: dict[str, int] = {}
    for cand in rows:
        cats[str(cand.get("category"))] = cats.get(str(cand.get("category")), 0) + 1
    stats["by_category"] = cats

    if a.json:
        print(json.dumps({"stats": stats, "events": res}, ensure_ascii=False, indent=2))
    else:
        print(f"导入 {src.name} → {a.out if not a.dry_run else '（dry-run）'}")
        print(f"读取 {len(rows)} 条，解析失败 {bad} 条，写出 {written} 条")
        print("分类：" + "，".join(f"{k} {v}" for k, v in cats.items()))
        print("统一状态：pending / credibility=unverified / 六维 0 分待评")
        print("待办：逐条写 analysis、打分（score.py apply 重算 total）、核验来源后置 published")
    return C.EXIT_OK if not bad else C.EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
