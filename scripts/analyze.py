#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ERA 分析入口：把已核验事件送 AI 分析，产出**草稿**（永不直接发布）。

用法:
  python3 scripts/analyze.py --content content/ --budget 20
  python3 scripts/analyze.py --content content/ --budget 5 --json
  python3 scripts/analyze.py --help

行为:
  * 只处理 verification.status == "passed" 且 status != "published" 的事件；
  * 每个事件产出 content/_intake/analysis/<id>.json，status=pending_review；
  * 超出 --budget 的事件追加到 content/_intake/review-queue.jsonl，不丢弃；
  * --budget 必传（按轮预算，默认值写在 pipeline.config.json，不在代码里写死）；
  * 无密钥：不写任何草稿，写 reports/analyze-<runId>.json，status=failed / reason=no_api_key / executed=false。

退出码:
  0 = 完成（可能 0 条，属 no_change）
  1 = 部分完成（超预算条目已入队）
  2 = 用法错误
  3 = 来源/网络失败（SOURCE_FAILED）
  4 = 无密钥（NO_API_KEY，status=failed + reason=no_api_key，分析未执行）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

TZ = timezone(timedelta(hours=8))
DEFAULT_BASE = "https://api.deepseek.com"
MODEL = "deepseek-chat"
EXIT_OK, EXIT_PARTIAL, EXIT_USAGE, EXIT_SOURCE, EXIT_NO_KEY = 0, 1, 2, 3, 4

PROMPT = """你是国际财经杂志的数据编辑。基于给定事件事实，只输出 JSON：
{"analysis": "<=180字，指出机制与不确定点，禁止形容词堆砌",
 "summary": "<=120字，事实陈述，不加评价",
 "trendLinks": [{"trendId": "已有趋势id", "direction": "support|weaken|challenge", "reason": "<=60字"}]}
规则：不得编造数字；facts 里没有的数字不许出现；无法判断就返回 {"analysis": "", "summary": "", "trendLinks": []}。"""


def now() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def load_events(content: Path) -> list[dict]:
    out = []
    for p in sorted(content.rglob("*.json")):
        if "_intake" in p.parts or p.name.startswith("."):
            continue
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if isinstance(d, dict) and "trendLinks" in d and "facts" in d:
            d["_path"] = str(p)
            out.append(d)
    return out


def api_key() -> str | None:
    for k in ("DEEPSEEK_API_KEY", "DSH_SECRET_DEEPSEEK_API_KEY"):
        v = os.environ.get(k)
        if v:
            return v
    return None


def call_model(key: str, base: str, ev: dict, timeout: int = 60) -> dict:
    body = {
        "model": MODEL,
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": PROMPT},
            {"role": "user", "content": json.dumps(
                {k: ev.get(k) for k in ("title", "summary", "facts", "occurredAt", "region")},
                ensure_ascii=False)},
        ],
    }
    req = urllib.request.Request(
        base.rstrip("/") + "/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        payload = json.loads(r.read().decode("utf-8"))
    return json.loads(payload["choices"][0]["message"]["content"])


def main() -> int:
    ap = argparse.ArgumentParser(prog="analyze.py", description="ERA 分析入口（AI 草稿，不直接发布）")
    ap.add_argument("--content", default="content", help="数据根，默认 content/")
    ap.add_argument("--budget", type=int, required=True,
                    help="本轮模型调用上限（按轮预算，必须显式传；见 pipeline.config.json budgets.analyze）")
    ap.add_argument("--base-url", default=os.environ.get("ERA_MODEL_BASE", DEFAULT_BASE))
    ap.add_argument("--out", default=None, help="运行报告路径")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    content = Path(a.content).resolve()
    if not content.is_dir():
        print(f"[analyze] 数据根不存在: {content}", file=sys.stderr)
        return EXIT_USAGE
    run_id = datetime.now(TZ).strftime("%Y%m%dT%H%M%S")
    report_path = Path(a.out) if a.out else content.parent / "reports" / f"analyze-{run_id}.json"

    events = load_events(content)
    todo = [e for e in events if e.get("status") != "published"
            and (e.get("verification") or {}).get("status") == "passed"]

    key = api_key()
    if not key:
        rep = {"status": "failed", "reason": "no_api_key", "executed": False, "runId": run_id, "at": now(),
               "events_total": len(events), "events_eligible": len(todo),
               "detail": "未配置 DEEPSEEK_API_KEY / DSH_SECRET_DEEPSEEK_API_KEY，本轮分析未执行",
               "next": "配置密钥后重跑本命令；站点保留上一版，不得标记为已更新"}
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
        print(json.dumps(rep, ensure_ascii=False, indent=1) if a.json
              else f"[analyze] 分析未执行：无密钥（eligible={len(todo)}）；报告 {report_path}")
        return EXIT_NO_KEY

    if not todo:
        rep = {"status": "no_change", "executed": True, "runId": run_id, "at": now(),
               "events_total": len(events), "events_eligible": 0}
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
        print("[analyze] no_change：没有待分析且已核验的事件")
        return EXIT_OK

    outdir = content / "_intake" / "analysis"
    outdir.mkdir(parents=True, exist_ok=True)
    queue = content / "_intake" / "review-queue.jsonl"
    done, queued, failed = 0, 0, 0
    for i, ev in enumerate(todo):
        if i >= a.budget:
            with queue.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"id": ev.get("id"), "status": "pending_review",
                                    "queuedAt": now(), "reason": "over_budget"}, ensure_ascii=False) + "\n")
            queued += 1
            continue
        try:
            draft = call_model(key, a.base_url, ev)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, KeyError, json.JSONDecodeError) as e:
            failed += 1
            print(f"[analyze] {ev.get('id')} 调用失败: {type(e).__name__}", file=sys.stderr)
            continue
        (outdir / f"{ev.get('id')}.json").write_text(json.dumps(
            {"id": ev.get("id"), "status": "pending_review", "source": ev.get("_path"),
             "model": MODEL, "at": now(), "draft": draft}, ensure_ascii=False, indent=1), encoding="utf-8")
        done += 1
        time.sleep(0.2)

    status = "partial" if (queued or failed) else "ok"
    rep = {"status": status, "executed": True, "runId": run_id, "at": now(),
           "events_total": len(events), "events_eligible": len(todo),
           "drafted": done, "queued": queued, "failed": failed, "budget": a.budget}
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(rep, ensure_ascii=False, indent=1) if a.json
          else f"[analyze] drafted={done} queued={queued} failed={failed} budget={a.budget}")
    if failed and not done:
        return EXIT_SOURCE
    return EXIT_PARTIAL if (queued or failed) else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
