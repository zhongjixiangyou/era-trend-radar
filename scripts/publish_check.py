#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""发布门禁（只读）：published 必须已核验、证据等级达标、有 primary 来源。

用法:
  python3 scripts/publish_check.py --content content/ --json
  python3 scripts/publish_check.py --file fixtures/publish/bad.json

退出码: 0=可发布 / 1=有阻断项 / 2=用法或读取失败
"""
from __future__ import annotations
import argparse, json, sys
from pathlib import Path

GRADES = {"single_primary", "multi_independent"}
EXIT_OK, EXIT_FAIL, EXIT_USAGE = 0, 1, 2


def check_event(d: dict) -> list[str]:
    errs = []
    if d.get("status") != "published":
        return errs
    eid = d.get("id", "?")
    v = d.get("verification") or {}
    if v.get("status") != "passed":
        errs.append(f"{eid}: published 但 verification.status={v.get('status')!r}（必须 passed）")
    if d.get("evidenceGrade") not in GRADES:
        errs.append(f"{eid}: evidenceGrade={d.get('evidenceGrade')!r}（published 只允许 {sorted(GRADES)}）")
    if not any(s.get("type") == "primary" for s in d.get("sources") or []):
        errs.append(f"{eid}: 没有 type=primary 的来源")
    if not (v.get("checks") or []):
        errs.append(f"{eid}: verification.checks 为空，核验记录不可追溯")
    return errs


def main() -> int:
    ap = argparse.ArgumentParser(prog="publish_check.py", description="发布门禁（只读）")
    ap.add_argument("--content", default="content")
    ap.add_argument("--file", default=None)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    files = []
    if a.file:
        files = [Path(a.file)]
    else:
        root = Path(a.content)
        if not root.is_dir():
            print(f"[publish_check] 目录不存在: {root}", file=sys.stderr); return EXIT_USAGE
        files = sorted(p for p in root.rglob("*.json") if "_intake" not in p.parts and "inbox" not in p.parts)
    errs, n = [], 0
    for p in files:
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            errs.append(f"{p.name}: 读取失败 {e}"); continue
        if isinstance(d, dict) and "trendLinks" in d and "facts" in d:
            n += 1
            errs += check_event(d)
    rep = {"checked": n, "blocking": len(errs), "ok": not errs, "errors": errs}
    print(json.dumps(rep, ensure_ascii=False, indent=1) if a.json else ("[publish_check] OK" if not errs else "\n".join(errs)))
    return EXIT_OK if not errs else EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main())
