#!/usr/bin/env python3
"""窗口契约 v1 独立验证（qa 侧，只读业务代码，不改、不 commit）。

口径来源：docs/window-rolling.md（契约 v1）
- Asia/Shanghai；一次运行固定一个 as-of
- rolling: from = asOf 当天 - (days-1) 天 00:00+08；to = asOf 当天；上界 = asOf 时刻本身
- 旧记录 date < from → warning(WINDOW_OUT)，不 error
- 上界之后 → error(TS_FUTURE)
- fixed: 与 2026-11-13 冻结时完全一致（from=2026-05-13, to=2026-11-13）

用法:
  python3 qa/window-verify/run_verify.py            # 全跑，打印表格 + 写 report.md/report.json
  python3 qa/window-verify/run_verify.py --only C   # 只跑某组（A=resolve_window 单元, B=JS, C=CLI, D=export）
  python3 qa/window-verify/run_verify.py --strict   # BLOCKED 也算失败（给 CI 用）
"""
from __future__ import annotations

import argparse
import datetime as dt
import inspect
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
SCRIPTS = ROOT / "scripts"
TMP = HERE / "_tmp"
TZ = dt.timezone(dt.timedelta(hours=8))

AS_OF = "2026-11-14T12:00:00+08:00"
DAYS = 180

RESULTS: list[dict] = []


# --------------------------------------------------------------------------- #
# 契约正向推导（独立实现，不 import 业务代码）
# --------------------------------------------------------------------------- #
def contract_window(as_of_iso: str, days: int = DAYS) -> dict:
    t = dt.datetime.fromisoformat(as_of_iso).astimezone(TZ)
    to = t.date()
    frm = to - dt.timedelta(days=days - 1)
    return {"from": frm.isoformat(), "to": to.isoformat(), "asOf": t.isoformat(), "days": days}


WIN = contract_window(AS_OF)


def judge(ts: str, win: dict) -> str:
    """契约判定：ok / old（warning）/ future（error）。"""
    raw = ts.strip()
    if len(raw) == 10:  # date-only
        d = dt.date.fromisoformat(raw)
        if d > dt.date.fromisoformat(win["to"]):
            return "future"
        if d < dt.date.fromisoformat(win["from"]):
            return "old"
        return "ok"
    t = dt.datetime.fromisoformat(raw)
    if t.tzinfo is None:
        t = t.replace(tzinfo=TZ)
    as_of = dt.datetime.fromisoformat(win["asOf"])
    if t > as_of:
        return "future"
    # 下界是 from 当天 00:00+08：00:00:00 本身通过，前一秒算旧
    lower = dt.datetime.fromisoformat(win["from"] + "T00:00:00+08:00")
    if t < lower:
        return "old"
    return "ok"


# --------------------------------------------------------------------------- #
# 记录器
# --------------------------------------------------------------------------- #
def record(cid: str, group: str, desc: str, expected: str, actual: str, status: str, evidence: str = "") -> None:
    RESULTS.append(
        {
            "id": cid,
            "group": group,
            "desc": desc,
            "expected": expected,
            "actual": actual,
            "status": status,
            "evidence": evidence,
        }
    )


def cmp(cid, group, desc, expected, actual, ok: bool, evidence="", blocked_reason=""):
    if blocked_reason:
        record(cid, group, desc, expected, actual, "BLOCKED", evidence or blocked_reason)
    else:
        record(cid, group, desc, expected, actual, "PASS" if ok else "FAIL", evidence)


# --------------------------------------------------------------------------- #
# 执行工具
# --------------------------------------------------------------------------- #
def run(cmd: list[str], env_extra: dict | None = None, timeout: int = 180) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    for k in ("ERA_MAX_TS", "ERA_AS_OF", "ERA_WINDOW_FROM", "ERA_WINDOW_TO", "ERA_WINDOW_MODE"):
        env.pop(k, None)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, env=env, timeout=timeout)


def as_json(stdout: str) -> dict | None:
    s = stdout.strip()
    if not s:
        return None
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        i, j = s.find("{"), s.rfind("}")
        if i >= 0 and j > i:
            try:
                return json.loads(s[i : j + 1])
            except json.JSONDecodeError:
                return None
        return None


def findings(payload: dict) -> list[dict]:
    return payload.get("findings") or []


def by_level(payload: dict, level: str) -> list[dict]:
    return [f for f in findings(payload) if f.get("level") == level]


def hit(payload: dict, code: str, field: str | None = None) -> list[dict]:
    out = []
    for f in findings(payload):
        if f.get("code") != code:
            continue
        if field and f.get("field") != field:
            continue
        out.append(f)
    return out


def short(payload: dict, n: int = 6) -> str:
    items = [f"[{f.get('level')}/{f.get('code')}] {f.get('path')}#{f.get('field')}" for f in findings(payload)]
    if not items:
        return "无 findings"
    return "；".join(items[:n]) + ("" if len(items) <= n else f" …共 {len(items)} 条")


def unsupported(proc: subprocess.CompletedProcess, flag: str) -> bool:
    blob = (proc.stderr or "") + (proc.stdout or "")
    return proc.returncode == 2 and ("unrecognized arguments" in blob and flag in blob)


# --------------------------------------------------------------------------- #
# fixture 构造：克隆真实事件，只改时间与标识，保证 schema 本身合法
# --------------------------------------------------------------------------- #
def template_event() -> dict:
    files = sorted((ROOT / "content" / "events").glob("*.json"))
    if not files:
        raise SystemExit("content/events 为空，无法取模板")
    base = json.loads(files[0].read_text(encoding="utf-8"))
    return base


