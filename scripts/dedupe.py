#!/usr/bin/env python3
"""ERA 去重：dedupeKey / 来源 URL / 标题相似度 三重判定，输出簇与保留建议。

用法:
  python3 scripts/dedupe.py [路径...] [--json] [--out FILE]
  python3 scripts/dedupe.py --key "标题" [--region 中国] [--date 2026-03-01]

退出码: 0=无重复 / 1=发现疑似重复 / 2=用法或读取失败
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _core as C  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="dedupe.py", description="ERA 去重（事件簇 + 保留建议）")
    p.add_argument("paths", nargs="*", help="JSON 文件或目录，默认 content/")
    p.add_argument("--json", action="store_true")
    p.add_argument("--out", default=None, help="把 Markdown 报告写到文件（默认 stdout）")
    p.add_argument("--key", default=None, help="只生成 dedupeKey：传入标题")
    p.add_argument("--region", default="", help="配合 --key 使用")
    p.add_argument("--date", default="", help="配合 --key 使用（YYYY-MM-DD）")
    return p


def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.key is not None:
        key = C.make_dedupe_key(args.key, args.region, args.date)
        if args.json:
            C.dump_json({"dedupeKey": key, "title": args.key, "region": args.region, "date": args.date})
        else:
            print(key)
        return C.EXIT_OK

    paths = args.paths or [str(C.CONTENT_DIR)]
    try:
        records, load_findings, files = C.load_paths(paths)
    except FileNotFoundError as exc:
        print(f"ERROR [E_PATH] 路径不存在：{exc}", file=sys.stderr)
        return C.EXIT_USAGE

    events = [r for r in records if r.kind == "event"]
    shared_weak: list[dict] = []
    clusters = C.dedupe_records(records, shared_out=shared_weak)
    index = {r.rid: r for r in events}

    lines: list[str] = []
    lines.append("# ERA 去重报告")
    lines.append("")
    lines.append(f"- 生成时间：{C.fmt_ts(C.now_ts())}")
    lines.append(f"- 数据根：`{C.ERA_HOME}`")
    lines.append(f"- 事件数：{len(events)} · 疑似重复簇：{len(clusters)}"
                 + (f" · 仅共享汇总页：{len(shared_weak)} 对" if shared_weak else ""))
    lines.append("")

    if clusters:
        for n, cluster in enumerate(clusters, 1):
            keep = cluster["recommended_keep"]
            lines.append(f"## 簇 {n}（{len(cluster['members'])} 条）")
            lines.append("")
            lines.append(f"- 建议保留：`{keep}`（总分最高）；合并 revisions 后删除其余")
            lines.append("")
            lines.append("| id | 标题 | 首报 | 总分 |")
            lines.append("| --- | --- | --- | ---: |")
            for rid in cluster["members"]:
                rec = index.get(rid)
                title = C.short(rec.data.get("title"), 42) if rec else "?"
                first = rec.data.get("firstReportedAt", "-") if rec else "-"
                total = (rec.data.get("scores") or {}).get("total", "-") if rec else "-"
                mark = " ✅" if rid == keep else ""
                lines.append(f"| `{rid}`{mark} | {title} | {first} | {total} |")
            lines.append("")
            lines.append("判定依据：")
            for pair in cluster["pairs"]:
                lines.append(f"- `{pair['a']}` ↔ `{pair['b']}`：{pair['why']}")
            lines.append("")
    else:
        lines.append("未发现疑似重复事件。")
        lines.append("")

    if shared_weak:
        lines.append("## 只共享来源、不算重复（采集端需处理）")
        lines.append("")
        for w in shared_weak[:20]:
            tag = "一手来源冲突" if w.get("kind") == "same_primary" else "汇总页"
            lines.append(f"- `{w['a']}` ↔ `{w['b']}`（{tag}）：`{w['url']}`")
        if len(shared_weak) > 20:
            lines.append(f"- …… 另有 {len(shared_weak) - 20} 对")
        lines.append("")

    markdown = "\n".join(lines)
    if args.out:
        out = Path(args.out).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(markdown + "\n", encoding="utf-8")

    payload = {
        "command": "dedupe",
        "stats": {"files": len(files), "events": len(events), "clusters": len(clusters),
                  "shared_only": len(shared_weak)},
        "clusters": clusters,
        "shared_only": shared_weak,
        "findings": [f.as_dict() for f in load_findings],
    }

    if args.json:
        C.dump_json(payload)
    elif not args.out:
        print(markdown)
    else:
        print(f"已写入 {args.out}")
        print(f"事件 {len(events)} 条，疑似重复簇 {len(clusters)} 组")

    if load_findings:
        return C.EXIT_FAIL
    return C.EXIT_OK if not clusters else C.EXIT_FAIL


if __name__ == "__main__":
    raise SystemExit(run())
