#!/usr/bin/env python3
"""ERA 六维评分：total 由代码求和，拒绝手填。

用法:
  python3 scripts/score.py check  [路径...] [--json]     # 只校验，total 不符即失败
  python3 scripts/score.py apply  [路径...] [--dry-run]  # 重算并写回（先备份 .bak）
  python3 scripts/score.py rubric [--json]               # 打印评分标准

退出码: 0=通过 / 1=有错误 / 2=用法或读取失败
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _core as C  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="score.py", description="ERA 六维评分（total 代码求和）")
    p.add_argument("action", nargs="?", default="check", choices=["check", "apply", "rubric"],
                   help="check=校验 / apply=重算写回 / rubric=打印标准")
    p.add_argument("paths", nargs="*", help="JSON 文件或目录，默认 content/")
    p.add_argument("--json", action="store_true")
    p.add_argument("--dry-run", action="store_true", help="apply 时只报告不写盘")
    p.add_argument("--max-ts", default=None)
    return p


def rubric_payload() -> dict:
    return {
        "scorerVersion": "score/v1.0",
        "maxTotal": sum(C.DIM_CAPS.values()),
        "dimensions": [
            {"key": d, "name": C.DIM_CN[d], "max": C.DIM_CAPS[d], "guide": C.DIM_GUIDE[d]}
            for d in C.DIMS
        ],
        "rules": [
            "total 必须等于六维之和，由 score.py 计算，禁止手填",
            "每一维必须在 rationale 里写一条 ≥6 字的依据（为什么给这个分）",
            "单项不得超上限；scorerVersion 形如 score/v1.0",
        ],
    }


def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.max_ts:
        C.set_max_ts(args.max_ts)

    if args.action == "rubric":
        payload = rubric_payload()
        if args.json:
            C.dump_json(payload)
        else:
            print(f"ERA 评分标准 · {payload['scorerVersion']} · 满分 {payload['maxTotal']}")
            print("-" * 72)
            for dim in payload["dimensions"]:
                print(f"{dim['name']}({dim['key']}) ≤{dim['max']}")
                print(f"  {dim['guide']}")
            print("-" * 72)
            for rule in payload["rules"]:
                print(f"· {rule}")
        return C.EXIT_OK

    paths = args.paths or [str(C.CONTENT_DIR)]
    try:
        records, load_findings, files = C.load_paths(paths)
    except FileNotFoundError as exc:
        print(f"ERROR [E_PATH] 路径不存在：{exc}", file=sys.stderr)
        return C.EXIT_USAGE

    write = args.action == "apply" and not args.dry_run
    ctx, results, written = C.score_records(records, write=write)
    ctx.findings = load_findings + ctx.findings

    payload = {
        "command": args.action,
        "dryRun": bool(args.dry_run),
        "stats": C.stats_dict(ctx, files),
        "results": results,
        "writtenFiles": written,
        "findings": [f.as_dict() for f in ctx.findings],
    }

    if args.json:
        C.dump_json(payload)
    else:
        print(f"ERA score {args.action} · {C.SCHEMA_VERSION} · {len(results)} 条 event" +
              ("（dry-run，不写盘）" if args.dry_run else ""))
        print("-" * 72)
        for item in results:
            if item["status"] == "ok":
                dims = "+".join(f"{item['dims'][d]:g}" for d in C.DIMS)
                print(f"OK    {item['id']}  total={item['total']}  ({dims} = {item['total']})")
            elif item["status"] == "mismatch":
                if item.get("written") is not None:
                    print(f"FIX   {item['id']}  total={item['total']} → 已写回 {item['written']}")
                else:
                    print(f"MISMATCH {item['id']}  total={item['total']} 应为 {item['expected']}（未写盘）")
            else:
                print(f"ERROR {item['id']}  {item['path']}")
        if ctx.findings:
            print("-" * 72)
            C.print_findings(ctx)
        print("-" * 72)
        print(f"结果：{'PASS' if ctx.ok else 'FAIL'}（{len(ctx.errors)} errors / {len(ctx.warnings)} warnings；写回 {written} 个文件）")

    return C.EXIT_OK if ctx.ok else C.EXIT_FAIL


if __name__ == "__main__":
    raise SystemExit(run())