def make_event(base: dict, eid: str, occurred: str, first_report: str, collected: str) -> dict:
    e = json.loads(json.dumps(base))  # deep copy
    e["id"] = eid
    e["dedupeKey"] = f"ev:qa-{eid.replace('_', '-')}"
    e["title"] = f"QA 窗口边界用例 {eid}"
    e["occurredAt"] = occurred
    e["firstReportedAt"] = first_report
    e["collectedAt"] = collected
    e["sources"] = [
        {
            "url": "https://example.com/qa-window",
            "title": f"QA fixture {eid}",
            "publisher": "QA",
            "publishedAt": occurred if len(occurred) > 10 else f"{occurred}T00:00:00+08:00",
            "type": "primary",
        }
    ]
    e["status"] = "published"
    e["trendLinks"] = []
    e["forecastRefs"] = []
    e["revisions"] = []
    e["retro"] = {"isRetro": False}
    e["verification"] = {
        "status": "passed",
        "method": "qa-fixture",
        "checkedAt": collected,
        "checks": [{"kind": "source_text", "result": "pass", "evidence": "QA 夹具：只验窗口边界"}],
    }
    e["credibility"] = {"level": "partial", "basis": "QA 夹具，仅用于窗口边界验证"}
    e["evidenceGrade"] = "single_primary"
    e["scoreHistory"] = [
        {
            "at": f"{occurred}T00:00:00+08:00" if len(occurred) == 10 else occurred,
            "by": "qa",
            "note": "夹具初始分",
            "scores": {k: e["scores"][k] for k in
                       ("structural", "scope", "signal", "delta", "timeliness", "crossDomain", "total")
                       if k in e["scores"]},
        }
    ]
    li = e["scores"]
    e["scores"] = {
        "structural": li["structural"],
        "scope": li["scope"],
        "signal": li["signal"],
        "delta": li["delta"],
        "timeliness": li["timeliness"],
        "crossDomain": li["crossDomain"],
        "total": sum(li[k] for k in ("structural", "scope", "signal", "delta", "timeliness", "crossDomain")),
        "rationale": {k: "QA 夹具：仅验窗口边界" for k in
                      ("structural", "scope", "signal", "delta", "timeliness", "crossDomain")},
        "scorerVersion": "score/v1.0",
    }
    return e


def write_fixture(name: str, events: list[dict]) -> Path:
    d = TMP / name
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)
    for ev in events:
        (d / f"{ev['id']}.json").write_text(json.dumps(ev, ensure_ascii=False, indent=1), encoding="utf-8")
    return d


def validate_on(path: Path, extra: list[str], env_extra: dict | None = None):
    return run([sys.executable, "scripts/validate.py", str(path), "--json", "--history", "off", *extra], env_extra)


def fixture_events() -> dict[str, dict]:
    base = template_event()
    ev = {}
    # 跨日主 fixture（Coder 指定三条）
    ev["edge"] = make_event(base, "ev_qa_edge_0030", "2026-11-14T00:30:00+08:00",
                            "2026-11-14T00:30:00+08:00", "2026-11-14T00:31:00+08:00")
    ev["old"] = make_event(base, "ev_qa_old_0518", "2026-05-18T12:00:00+08:00",
                           "2026-05-18T12:00:00+08:00", "2026-05-18T13:00:00+08:00")
    ev["future"] = make_event(base, "ev_qa_future_1115", "2026-11-15T09:00:00+08:00",
                              "2026-11-15T09:00:00+08:00", "2026-11-15T09:05:00+08:00")
    # 补充边界
    ev["lower_exact"] = make_event(base, "ev_qa_lower_exact", "2026-05-19T00:00:00+08:00",
                                   "2026-05-19T00:00:00+08:00", "2026-05-19T01:00:00+08:00")
    ev["lower_minus1s"] = make_event(base, "ev_qa_lower_minus1s", "2026-05-18T23:59:59+08:00",
                                     "2026-05-18T23:59:59+08:00", "2026-05-19T00:10:00+08:00")
    ev["upper_exact"] = make_event(base, "ev_qa_upper_exact", "2026-11-14T12:00:00+08:00",
                                   "2026-11-14T12:00:00+08:00", "2026-11-14T12:00:30+08:00")
    ev["upper_plus1s"] = make_event(base, "ev_qa_upper_plus1s", "2026-11-14T12:00:01+08:00",
                                    "2026-11-14T12:00:01+08:00", "2026-11-14T12:00:30+08:00")
    ev["date_only_today"] = make_event(base, "ev_qa_dateonly_today", "2026-11-14",
                                       "2026-11-14T00:00:00+08:00", "2026-11-14T00:00:00+08:00")
    ev["date_only_next"] = make_event(base, "ev_qa_dateonly_next", "2026-11-15",
                                      "2026-11-15T00:00:00+08:00", "2026-11-15T00:00:00+08:00")
    return ev


# --------------------------------------------------------------------------- #
# A 组：scripts/_core.py 的 resolve_window 单元
# --------------------------------------------------------------------------- #
def load_resolve_window():
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    import importlib

    core = importlib.import_module("_core")
    fn = getattr(core, "resolve_window", None)
    if fn is None:
        return None, core
    return fn, core


def call_resolve(fn, cfg: dict, as_of: str | None):
    """容错调用：签名未知时按常见形态试。"""
    sig = inspect.signature(fn)
    params = list(sig.parameters)
    dt_as_of = dt.datetime.fromisoformat(as_of) if as_of else None
    strategies = []
    if len(params) >= 2:
        strategies.append(lambda: fn(cfg, dt_as_of))
        strategies.append(lambda: fn(cfg, as_of))
        strategies.append(lambda: fn(cfg, as_of=as_of))
    else:
        strategies.append(lambda: fn(cfg))
    last = None
    for st in strategies:
        try:
            return st(), None
        except Exception as exc:  # noqa: BLE001
            last = exc
    return None, last


