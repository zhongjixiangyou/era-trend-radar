#!/usr/bin/env python3
"""校验 content/_intake/analysis/ 下的分析草稿（本机分析流程专用）。

整库 `cli.py validate` 只覆盖 content/ 下的正式记录，不检查 _intake 草稿，
所以草稿必须单独验一遍：结构、长度、趋势 id 是否真实存在、来源是否可追溯、
以及「不许出现 facts 里没有的数字」。

用法：
  python3 scripts/check_drafts.py            # 校验全部草稿
  python3 scripts/check_drafts.py --only <id>  # 只校验一条
退出码：0 全通过 / 1 有问题
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONTENT = ROOT / "content"
DRAFTS = CONTENT / "_intake" / "analysis"

LIMITS = {"analysis": 180, "summary": 120, "reason": 60}
DIRECTIONS = {"support", "weaken", "challenge"}


def load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def trend_ids() -> set[str]:
    ids = set()
    for p in (CONTENT / "trends").glob("*.json"):
        try:
            ids.add(load(p).get("id"))
        except Exception:
            pass
    return ids


def digits(text: str) -> set[str]:
    # 只挑「两位以上的数字串」，个位数（比如排序编号）噪声太大
    return {m for m in re.findall(r"\d+(?:\.\d+)?", text or "") if len(m.replace(".", "")) >= 2}


def check_one(path: Path, known_trends: set[str]) -> list[str]:
    errs: list[str] = []
    try:
        d = load(path)
    except Exception as e:
        return [f"{path.name}: 不是合法 JSON（{e}）"]

    eid = d.get("id")
    if not eid:
        errs.append("缺少 id")
    elif eid != path.stem:
        errs.append(f"文件名与 id 不一致（{path.stem} vs {eid}）")
    if d.get("status") != "pending_review":
        errs.append(f"status 必须是 pending_review，实际 {d.get('status')!r}")
    for k in ("source", "model", "at"):
        if not d.get(k):
            errs.append(f"缺少 {k}")

    src = d.get("source")
    if src:
        sp = ROOT / src
        if not sp.exists():
            errs.append(f"source 指向的文件不存在：{src}")
        else:
            try:
                ev = load(sp)
            except Exception:
                ev = {}
            draft = d.get("draft") or {}
            facts_text = json.dumps(ev.get("facts") or [], ensure_ascii=False) + \
                (ev.get("summary") or "") + (ev.get("title") or "")
            for field in ("analysis", "summary"):
                bad = [n for n in digits(draft.get(field, "")) if n not in facts_text]
                if bad:
                    errs.append(f"{field} 出现来源里没有的数字：{', '.join(sorted(bad))}")

    draft = d.get("draft")
    if not isinstance(draft, dict):
        return errs + ["缺少 draft 对象"]
    for field in ("analysis", "summary"):
        v = draft.get(field, "")
        if not isinstance(v, str):
            errs.append(f"{field} 必须是字符串")
        elif len(v) > LIMITS[field]:
            errs.append(f"{field} 超长：{len(v)} > {LIMITS[field]}")
    links = draft.get("trendLinks")
    if not isinstance(links, list):
        errs.append("trendLinks 必须是数组")
        links = []
    for i, l in enumerate(links):
        if not isinstance(l, dict):
            errs.append(f"trendLinks[{i}] 不是对象")
            continue
        tid = l.get("trendId")
        if tid not in known_trends:
            errs.append(f"trendLinks[{i}] 引用了不存在的趋势：{tid!r}")
        if l.get("direction") not in DIRECTIONS:
            errs.append(f"trendLinks[{i}] direction 非法：{l.get('direction')!r}")
        r = l.get("reason", "")
        if not isinstance(r, str) or not r.strip():
            errs.append(f"trendLinks[{i}] 缺少 reason")
        elif len(r) > LIMITS["reason"]:
            errs.append(f"trendLinks[{i}] reason 超长：{len(r)} > {LIMITS['reason']}")
    return errs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default=None)
    a = ap.parse_args()

    if not DRAFTS.exists():
        print("[check-drafts] 没有草稿目录，视为通过（no drafts）")
        return 0

    files = sorted(DRAFTS.glob("*.json"))
    if a.only:
        files = [f for f in files if f.stem == a.only]
    if not files:
        print("[check-drafts] 没有待校验草稿（no drafts）")
        return 0

    known = trend_ids()
    total_err = 0
    for f in files:
        errs = check_one(f, known)
        total_err += len(errs)
        if errs:
            print(f"FAIL {f.name}")
            for e in errs:
                print(f"     - {e}")
        else:
            d = load(f)
            print(f"PASS {f.name} · analysis {len(d['draft'].get('analysis',''))} 字 · "
                  f"summary {len(d['draft'].get('summary',''))} 字 · links {len(d['draft'].get('trendLinks', []))}")
    print(f"\n[check-drafts] {len(files)} 份草稿，{total_err} 个问题 → "
          f"{'PASS' if total_err == 0 else 'FAIL'}")
    return 0 if total_err == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
