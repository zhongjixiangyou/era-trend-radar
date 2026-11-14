#!/usr/bin/env python3
"""ERA 数据校验（era-schema/v1）。

用法:
  python3 scripts/validate.py [路径...] [--json] [--refs auto|error|warn|skip]

退出码: 0=全部通过 / 1=存在校验错误 / 2=用法或读取失败
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _core as C  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="validate.py", description="ERA 数据校验：字段/枚举/时间/评分/引用一致性")
    p.add_argument("paths", nargs="*", help="JSON 文件或目录，默认 content/")
    p.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    p.add_argument("--refs", choices=["auto", "error", "warn", "skip"], default="auto",
                   help="跨记录引用检查：auto=多文件按错误、单文件按警告（默认）")
    p.add_argument("--as-of", default=None,
                   help="窗口基准时刻（ISO 8601，默认读 ERA_AS_OF，再退到 --max-ts/配置/时钟）")
    p.add_argument("--max-ts", default=None, help=f"数据时间上限（等价 as-of）；默认 {C.MAX_TS_DEFAULT}")
    p.add_argument("--baseline", default=None, help="上一版数据目录（没有 git 时用它检查追加性）")
    p.add_argument("--history", choices=["auto", "git", "baseline", "off"], default="auto",
                   help="追加历史检查：auto=有 git 用 HEAD、给了 --baseline 用目录（默认），off=关闭")
    p.add_argument("--limit", type=int, default=200, help="文本输出最多显示多少条（默认 200）")
    return p


def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # 一次运行只解析一次窗口；--as-of/ERA_AS_OF > --max-ts/ERA_MAX_TS > 配置 > 时钟
    win = C.apply_window(C.load_pipeline_config(), as_of=args.as_of, max_ts=args.max_ts)

    paths = args.paths or [str(C.CONTENT_DIR)]
    try:
        ctx, files = C.validate_paths(paths, strict_refs=args.refs,
                                      baseline_dir=args.baseline, history=args.history)
    except FileNotFoundError as exc:
        print(f"ERROR [E_PATH] 路径不存在：{exc}", file=sys.stderr)
        return C.EXIT_USAGE

    payload = {
        "command": "validate",
        "stats": C.stats_dict(ctx, files),
        "findings": [f.as_dict() for f in ctx.findings],
    }

    if args.json:
        C.dump_json(payload)
    else:
        stats = payload["stats"]
        print(f"ERA validate · {C.SCHEMA_VERSION} · {stats['files']} 文件 · "
              f"{stats['records']} 记录（event {stats['events']} / trend {stats['trends']} / forecast {stats['forecasts']}）")
        print(f"窗口 {win.from_str} → {win.to_str}（{win.mode}，asOf {C.fmt_ts(win.as_of)} via {win.as_of_source}/{win.as_of_from}）"
              f" · 数据时间上限 {stats['maxTs']}")
        print("-" * 72)
        C.print_findings(ctx, args.limit)
        print("-" * 72)
        verdict = "PASS" if ctx.ok else "FAIL"
        print(f"结果：{verdict}（{stats['errors']} errors / {stats['warnings']} warnings）")

    return C.EXIT_OK if ctx.ok else C.EXIT_FAIL


if __name__ == "__main__":
    raise SystemExit(run())