def norm_window(win) -> dict:
    if win is None:
        return {}
    if not isinstance(win, dict):
        win = getattr(win, "__dict__", {}) or {}
    get = lambda *keys: next((win[k] for k in keys if k in win and win[k] is not None), None)  # noqa: E731

    def _iso(v) -> str:
        if v is None:
            return ""
        if isinstance(v, (dt.datetime, dt.date)):
            return v.isoformat()
        return str(v)

    return {
        "from": _iso(get("from", "start", "window_from", "from_str", "from_date")),
        "to": _iso(get("to", "end", "window_to", "to_str", "to_date")),
        "asOf": _iso(get("asOf", "as_of", "asof")),
        "upper": _iso(get("upper", "upper_ts")),
        "mode": str(get("mode", "window_mode") or ""),
        "days": get("days"),
        "asOfSource": str(get("asOfSource", "as_of_source") or ""),
        "frozenAt": str(get("frozenAt", "frozen_at") or ""),
    }


def day_of(v: str) -> str:
    return v[:10]


def group_a(cfg_rolling: dict, cfg_fixed: dict) -> None:
    fn, core = load_resolve_window()
    if fn is None:
        for cid, desc, exp in [
            ("A1", "resolve_window 存在且滚动窗口取值正确", f"from={WIN['from']} to={WIN['to']} days=180 mode=rolling"),
            ("A2", "返回体带 asOf（本轮固定锚点）", f"asOf={AS_OF}"),
            ("A3", "返回体带 asOfSource", "cli"),
            ("A4", "asOf=14T00:00:01 与 14T23:59:59 → 同一个 to/from", "to=2026-11-14 from=2026-05-19（两者相同）"),
            ("A5", "跨日：13T23:59:59 → to=11-13 from=05-18（较 14 日整体移一天）", "to=2026-11-13 from=2026-05-18"),
            ("A6", "fixed 模式回归", "from=2026-05-13 to=2026-11-13 mode=fixed 且保留 frozenAt"),
        ]:
            record(cid, "A", desc, exp, "scripts/_core.py 无 resolve_window（未实现）", "BLOCKED",
                   "inspect: getattr(_core,'resolve_window') -> None")
        return

    # A1
    win, err = call_resolve(fn, cfg_rolling, AS_OF)
    nw = norm_window(win)
    got = f"from={day_of(nw['from'])} to={day_of(nw['to'])} days={nw['days']} mode={nw['mode']}"
    cmp("A1", "A", "resolve_window 滚动窗口取值",
        f"from={WIN['from']} to={WIN['to']} days=180 mode=rolling", got or f"调用失败 {err}",
        day_of(nw["from"]) == WIN["from"] and day_of(nw["to"]) == WIN["to"]
        and str(nw["days"]) == "180" and nw["mode"] == "rolling",
        f"signature={inspect.signature(fn)}")

    # A2 asOf 字段
    cmp("A2", "A", "返回体带 asOf", f"asOf={AS_OF}", nw["asOf"] or "缺失",
        nw["asOf"].replace("Z", "+00:00").startswith("2026-11-14T12:00:00"),
        "契约 §2：滚动窗口上界即 asOf 时刻")

    # A3 asOfSource
    cmp("A3", "A", "返回体带 asOfSource", "cli", nw["asOfSource"] or "缺失",
        nw["asOfSource"] == "cli", "契约 §3：报告里写明 asOfSource(cli/env/clock/config)")

    # A4 同日内两个时刻
    w2, _ = call_resolve(fn, cfg_rolling, "2026-11-14T00:00:01+08:00")
    w3, _ = call_resolve(fn, cfg_rolling, "2026-11-14T23:59:59+08:00")
    n2, n3 = norm_window(w2), norm_window(w3)
    a4_ok = (day_of(n2["to"]) == "2026-11-14" and day_of(n3["to"]) == "2026-11-14"
             and day_of(n2["from"]) == "2026-05-19" and day_of(n3["from"]) == "2026-05-19")
    cmp("A4", "A", "同日 00:00:01 与 23:59:59 得到同一窗口",
        "to=2026-11-14 from=2026-05-19（两者相同）",
        f"00:00:01 → to={day_of(n2['to'])} from={day_of(n2['from'])}；23:59:59 → to={day_of(n3['to'])} from={day_of(n3['from'])}",
        a4_ok, "承接 docs §6「得到同一个 to」")

    # A5 跨日
    w4, _ = call_resolve(fn, cfg_rolling, "2026-11-13T23:59:59+08:00")
    n4 = norm_window(w4)
    cmp("A5", "A", "跨日：13T23:59:59 vs 14T00:00:01 下界差一天",
        "to=2026-11-13 from=2026-05-18",
        f"to={day_of(n4['to'])} from={day_of(n4['from'])}",
        day_of(n4["to"]) == "2026-11-13" and day_of(n4["from"]) == "2026-05-18",
        "契约 §6 跨日条；Coder 原文「14T00:00:01 与 23:59:59 下界差一天」两种读法都覆盖")

    # A6 fixed
    wf, errf = call_resolve(fn, cfg_fixed, AS_OF)
    nf = norm_window(wf)
    cmp("A6", "A", "fixed 模式回归",
        "from=2026-05-13 to=2026-11-13 mode=fixed 且保留 frozenAt",
        f"from={day_of(nf['from'])} to={day_of(nf['to'])} mode={nf['mode']} frozenAt={day_of(nf['frozenAt']) or '无'}",
        day_of(nf["from"]) == "2026-05-13" and day_of(nf["to"]) == "2026-11-13"
        and nf["mode"] == "fixed" and day_of(nf["frozenAt"]) == "2026-11-13",
        f"err={errf}" if errf else "")


