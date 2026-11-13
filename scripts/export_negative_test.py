#!/usr/bin/env python3
"""export_astro.py 的反例回归：每条都要求「必须报错，且错误信息能定位」。

用法: python3 scripts/export_negative_test.py [-v]

做法：以 samples/export-astro 为基线复制到临时目录，逐条破坏一个字段，
跑 `--check --json`，断言退出码与错误关键词。全过 → 0，有漏网 → 1。
"""

from __future__ import annotations

import copy
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASE = ROOT / "samples" / "export-astro"
SCRIPT = ROOT / "scripts" / "export_astro.py"
SCHEMA = ROOT / "schema"
VERBOSE = "-v" in sys.argv


def run_case(name: str, mutate, want_keyword: str, want_exit: int = 1) -> bool:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        content = tmp_path / "content"
        shutil.copytree(BASE, content)
        if mutate is not None:
            mutated = mutate(content)
            if mutated is False:
                return fail(name, "mutate 返回 False（基线结构不符合预期）")
        proc = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--content",
                str(content),
                "--out",
                str(tmp_path / "out"),
                "--schema",
                str(SCHEMA),
                "--check",
                "--json",
            ],
            capture_output=True,
            text=True,
        )
        if proc.returncode != want_exit:
            return fail(
                name,
                f"退出码 {proc.returncode}，期望 {want_exit}\nstderr={proc.stderr.strip()[:400]}",
            )
        if want_exit == 2:
            return ok(name, f"exit=2, {proc.stderr.strip().splitlines()[-1][:120]}")
        try:
            report = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return fail(name, f"stdout 不是 JSON：{proc.stdout[:300]}")
        hit = [e for e in report.get("errors", []) if want_keyword in e]
        if not hit:
            return fail(name, f"没报出含 {want_keyword!r} 的错误：{report.get('errors')}")
        return ok(name, hit[0][:160])


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def save(path: Path, doc: dict) -> None:
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def mutate_event(fn):
    """返回一个在 content 上改 event-a.json 的 mutate。"""

    def _mutate(content: Path) -> bool:
        path = content / "event-a.json"
        doc = load(path)
        result = fn(doc)
        if result is False:
            return False
        save(path, doc)
        return True

    return _mutate


def main() -> int:
    cases: list[tuple[str, object, str, int]] = []

    def add(name, mutate, keyword, exit_code=1):
        cases.append((name, mutate, keyword, exit_code))

    def total_mismatch(d):
        d["scores"]["total"] = 99

    add(
        "总分手填与六维和不符",
        mutate_event(total_mismatch),
        "total",
    )

    def dangling_trend(d):
        d["trendLinks"][0]["trendId"] = "tr_not-exist"

    add("trendId 悬空", mutate_event(dangling_trend), "不存在")

    def published_without_primary(d):
        d["status"] = "published"
        d["sources"] = [s for s in d["sources"] if s["type"] != "primary"]
        if len(d["sources"]) == 0:
            d["sources"] = [
                {
                    "url": "https://example.com/x",
                    "title": "二手转述",
                    "publisher": "示例媒体",
                    "type": "corroboration",
                }
            ]

    add("published 来源无 primary", mutate_event(published_without_primary), "primary")

    def dim_over_cap(d):
        d["scores"]["structural"] = 30
        d["scores"]["total"] = sum(
            d["scores"][k]
            for k in ("structural", "scope", "signal", "delta", "timeliness", "crossDomain")
        )

    add("单维超上限", mutate_event(dim_over_cap), "structural")

    def summary_too_long(d):
        d["summary"] = "长" * 130

    add("summary 超 120 字", mutate_event(summary_too_long), "summary")

    def bad_category(d):
        d["category"] = "外星经济"

    add("category 非法", mutate_event(bad_category), "category")

    def future_collected(d):
        d["collectedAt"] = "2099-01-01T00:00:00+08:00"

    add("collectedAt 在未来", mutate_event(future_collected), "时间穿越")

    def dup_key(content: Path) -> bool:
        a = load(content / "event-a.json")
        b = load(content / "event-b.json")
        b["dedupeKey"] = a["dedupeKey"]
        save(content / "event-b.json", b)
        return True

    add("dedupeKey 重复", dup_key, "dedupeKey")

    def bad_id(d):
        d["id"] = "event-no-prefix"

    add("id 缺 ev_ 前缀", mutate_event(bad_id), "ev_")

    def forecast_url_ref(content: Path) -> bool:
        path = content / "forecast-1.json"
        f = load(path)
        f["evidenceRefs"] = ["https://example.com/a"]
        save(path, f)
        return True

    add("forecast evidenceRefs 用 URL", forecast_url_ref, "事件 id")

    def empty_counter(content: Path) -> bool:
        path = content / "trend-1.json"
        t = load(path)
        t["counterEvidence"] = []
        save(path, t)
        return True

    add("趋势反证为空", empty_counter, "counterEvidence")

    def invalid_json(content: Path) -> bool:
        (content / "broken.json").write_text("{not json", encoding="utf-8")
        return True

    add("非法 JSON", invalid_json, "", 2)

    failed = 0
    for name, mutate, keyword, exit_code in cases:
        if not run_case(name, mutate, keyword, exit_code):
            failed += 1
    if failed:
        print(f"NEGATIVE FAILED：{failed}/{len(cases)} 条没按预期拦住")
        return 1
    print(f"NEGATIVE OK：{len(cases)} 条反例全部拦住")
    return 0


def ok(name: str, detail: str) -> bool:
    print(f"  PASS {name}  ← {detail}")
    return True


def fail(name: str, detail: str) -> bool:
    print(f"  FAIL {name}：{detail}")
    return False


if __name__ == "__main__":
    sys.exit(main())
