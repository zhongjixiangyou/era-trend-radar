#!/usr/bin/env python3
"""ERA 数据层命令行入口（纯标准库，GitHub Actions 可直接跑）。

用法:
  python3 scripts/cli.py validate [路径...] [--json] [--refs auto|error|warn|skip]
  python3 scripts/cli.py score    [路径...] [--write] [--json]
  python3 scripts/cli.py dedupe   [路径...] [--json] [--out FILE]
  python3 scripts/cli.py report   [路径...] [--out FILE] [--json]

退出码: 0=通过 / 1=发现问题 / 2=用法或读取失败
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _core as C  # noqa: E402
import dedupe as dedupe_mod  # noqa: E402
import score as score_mod  # noqa: E402
import validate as validate_mod  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="cli.py",
        description=f"ERA 数据层 CLI（{C.SCHEMA_VERSION}）：validate | score | dedupe | report",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    v = sub.add_parser("validate", help="字段/枚举/时间/评分/引用一致性校验")
    v.add_argument("paths", nargs="*", help="JSON 文件或目录，默认 content/")
    v.add_argument("--json", action="store_true")
    v.add_argument("--refs", choices=["auto", "error", "warn", "skip"], default="auto")
    v.add_argument("--max-ts", default=None)
    v.add_argument("--baseline", default=None, help="上一版数据目录（无 git 时检查追加性）")
    v.add_argument("--history", choices=["auto", "git", "baseline", "off"], default="auto")

    s = sub.add_parser("score", help="六维评分：total 代码求和")
    s.add_argument("paths", nargs="*", help="JSON 文件或目录，默认 content/")
    s.add_argument("--write", action="store_true", help="重算并把 total 写回（先备份 .bak）")
    s.add_argument("--dry-run", action="store_true", help="只报告不写盘")
    s.add_argument("--json", action="store_true")
    s.add_argument("--max-ts", default=None)

    d = sub.add_parser("dedupe", help="去重：dedupeKey / URL / 标题相似度")
    d.add_argument("paths", nargs="*", help="JSON 文件或目录，默认 content/")
    d.add_argument("--json", action="store_true")
    d.add_argument("--out", default=None, help="Markdown 报告输出路径")
    d.add_argument("--key", default=None, help="只生成 dedupeKey：传入标题")
    d.add_argument("--region", default="")
    d.add_argument("--date", default="")

    r = sub.add_parser("report", help="生成体检报告（reports/report.md）")
    r.add_argument("paths", nargs="*", help="JSON 文件或目录，默认 content/")
    r.add_argument("--out", default=None, help="输出路径；'-' 表示打印到 stdout")
    r.add_argument("--json", action="store_true")
    r.add_argument("--max-ts", default=None)
    return p


def cmd_validate(args: argparse.Namespace) -> int:
    argv = list(args.paths)
    if args.json:
        argv.append("--json")
    argv += ["--refs", args.refs]
    if args.max_ts:
        argv += ["--max-ts", args.max_ts]
    if args.baseline:
        argv += ["--baseline", args.baseline]
    argv += ["--history", args.history]
    return validate_mod.run(argv)


def cmd_score(args: argparse.Namespace) -> int:
    argv = ["apply" if args.write and not args.dry_run else "check", *args.paths]
    if args.dry_run:
        argv.append("--dry-run")
    if args.json:
        argv.append("--json")
    if args.max_ts:
        argv += ["--max-ts", args.max_ts]
    return score_mod.run(argv)


def cmd_dedupe(args: argparse.Namespace) -> int:
    argv = list(args.paths)
    if args.json:
        argv.append("--json")
    if args.out:
        argv += ["--out", args.out]
    if args.key is not None:
        argv += ["--key", args.key, "--region", args.region, "--date", args.date]
    return dedupe_mod.run(argv)


def cmd_report(args: argparse.Namespace) -> int:
    if args.max_ts:
        C.set_max_ts(args.max_ts)
    paths = args.paths or [str(C.CONTENT_DIR)]
    try:
        records, load_findings, files = C.load_paths(paths)
    except FileNotFoundError as exc:
        print(f"ERROR [E_PATH] 路径不存在：{exc}", file=sys.stderr)
        return C.EXIT_USAGE

    strict = len(files) > 1
    ctx = C.validate_records(records, strict_refs=strict)
    ctx.findings = load_findings + ctx.findings

    markdown = C.build_report(records, ctx)
    if ctx.findings:
        markdown += "\n## 校验问题\n\n"
        for f in sorted(ctx.findings, key=lambda x: (0 if x.level == "error" else 1, x.path))[:80]:
            markdown += f"- `{f.level.upper()}` [{f.code}] {f.path}#{f.field}：{f.message}\n"
        markdown += ""

    target = args.out
    if target == "-":
        print(markdown)
        written_path = None
    else:
        out_path = Path(target).expanduser() if target else (C.REPORTS_DIR / "report.md")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(markdown + "\n", encoding="utf-8")
        written_path = str(out_path)

    payload = {
        "command": "report",
        "stats": C.stats_dict(ctx, files),
        "output": written_path,
        "findings": [f.as_dict() for f in ctx.findings],
    }
    if args.json:
        C.dump_json(payload)
    elif written_path:
        stats = payload["stats"]
        print(f"ERA report · {stats['records']} 记录（event {stats['events']} / trend {stats['trends']} / forecast {stats['forecasts']}）")
        print(f"校验：{stats['errors']} errors / {stats['warnings']} warnings")
        print(f"已写入 {written_path}")

    return C.EXIT_OK if ctx.ok else C.EXIT_FAIL


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handlers = {
        "validate": cmd_validate,
        "score": cmd_score,
        "dedupe": cmd_dedupe,
        "report": cmd_report,
    }
    return handlers[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