# --------------------------------------------------------------------------- #
# B 组：scripts/ci/lib/data.mjs resolveWindow
# --------------------------------------------------------------------------- #
def group_b(cfg_rolling: dict, cfg_fixed: dict) -> None:
    script = """
import { resolveWindow } from "__DATA_MJS__";
const out = {};
out.rolling = resolveWindow(__CFG_ROLLING__, new Date("2026-11-14T12:00:00+08:00"));
out.rollingEarly = resolveWindow(__CFG_ROLLING__, new Date("2026-11-14T00:00:01+08:00"));
out.rollingLate = resolveWindow(__CFG_ROLLING__, new Date("2026-11-14T23:59:59+08:00"));
out.fixed = resolveWindow(__CFG_FIXED__, new Date("2026-11-14T12:00:00+08:00"));
console.log(JSON.stringify(out));
"""
    script = script.replace("__CFG_ROLLING__", json.dumps(cfg_rolling)).replace("__CFG_FIXED__", json.dumps(cfg_fixed))
    script = script.replace("__DATA_MJS__", (ROOT / "scripts" / "ci" / "lib" / "data.mjs").as_uri())
    tmp_js = TMP / "js_probe.mjs"
    tmp_js.write_text(script, encoding="utf-8")
    proc = run(["node", str(tmp_js)])
    if proc.returncode != 0:
        record("B0", "B", "node 调用 resolveWindow", "可执行", (proc.stderr or "").strip()[:200], "BLOCKED",
               "node 不可用或模块报错")
        return
    data = as_json(proc.stdout) or {}

    def g(k, *keys):
        d = data.get(k) or {}
        return {kk: str(d.get(kk) or "")[:10] for kk in keys}

    r = g("rolling", "start", "end")
    cmp("B1", "B", "data.mjs resolveWindow rolling 取值",
        "start=2026-05-19 end=2026-11-14", f"start={r['start']} end={r['end']}",
        r["start"] == "2026-05-19" and r["end"] == "2026-11-14", "scripts/ci/lib/data.mjs:36")
    has_asof = "asOf" in (data.get("rolling") or {})
    cmp("B2", "B", "data.mjs resolveWindow 返回 asOf/asOfSource",
        "带 asOf 与 asOfSource", f"keys={sorted((data.get('rolling') or {}).keys())}", has_asof,
        "契约 §4：只用 resolveWindow(config, asOf)")
    e, l = g("rollingEarly", "start", "end"), g("rollingLate", "start", "end")
    cmp("B3", "B", "同日 00:00:01 与 23:59:59 同一窗口", "两者 start/end 相同",
        f"early={e['start']}~{e['end']} late={l['start']}~{l['end']}",
        e["start"] == l["start"] and e["end"] == l["end"])
    f = g("fixed", "start", "end")
    cmp("B4", "B", "data.mjs fixed 回归", "start=2026-05-13 end=2026-11-13",
        f"start={f['start']} end={f['end']}", f["start"] == "2026-05-13" and f["end"] == "2026-11-13")


# --------------------------------------------------------------------------- #
# C 组：validate.py CLI
# --------------------------------------------------------------------------- #
def group_c(ev: dict) -> None:
    # C0 夹具自检：一条合规事件不应报窗口错
    d0 = write_fixture("c0_clean", [ev["edge"]])
    p0 = validate_on(d0, ["--max-ts", "2026-11-13T23:59:59+08:00"])
    payload0 = as_json(p0.stdout)
    if payload0 is None:
        record("C0", "C", "夹具自检（合规事件无窗口错）", "0 error", (p0.stderr or "")[:200], "BLOCKED", "validate.py 不可解析")
        return
    # 现状：没有 --as-of，默认上界 2026-11-13；edge(11-14T00:30) 会被判未来
    cmp("C0", "C", "夹具自检：11-14T00:30 事件在 --max-ts=11-13 下应报 TS_FUTURE",
        "存在 TS_FUTURE（说明夹具时间字段被真正校验）",
        short(payload0), len(hit(payload0, "TS_FUTURE")) > 0,
        "把上界钉回 11-13，验证夹具不是空壳")

    # C1 跨日 fixture：三条一起灌，看 validate 各自怎么判
    d = write_fixture("cross_day", [ev["edge"], ev["old"], ev["future"]])
    proc = validate_on(d, ["--as-of", AS_OF])
    if unsupported(proc, "--as-of"):
        record("C1", "C", "跨日 fixture：--as-of 生效（edge 合法 / old warning / future error）",
               "exit1；edge 无 finding；old 有 WINDOW_OUT(warn)；future 有 TS_FUTURE(error)",
               "validate.py 不认识 --as-of（exit 2 unrecognized arguments）", "BLOCKED",
               "契约 §3 覆盖优先级 1 需要 --as-of")
        record("C1b", "C", "跨日 fixture：ERA_AS_OF 环境变量", "同 C1", "validate.py 未读 ERA_AS_OF", "BLOCKED",
               "grep scripts/validate.py 无 ERA_AS_OF")
    else:
        payload = as_json(proc.stdout) or {}
        edge_ok = not hit(payload, "TS_FUTURE", "occurredAt") and not hit(payload, "WINDOW_OUT")
        old_warn = len(hit(payload, "WINDOW_OUT")) > 0 or (payload.get("stats", {}).get("warnings", 0) > 0)
        old_err = [f for f in by_level(payload, "error") if f.get("path", "").endswith("ev_qa_old_0518")]
        fut_err = len(hit(payload, "TS_FUTURE", "occurredAt")) > 0
        cmp("C1", "C", "跨日 fixture 三条：edge 合法 / old warning 不 error / future error",
            "exit1；old 仅 warning；future 报 TS_FUTURE；edge 干净",
            f"exit={proc.returncode} {short(payload)}",
            proc.returncode == 1 and old_warn and not old_err and fut_err,
            "--history off；fixture 见 _tmp/cross_day")

    # C2 下界精确：05-19T00:00 通过 / 05-18T23:59:59 旧
    d2 = write_fixture("lower", [ev["lower_exact"], ev["lower_minus1s"]])
    proc2 = validate_on(d2, ["--as-of", AS_OF])
    if unsupported(proc2, "--as-of"):
        record("C2", "C", "下界：from 当天 00:00 通过 / from-1s 判旧（warning）",
               "exit0；WINDOW_OUT 命中 05-18T23:59:59，且不报 error",
               "缺 --as-of，无法设 rolling 下界", "BLOCKED", "契约 §6 下界条")
    else:
        p2 = as_json(proc2.stdout) or {}
        cmp("C2", "C", "下界：from 00:00 通过 / from-1s 判旧",
            "05-19T00:00 无 finding；05-18T23:59:59 仅 WINDOW_OUT(warn)，exit0",
            f"exit={proc2.returncode} {short(p2)}",
            proc2.returncode == 0 and len(hit(p2, "WINDOW_OUT")) >= 1 and not by_level(p2, "error"),
            "旧记录只 warning 不 error（契约 §2）")

    # C3 上界：asOf 时刻通过 / +1s error
    d3 = write_fixture("upper", [ev["upper_exact"], ev["upper_plus1s"]])
    proc3 = validate_on(d3, ["--as-of", AS_OF])
    if unsupported(proc3, "--as-of"):
        record("C3", "C", "上界：asOf 通过 / asOf+1s 报 TS_FUTURE",
               "exit1；仅 12:00:01 报 TS_FUTURE", "缺 --as-of", "BLOCKED", "契约 §6 上界条")
    else:
        p3 = as_json(proc3.stdout) or {}
        cmp("C3", "C", "上界：asOf 通过 / +1s error",
            "exit1；12:00:00 干净，12:00:01 报 TS_FUTURE",
            f"exit={proc3.returncode} {short(p3)}",
            proc3.returncode == 1 and len(hit(p3, "TS_FUTURE", "occurredAt")) == 1,
            "不允许用整日上限放过 12:00 跑的 18:00 数据")

    # C4 date-only：当天合法 / 次日非法
    d4 = write_fixture("dateonly", [ev["date_only_today"], ev["date_only_next"]])
    proc4 = validate_on(d4, ["--as-of", AS_OF])
    if unsupported(proc4, "--as-of"):
        record("C4", "C", "date-only：date==to 合法 / date==to+1 非法",
               "exit1；11-14 干净，11-15 报错", "缺 --as-of", "BLOCKED", "契约 §2 未来时间条")
    else:
        p4 = as_json(proc4.stdout) or {}
        errs = [f for f in by_level(p4, "error")]
        cmp("C4", "C", "date-only：当天合法 / 次日非法",
            "11-14 无 error；11-15 有 error（TS_FUTURE）",
            f"exit={proc4.returncode} {short(p4)}",
            proc4.returncode == 1 and len(errs) >= 1,
            "")

    # C5 --max-ts 单用：压过配置
    d5 = write_fixture("maxts", [ev["upper_exact"]])  # 11-14T12:00
    proc5 = validate_on(d5, ["--max-ts", "2026-11-12T00:00:00+08:00"])
    p5 = as_json(proc5.stdout) or {}
    cmp("C5", "C", "--max-ts 生效（11-12 上限压掉 11-14 事件）",
        "exit1；TS_FUTURE 命中", f"exit={proc5.returncode} {short(p5)}",
        proc5.returncode == 1 and len(hit(p5, "TS_FUTURE")) > 0, "老参数，现状已实现（这是现状能力基线）")

    # C6 优先级：ERA_MAX_TS(11-12) + --as-of(11-14T12) → as-of 胜
    d6 = write_fixture("prio", [ev["edge"]])
    proc6 = validate_on(d6, ["--as-of", AS_OF], {"ERA_MAX_TS": "2026-11-12T00:00:00+08:00"})
    if unsupported(proc6, "--as-of"):
        record("C6", "C", "--as-of 压过 ERA_MAX_TS", "exit0（按 11-14T12 判，事件合法）",
               "缺 --as-of", "BLOCKED", "契约 §3 优先级 1 > 2")
    else:
        p6 = as_json(proc6.stdout) or {}
        w6 = (p6.get("stats") or {}).get("window") or p6.get("window") or {}
        src = w6.get("asOfSource") or ""
        up6 = str(w6.get("upper") or w6.get("asOf") or "")
        cmp("C6", "C", "--as-of 压过 ERA_MAX_TS（11-14T00:30 事件不报 TS_FUTURE）",
            "asOfSource=cli；edge 无 TS_FUTURE；window.to=2026-11-14",
            f"asOfSource={src or '未报'} to={w6.get('to')} upper={up6} "
            f"ts_future={len(hit(p6, 'TS_FUTURE'))} {short(p6)}",
            src == "cli" and w6.get("to") == "2026-11-14" and len(hit(p6, "TS_FUTURE")) == 0,
            "契约 §3 优先级 1 > 2；若被 ERA_MAX_TS 压住，这条会报 TS_FUTURE")

    # C7 rolling 默认口径：真实 content/ 全量（pipeline.config.json 已切 rolling）
    proc7 = run([sys.executable, "scripts/validate.py", "--json", "--history", "off"])
    p7 = as_json(proc7.stdout) or {}
    st = p7.get("stats") or {}
    f7 = p7.get("findings") or []
    w7 = st.get("window") or {}
    olds = [x for x in f7 if x.get("code") == "WINDOW_OUT"]
    bad = [x for x in f7 if x.get("level") == "error"]
    cmp("C7", "C", "rolling 真实 content/ 全量：旧记录只 warning、不 error",
        "exit0；errors=0；warning 全部是 WINDOW_OUT；window{from,to,mode,asOf,days,asOfSource} 齐全",
        f"exit={proc7.returncode} errors={st.get('errors')} warnings={st.get('warnings')} "
        f"old={len(olds)} errfind={len(bad)} window={json.dumps(w7, ensure_ascii=False)}",
        proc7.returncode == 0 and st.get("errors") == 0 and len(olds) == st.get("warnings")
        and not bad and w7.get("mode") == "rolling" and w7.get("from") == WIN["from"]
        and bool(w7.get("asOf")) and str(w7.get("days")) == "180" and bool(w7.get("asOfSource")),
        "契约 §2 旧记录/§5 报告字段")

    # C8 fixed 回归：真实 content/ 全量，用固定配置跑（不改仓库配置，进程内 mock）
    mock = (
        "import sys, json\n"
        "sys.path.insert(0, 'scripts')\n"
        "import _core as C\n"
        "_real = C.load_pipeline_config\n"
        "C.load_pipeline_config = lambda path=None: {**_real(path), 'window': {'mode': 'fixed', "
        "'timezone': 'Asia/Shanghai', 'frozen': {'start': '2026-05-13', 'end': '2026-11-13', "
        "'frozenAt': '2026-11-13'}}}\n"
        "import validate\n"
        "rc = validate.run(['content', '--json', '--history', 'off'])\n"
        "print('@@RC@@', rc)\n"
    )
    proc8 = run([sys.executable, "-c", mock])
    p8 = as_json(proc8.stdout) or {}
    st8 = p8.get("stats") or {}
    w8 = st8.get("window") or {}
    cmp("C8", "C", "fixed 回归：真实 content/ 全量（冻结 2026-05-13~11-13 口径）",
        "errors=0 warnings=0 records=368；window.mode=fixed from=2026-05-13 to=2026-11-13 且带 frozenAt",
        f"errors={st8.get('errors')} warnings={st8.get('warnings')} records={st8.get('records')} "
        f"window={json.dumps(w8, ensure_ascii=False)}",
        st8.get("errors") == 0 and st8.get("warnings") == 0 and st8.get("records") == 368
        and w8.get("mode") == "fixed" and w8.get("from") == "2026-05-13"
        and w8.get("to") == "2026-11-13" and w8.get("frozenAt") == "2026-11-13",
        "契约 §4 fixed 必须与冻结时一致；用工单 mock config，不改仓库配置")


# --------------------------------------------------------------------------- #
# D 组：export_site.py 的 meta.window
# --------------------------------------------------------------------------- #
def group_d(ev: dict) -> None:
    data_dir = TMP / "export_data"
    if data_dir.exists():
        shutil.rmtree(data_dir)
    data_dir.mkdir(parents=True)
    (data_dir / "events.json").write_text(json.dumps([ev["edge"], ev["old"], ev["future"]], ensure_ascii=False), encoding="utf-8")
    (data_dir / "trends.json").write_text("[]", encoding="utf-8")
    (data_dir / "forecasts.json").write_text("[]", encoding="utf-8")

    site = TMP / "export_site"
    sd = site / "src" / "data"
    sd.mkdir(parents=True, exist_ok=True)
    (sd / "meta.json").write_text(json.dumps({"schemaVersion": "site/v1"}, ensure_ascii=False), encoding="utf-8")
    for name in ("events", "trends", "forecasts", "snapshots"):
        (sd / f"{name}.json").write_text("[]", encoding="utf-8")

    out = TMP / "export_out"
    cmd = [
        sys.executable, "scripts/export_site.py",
        "--data", str(data_dir), "--site", str(site), "--out", str(out),
        "--report", str(TMP / "export_report.md"), "--dry-run", "--json",
        "--as-of", AS_OF,
    ]
    proc = run(cmd, timeout=300)
    if unsupported(proc, "--as-of"):
        record("D1", "D", "export_site 按窗口过滤 + meta.window 完整",
               "meta.window={from:2026-05-19,to:2026-11-14,mode:rolling,asOf,days:180}，rolling 下无 frozenAt",
               "export_site.py 不认识 --as-of", "BLOCKED", "契约 §4 export_site 行")
        record("D2", "D", "export 展示数组过滤越界事件",
               "old/future 不出现在导出 events", "缺 --as-of，无法指定本轮窗口", "BLOCKED", "")
        return
    payload = as_json(proc.stdout) or {}
    win = ((payload.get("meta") or {}).get("window") or payload.get("window") or {})
    got = json.dumps(win, ensure_ascii=False)
    ok = (str(win.get("from"))[:10] == WIN["from"] and str(win.get("to"))[:10] == WIN["to"]
          and win.get("mode") == "rolling" and str(win.get("days")) == "180" and "frozenAt" not in win)
    cmp("D1", "D", "export meta.window 完整且 rolling 不写 frozenAt",
        "from=2026-05-19 to=2026-11-14 mode=rolling days=180 无 frozenAt",
        f"exit={proc.returncode} window={got[:300]}", ok, "")

    exported = [e for e in (payload.get("events") or []) if isinstance(e, dict)]
    ids = [str(e.get("_v1_id") or e.get("id") or "") for e in exported]
    cmp("D2", "D", "export 展示数组按窗口过滤",
        "只留 11-14T00:30（11-15 未来剔除；05-18 旧记录不展示）",
        f"导出 {len(ids)} 条: {ids[:6]}", len(ids) == 1, "契约 §4：展示数组按窗口过滤")


# --------------------------------------------------------------------------- #
# E 组：scripts/ci/lib/window.mjs 的 judge() 边界判定（直接打契约用例）
# --------------------------------------------------------------------------- #
def group_e() -> None:
    script = """
import { resolveWindow, judge } from "__WINDOW_MJS__";
const cfg = { window: { mode: "rolling", days: 180, timezone: "Asia/Shanghai" } };
const win = resolveWindow(cfg, { asOf: "2026-11-14T12:00:00+08:00" });
const cases = __CASES__;
const out = cases.map(([id, v]) => [id, judge(v, win)]);
console.log(JSON.stringify({ win, out }));
"""
    cases = [
        ["E1", "2026-05-19T00:00:00+08:00"],
        ["E2", "2026-05-18T23:59:59+08:00"],
        ["E3", "2026-11-14T12:00:00+08:00"],
        ["E4", "2026-11-14T12:00:01+08:00"],
        ["E5", "2026-11-14"],
        ["E6", "2026-11-15"],
        ["E7", "2026-05-18"],
        ["E8", "2026-11-14T23:00:00+08:00"],
    ]
    script = script.replace("__CASES__", json.dumps(cases))
    script = script.replace("__WINDOW_MJS__", (ROOT / "scripts" / "ci" / "lib" / "window.mjs").as_uri())
    tmp_js = TMP / "js_judge.mjs"
    tmp_js.write_text(script, encoding="utf-8")
    proc = run(["node", str(tmp_js)])
    if proc.returncode != 0:
        record("E0", "E", "node 调用 judge()", "可执行", (proc.stderr or "").strip()[:300], "BLOCKED",
               "scripts/ci/lib/window.mjs")
        return
    data = as_json(proc.stdout) or {}
    got = dict(data.get("out") or [])
    win = data.get("win") or {}
    expect = {
        "E1": ("ok", "from 当天 00:00:00+08 → 通过"),
        "E2": ("old", "from-1 秒（05-18T23:59:59）→ 旧记录（warning 口径）"),
        "E3": ("ok", "asOf 时刻本身 → 通过"),
        "E4": ("future", "asOf+1 秒 → error(TS_FUTURE)"),
        "E5": ("ok", "date-only == to（2026-11-14）→ 合法"),
        "E6": ("future", "date-only == to+1（2026-11-15）→ 非法"),
        "E7": ("old", "date-only < from（2026-05-18）→ 旧记录"),
        "E8": ("future", "asOf 当天 23:00 带时刻 → future（不得用整日上限）"),
    }
    for cid, (exp, desc) in expect.items():
        act = got.get(cid, "未返回")
        cmp(cid, "E", f"judge(): {desc}", exp, str(act), act == exp,
            f"window.from={win.get('from')} to={win.get('to')} upper={win.get('upper')}")


# --------------------------------------------------------------------------- #
# F 组：scripts/ci/window.mjs（CI 单步钉窗口）
# --------------------------------------------------------------------------- #
def window_cli(args: list[str], env_extra: dict | None = None) -> dict | None:
    proc = run(["node", "scripts/ci/window.mjs", "--json", *args], env_extra)
    if proc.returncode != 0:
        return None
    return as_json(proc.stdout)


def group_f() -> None:
    # F1 rolling 基本取值
    w = window_cli(["--mode", "rolling", "--as-of", AS_OF])
    got = json.dumps(w, ensure_ascii=False) if w else "命令失败"
    ok = bool(w) and w.get("mode") == "rolling" and w.get("from") == WIN["from"] and w.get("to") == WIN["to"] \
        and str(w.get("days")) == "180" and w.get("asOfSource") == "cli"
    cmp("F1", "F", "window.mjs rolling：from/to/days/asOfSource", 
        f"mode=rolling from={WIN['from']} to={WIN['to']} days=180 asOfSource=cli", got[:300], ok,
        "scripts/ci/window.mjs")

    # F2 上界=asOf 时刻
    up_ok = bool(w) and abs(dt.datetime.fromisoformat(w["upper"].replace("Z", "+00:00")).astimezone(TZ)
                           - dt.datetime.fromisoformat(AS_OF)) < dt.timedelta(seconds=1)
    cmp("F2", "F", "window.mjs rolling：upper = asOf 时刻本身",
        "upper=2026-11-14T12:00:00+08:00", (w or {}).get("upper", "缺失"), up_ok, "契约 §2 上界定义")

    # F3 fixed 回归（显式钉 fixed）
    wf = window_cli(["--mode", "fixed"])
    gotf = json.dumps(wf, ensure_ascii=False) if wf else "命令失败"
    okf = bool(wf) and wf.get("from") == "2026-05-13" and wf.get("to") == "2026-11-13" \
        and wf.get("mode") == "fixed" and wf.get("frozenAt") == "2026-11-13"
    cmp("F3", "F", "window.mjs fixed 回归（与 2026-11-13 冻结一致）",
        "from=2026-05-13 to=2026-11-13 frozenAt=2026-11-13", gotf[:300], okf, "")

    # F4 fixed + --max-ts：上界被覆盖，日期仍取配置
    wm = window_cli(["--mode", "fixed", "--max-ts", "2026-11-12T00:00:00+08:00"])
    okm = bool(wm) and wm.get("from") == "2026-05-13" and wm.get("to") == "2026-11-13" \
        and str(wm.get("upper", "")).startswith("2026-11-11T16:00:00")
    cmp("F4", "F", "fixed + --max-ts：上界被压过、窗口日期不变",
        "from/to 仍 05-13/11-13；upper=2026-11-12T00:00+08",
        (json.dumps(wm, ensure_ascii=False) if wm else "命令失败")[:300], okm, "契约 §3")

    # F5 优先级：--as-of 压过 ERA_MAX_TS
    wp = window_cli(["--mode", "rolling", "--as-of", AS_OF],
                    {"ERA_MAX_TS": "2026-11-12T00:00:00+08:00"})
    okp = bool(wp) and wp.get("asOfSource") == "cli" and wp.get("to") == WIN["to"] \
        and str(wp.get("upper", "")).startswith("2026-11-14T04:00:00")
    cmp("F5", "F", "--as-of 压过 ERA_MAX_TS",
        "asOfSource=cli，upper=2026-11-14T12:00+08（不是 11-12）",
        (json.dumps(wp, ensure_ascii=False) if wp else "命令失败")[:300], okp, "契约 §3 优先级 1>2")

    # F6 跨日：13T23:59:59 vs 14T00:00:01 下界差一天
    w13 = window_cli(["--mode", "rolling", "--as-of", "2026-11-13T23:59:59+08:00"])
    w14 = window_cli(["--mode", "rolling", "--as-of", "2026-11-14T00:00:01+08:00"])
    ok6 = bool(w13) and bool(w14) and w13.get("to") == "2026-11-13" and w13.get("from") == "2026-05-18" \
        and w14.get("to") == "2026-11-14" and w14.get("from") == "2026-05-19"
    cmp("F6", "F", "跨日：13T23:59:59 → 05-18/11-13；14T00:00:01 → 05-19/11-14",
        "下界随日历日平移一天",
        f"13日: {w13.get('from') if w13 else '失败'}~{w13.get('to') if w13 else '失败'}；"
        f"14日: {w14.get('from') if w14 else '失败'}~{w14.get('to') if w14 else '失败'}", ok6, "契约 §6 跨日条")

    # F7 同日两时刻必须同窗
    we = window_cli(["--mode", "rolling", "--as-of", "2026-11-14T00:00:01+08:00"])
    wl = window_cli(["--mode", "rolling", "--as-of", "2026-11-14T23:59:59+08:00"])
    ok7 = bool(we) and bool(wl) and (we.get("from"), we.get("to")) == (wl.get("from"), wl.get("to")) \
        == ("2026-05-19", "2026-11-14")
    cmp("F7", "F", "同日 00:00:01 与 23:59:59 同窗（差一天只发生在跨日）",
        "两者 from=2026-05-19 to=2026-11-14",
        f"{we.get('from') if we else '失败'}~{we.get('to') if we else '失败'} / "
        f"{wl.get('from') if wl else '失败'}~{wl.get('to') if wl else '失败'}", ok7,
        "澄清 Coder 口述与 docs §6 的歧义")


    # F8 时区换算：UTC 表示的 asOf 必须按 Asia/Shanghai 落日历日
    wu = window_cli(["--mode", "rolling", "--as-of", "2026-11-13T17:00:00Z"])  # = 11-14T01:00+08
    ok8 = bool(wu) and wu.get("to") == "2026-11-14" and wu.get("from") == "2026-05-19"
    cmp("F8", "F", "asOf 用 UTC 写法（13T17:00Z）→ 按 +08 落到 11-14",
        "to=2026-11-14 from=2026-05-19",
        f"to={wu.get('to') if wu else '失败'} from={wu.get('from') if wu else '失败'}", ok8,
        "时区换算边界：UTC 17:00 = 次日 01:00+08")

    # F9 UTC 15:59:59Z = 11-13T23:59:59+08，仍属 11-13
    wu2 = window_cli(["--mode", "rolling", "--as-of", "2026-11-13T15:59:59Z"])
    ok9 = bool(wu2) and wu2.get("to") == "2026-11-13" and wu2.get("from") == "2026-05-18"
    cmp("F9", "F", "asOf=13T15:59:59Z → 仍属 11-13（前一日）",
        "to=2026-11-13 from=2026-05-18",
        f"to={wu2.get('to') if wu2 else '失败'} from={wu2.get('from') if wu2 else '失败'}", ok9,
        "与 F8 成对，验证不是简单取 UTC 日期")


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="", help="A/B/C/D 组合，如 AC")
    ap.add_argument("--strict", action="store_true", help="BLOCKED 视为失败")
    args = ap.parse_args()
    only = args.only.upper() or "ABCDEF"

    TMP.mkdir(parents=True, exist_ok=True)
    cfg_rolling = {
        "schemaVersion": "1.0.0", "timezone": "Asia/Shanghai",
        "window": {"mode": "rolling", "days": DAYS, "timezone": "Asia/Shanghai",
                   "frozen": {"start": "2026-05-13", "end": "2026-11-13", "frozenAt": "2026-11-13"}},
    }
    cfg_fixed = {
        "schemaVersion": "1.0.0", "timezone": "Asia/Shanghai",
        "window": {"mode": "fixed", "start": "2026-05-13", "end": "2026-11-13",
                   "days": 180, "frozenAt": "2026-11-13"},
    }
    ev = fixture_events()

    if "A" in only:
        group_a(cfg_rolling, cfg_fixed)
    if "B" in only:
        group_b(cfg_rolling, cfg_fixed)
    if "C" in only:
        group_c(ev)
    if "D" in only:
        group_d(ev)
    if "E" in only:
        group_e()
    if "F" in only:
        group_f()

    # 报告
    order = {"FAIL": 0, "BLOCKED": 1, "PASS": 2}
    rows = sorted(RESULTS, key=lambda r: (order.get(r["status"], 3), r["id"]))
    lines = [
        "# 窗口契约 v1 独立验证报告",
        "",
        f"- 生成时间：{dt.datetime.now(TZ).isoformat(timespec='seconds')}",
        f"- 口径：Asia/Shanghai，asOf={AS_OF}，days={DAYS}",
        f"- 契约正向窗口（独立推导）：from={WIN['from']} to={WIN['to']}",
        f"- 结果：PASS {sum(r['status']=='PASS' for r in rows)} / FAIL {sum(r['status']=='FAIL' for r in rows)} / "
        f"BLOCKED {sum(r['status']=='BLOCKED' for r in rows)}",
        "",
        "| 编号 | 组 | 用例 | 契约期望 | 实际结果 | 结论 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for r in rows:
        clean = lambda s: str(s).replace("|", "\\|").replace("\n", " ")[:300]  # noqa: E731
        lines.append(f"| {r['id']} | {r['group']} | {clean(r['desc'])} | {clean(r['expected'])} | {clean(r['actual'])} | {r['status']} |")
    lines.append("")
    if any(r["status"] == "BLOCKED" for r in rows):
        lines += [
            "## BLOCKED 说明",
            "",
            "后端代码未落地的用例标 BLOCKED（不是失败，是「还没法验」）。落地后重跑本脚本即可得到真实结论。",
            "",
        ]
    report = "\n".join(lines)
    (HERE / "report.md").write_text(report, encoding="utf-8")
    (HERE / "report.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    print(report)

    fails = sum(r["status"] == "FAIL" for r in rows) + (sum(r["status"] == "BLOCKED" for r in rows) if args.strict else 0)
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
