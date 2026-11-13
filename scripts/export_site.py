#!/usr/bin/env python3
"""ERA 数据导出：v1（content 权威根）→ 站点契约 v1（src/data 形状）。

设计约束（主管 m00302 / m00310 决策）：
  * 站点 `docs/数据契约.md` 是唯一字段标准，本脚本只做出口映射，不改站点代码。
  * 默认**不写**站点 src/data：先出暂存 `data/site/` + 对照表，人工放行后再 `--write-src`。
  * 合并策略只增不删：站点现有条目按 id 保留，本批按并集追加；同 id 冲突保留站点那条并记录。
  * ID 规则：事件 ev-YYYYMMDD-<slug>、趋势 tr-<slug>、判断 fc-YYYYMM-<slug>、快照 snap-YYYY-MM-DD。

用法：
  python3 scripts/export_site.py                    # 出暂存 data/site/ + 对照表
  python3 scripts/export_site.py --dry-run          # 只出对照表，不写文件
  python3 scripts/export_site.py --write-src        # 人工放行后写入站点 src/data（仍按只增不删合并）
  python3 scripts/export_site.py --json             # 机器可读输出（人类提示走 stderr）

退出码：0 通过 / 1 校验未通过（暂存仍会写出，便于排查）/ 2 用法或输入文件错误。
纯标准库，无第三方依赖。
"""
from __future__ import annotations

import argparse
import ast
import json
import math
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

CST = timezone(timedelta(hours=8))

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
DEFAULT_DATA = REPO / "data"
DEFAULT_OUT = REPO / "data" / "site"
DEFAULT_REPORT = REPO / "reports" / "site-export-diff.md"
DEFAULT_CONFIDENCE = REPO / "reports" / "trend-confidence.json"
DEFAULT_SITE = Path(
    "/Users/zhongjixiangyou/Documents/deepseek-harness/default-workspace/era-trend-radar"
)

SITE_FILES = ("meta.json", "events.json", "trends.json", "forecasts.json", "snapshots.json")

REGION_MAP = {
    "全球": "global",
    "中国": "china",
    "美国": "us",
    "欧洲": "europe",
    "亚洲": "asia",
    "其他": "other",
}
CATEGORY_MAP = {
    "全球宏观": "economy",
    "中国经济与产业": "economy",
    "科技与社会": "technology",
    "未来趋势": "technology",  # 站点无「未来趋势」桶，见对照表「口径说明」
}
CATEGORY_OVERRIDES = {
    # v1「未来趋势」在站点没有对应桶，必须逐条映射；默认 technology 会把能源类事件放错版块
    "ev_nrc-smr-first": "energy",
    "ev_ai-dc-power-161gw": "energy",
}
CRED_MAP = {"verified": "high", "partial": "medium", "unverified": "low"}
STAGE_CONFIDENCE = {
    "emerging": 45,
    "strengthening": 65,
    "stable": 70,
    "weakening": 40,
    "falsified": 15,
}
# currentJudgment 里的置信度措辞 → 站点 0–100（取不到再退 STAGE_CONFIDENCE）
JUDGMENT_CONFIDENCE = {
    "高": 75,
    "中高": 65,
    "中": 55,
    "中低": 45,
    "低": 35,
}
# 语义重复：v1 趋势并进站点已发布趋势（站点 id 不能改），只追加指标与反证
TREND_MERGES = {
    "tr_global-rates-rehike": "tr-fed-hike-cycle",
    "tr_cn-industrial-margin-repair": "tr-cn-profit-recovery",
}
FORECAST_STATUS = ["open", "supported", "weakened", "falsified", "unresolved"]
WEIGHTS = {
    "structural": 0.25,
    "scope": 0.20,
    "signal": 0.20,
    "novelty": 0.15,
    "timeliness": 0.10,
    "cross": 0.10,
}
DIMS = list(WEIGHTS)

EVENT_ID_RE = re.compile(r"^ev-\d{8}-[a-z0-9-]{1,40}$")
TREND_ID_RE = re.compile(r"^tr-[a-z0-9-]{1,40}$")
FORECAST_ID_RE = re.compile(r"^fc-\d{6}-[a-z0-9-]{1,40}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

USAGE_ERROR = 2
VALIDATION_ERROR = 1


class ExportError(Exception):
    """输入文件缺失/格式错误（退出码 2）。"""


# ---------------------------------------------------------------- 基础工具


def say(args, msg: str) -> None:
    """人类提示：--json 时走 stderr，避免污染 stdout。"""
    stream = sys.stderr if getattr(args, "as_json", False) else sys.stdout
    print(msg, file=stream)


def slugify(value: str) -> str:
    s = str(value or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s[:40]


def date10(value) -> str:
    """把 '2026-09-29T00:00:00+08:00' / '2026-09-29' 统一成 YYYY-MM-DD。"""
    if not value:
        return ""
    text = str(value).strip()
    m = re.match(r"^(\d{4}-\d{2}-\d{2})", text)
    return m.group(1) if m else ""


def now_iso() -> str:
    return datetime.now(CST).replace(microsecond=0).isoformat()


def js_round(value) -> int:
    """与站点（JS Math.round）一致：.5 向上取整，避免 Python 银行家舍入。"""
    return int(math.floor(float(value) + 0.5))


def fmt_total(scores: dict) -> int:
    return js_round(sum(scores[d] * WEIGHTS[d] for d in DIMS))


def load_json(path: Path):
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError as exc:
        raise ExportError(f"缺少输入文件：{path}") from exc
    except json.JSONDecodeError as exc:
        raise ExportError(f"JSON 解析失败：{path}：{exc}") from exc


def v1_list(doc, key: str) -> list:
    """v1 导出可能是数组，也可能是 {schemaVersion, events:[...]} 包装。"""
    if isinstance(doc, list):
        return doc
    if isinstance(doc, dict) and isinstance(doc.get(key), list):
        return doc[key]
    raise ExportError(f"输入不是数组也不是含 {key} 的对象")


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------- v1 → 站点映射


def map_region(raw: str) -> str:
    return REGION_MAP.get(str(raw or "").strip(), slugify(raw) or "other")


def map_category(raw: str, v1_id: str = "") -> str:
    override = CATEGORY_OVERRIDES.get(str(v1_id or "").strip())
    if override:
        return override
    return CATEGORY_MAP.get(str(raw or "").strip(), slugify(raw) or "economy")


def map_credibility(level: str) -> str:
    return CRED_MAP.get(str(level or "").strip(), "low")


def map_event_status(v1: dict, credibility: str) -> str:
    status = str(v1.get("status") or "pending").strip()
    if credibility == "low":
        return "pending"  # 契约：low 必须 pending
    if status == "published":
        return "confirmed" if credibility == "high" else "partial"
    return "pending"


def convert_scores(v1_scores: dict) -> dict:
    s = v1_scores or {}

    def clamp(v) -> int:
        try:
            return max(0, min(100, js_round(v)))
        except (TypeError, ValueError):
            return 0

    dims = {
        "structural": clamp(float(s.get("structural") or 0) * 4),
        "scope": clamp(float(s.get("scope") or 0) * 5),
        "signal": clamp(float(s.get("signal") or 0) * 5),
        "novelty": clamp(float(s.get("delta") or 0) * 100 / 15),
        "timeliness": clamp(float(s.get("timeliness") or 0) * 10),
        "cross": clamp(float(s.get("crossDomain") or 0) * 10),
    }
    # 站点契约（commit 31b7629 起）：scores.total 是当前分的唯一来源，必须等于六维加权
    dims["total"] = fmt_total(dims)
    return dims


def convert_source(src: dict) -> dict:
    out = {
        "name": str(src.get("publisher") or src.get("title") or "").strip()[:80],
        "url": str(src.get("url") or "").strip(),
    }
    d = date10(src.get("publishedAt"))
    if d:
        out["date"] = d
    if str(src.get("type") or "").strip() == "primary":
        out["official"] = True
    return out


def convert_event(ev: dict, notes: list, trend_id_map: dict | None = None) -> dict:
    ev_id = str(ev.get("id") or "").strip()
    slug = slugify(ev_id[3:] if ev_id.startswith("ev_") else ev_id)
    occurred = date10(ev.get("occurredAt"))
    if not occurred:
        raise ExportError(f"事件 {ev_id} 缺少 occurredAt，无法生成 ev-YYYYMMDD-<slug>")
    out_id = f"ev-{occurred.replace('-', '')}-{slug}"

    scores = convert_scores(ev.get("scores") or {})
    subscribed = fmt_total(scores)
    credibility = map_credibility((ev.get("credibility") or {}).get("level"))
    status = map_event_status(ev, credibility)

    title = str(ev.get("title") or "").strip()
    if len(title) > 42:
        notes.append(f"{out_id}：title {len(title)} 字 > 契约 42 字，已截断")
        title = title[:42]

    summary = str(ev.get("summary") or "").strip()
    if len(summary) < 80:
        notes.append(f"{out_id}：summary {len(summary)} 字 < 契约建议 80–120 字（未改内容，留待人工补写）")

    source_list = [convert_source(s) for s in (ev.get("sources") or [])]
    trend_links = []
    for link in ev.get("trendLinks") or []:
        tid = str(link.get("trendId") or "").strip()
        site_tid = (trend_id_map or {}).get(tid)
        if not site_tid:
            site_tid = f"tr-{slugify(tid[3:] if tid.startswith('tr_') else tid)}"
        trend_links.append(
            {
                "id": site_tid,
                "effect": str(link.get("direction") or "support"),
                "reason": str(link.get("reason") or "").strip(),
            }
        )

    evaluated_category = map_category(ev.get("category"), ev_id)
    if str(ev.get("category") or "").strip() == "未来趋势" and ev_id not in CATEGORY_OVERRIDES:
        notes.append(
            f"{out_id}：v1 类目「未来趋势」无站点对应桶，且未在 CATEGORY_OVERRIDES 逐条映射，暂按 technology 输出，需人工确认"
        )

    collected = date10(ev.get("collectedAt"))
    out = {
        "id": out_id,
        "title": title,
        "date": occurred,
        "reportedAt": date10(ev.get("firstReportedAt")) or occurred,
        "collectedAt": collected,
        "region": map_region(ev.get("region")),
        "category": evaluated_category,
        "summary": summary,
        "facts": [str(x).strip() for x in (ev.get("facts") or []) if str(x).strip()],
        "scores": scores,
        "credibility": credibility,
        "status": status,
        "sources": source_list,
        "trends": trend_links,
        "scoreHistory": [
            {
                "date": collected or occurred,
                "total": subscribed,
                "reason": "由 v1 六维换算：structural×4 / scope×5 / signal×5 / delta×6.67 / timeliness×10 / crossDomain×10",
            }
        ],
    }
    retro = ev.get("retro") or {}
    if retro.get("isRetro"):
        out["backfill"] = True
    for dropped in ("analysis", "dedupeKey", "revisions", "forecastRefs", "display"):
        if ev.get(dropped):
            notes.append(f"{out_id}：v1 字段 {dropped} 在站点契约中无落点（内容未导出）")
    return out


def parse_indicator(raw) -> str:
    """v1 指标可能是字符串，也可能是被字符串化的 dict：{'name','note'}。"""
    text = str(raw or "").strip()
    if text.startswith("{") and ("'name'" in text or '"name"' in text):
        try:
            obj = ast.literal_eval(text)
        except (ValueError, SyntaxError):
            return text
        if isinstance(obj, dict):
            name = str(obj.get("name") or "").strip()
            note = str(obj.get("note") or "").strip()
            return f"{name}（{note}）" if name and note else (name or note or text)
    return text


def trend_domain(trend: dict) -> str:
    slug = slugify(str(trend.get("id") or "").replace("tr_", ""))
    hay = slug + " " + str(trend.get("thesis") or "") + str(trend.get("currentJudgment") or "")
    if slug.startswith("cn-"):
        return "china"
    if slug.startswith("global-"):
        return "global"
    if any(k in hay for k in ("气候", "粮", "人口", "结婚", "出生", "社会", "就业", "通胀")):
        return "society"
    if any(k in hay for k in ("AI", "算力", "芯片", "技术", "电力", "SMR", "核电")):
        return "technology"
    return "industry"


def trend_site_id(v1_id: str) -> str:
    """v1 趋势 id → 站点 id；语义重复的合并到站点已发布 id。"""
    tid = str(v1_id or "").strip()
    if tid in TREND_MERGES:
        return TREND_MERGES[tid]
    return f"tr-{slugify(tid[3:] if tid.startswith('tr_') else tid)}"


def judgment_confidence(judgment: str):
    """站点 confidence 从作者判断里的措辞取：高 75 / 中高 65 / 中 55 / 中低 45 / 低 35。"""
    text = str(judgment or "")
    hits = re.findall(r"置信度[：:\s]*(中高|中低|高|中|低)", text)
    if not hits:
        hits = re.findall(r"(中高|中低|高|中|低)\s*置信度", text)
    for hit in hits:
        if hit in JUDGMENT_CONFIDENCE:
            return JUDGMENT_CONFIDENCE[hit], f"判断原文「{hit}」"
    return None, ""


def counter_tail(trend: dict, file_slug: str) -> str:
    """反证在站点无字段：按主管口径写进 judgment 末尾，不静默丢。"""
    items = trend.get("counterEvidence") or []
    texts = []
    for item in items:
        txt = str(item.get("text") if isinstance(item, dict) else item or "").strip()
        if txt:
            texts.append(txt)
    if not texts:
        return ""
    shown = "；".join(texts[:3])
    return f"\n\n反证：{shown}（共 {len(texts)} 条，见 content/trends/{file_slug}.json）"


def plus_one_day(date_text: str) -> str:
    try:
        return (datetime.strptime(date_text, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    except ValueError:
        return date_text


def build_confidence_chain(
    first_seen: str, stage: str, confidence: int, revisions: list, conf_source: str
) -> list:
    """站点置信度链：首条 from=null、日期严格递增、末条 to=confidence；置信度没变不写空转。"""
    seq = [
        {
            "date": first_seen,
            "from": None,
            "to": confidence,
            "reason": f"首次建立（stage={stage}；confidence {confidence} 取自{conf_source}）",
        }
    ]
    for rev in sorted(revisions or [], key=lambda r: str(r.get("at") or "")):
        rdate = date10(rev.get("at"))
        if not rdate or rdate <= seq[-1]["date"]:
            continue
        value = STAGE_CONFIDENCE.get(str(rev.get("stageBefore") or "").strip(), confidence)
        if value == seq[-1]["to"]:
            continue  # 置信度没变：不生成 from=X to=X 的空转条目
        seq.append(
            {
                "date": rdate,
                "from": seq[-1]["to"],
                "to": value,
                "reason": str(rev.get("note") or "阶段变更").strip()[:400],
            }
        )
    if seq[-1]["to"] != confidence:
        seq.append(
            {
                "date": plus_one_day(seq[-1]["date"]),
                "from": seq[-1]["to"],
                "to": confidence,
                "reason": "对齐当前置信度",
            }
        )
    return seq


def load_confidence(path: Path) -> dict:
    """读审稿给的人工置信度：{v1_id: {confidence, level}}。缺文件返回空 dict。"""
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ExportError(f"人工置信度文件不是合法 JSON：{path}: {exc}")
    conf = raw.get("confidence") or {}
    details = raw.get("details") or {}
    out = {}
    for key, val in conf.items():
        if isinstance(val, int) and 0 <= val <= 100:
            out[str(key)] = {"confidence": val, "level": str((details.get(key) or {}).get("level") or "")}
    return out


def convert_trend(tr: dict, notes: list, manual: dict | None = None) -> dict:
    tr_id = str(tr.get("id") or "").strip()
    out_id = trend_site_id(tr_id)
    merged_into = TREND_MERGES.get(tr_id)
    stage = str(tr.get("stage") or "emerging").strip()
    first_seen = date10(tr.get("firstSeenAt"))
    if not first_seen:
        raise ExportError(f"趋势 {tr_id} 缺少 firstSeenAt")

    raw_judgment = str(tr.get("currentJudgment") or "").strip()
    conf_text, conf_source = judgment_confidence(raw_judgment)
    if manual and isinstance(manual.get("confidence"), int):
        confidence = manual["confidence"]
        level = str(manual.get("level") or "").strip()
        conf_source = f"人工（reports/trend-confidence.json：{level}）" if level else "人工（reports/trend-confidence.json）"
    elif conf_text is None:
        confidence = STAGE_CONFIDENCE.get(stage, 50)
        conf_source = f"stage 映射（judgment 未写置信度；{stage}={confidence}）"
    else:
        confidence = conf_text

    thesis = str(tr.get("thesis") or "").strip()
    title = str(tr.get("title") or "").strip() or thesis
    if len(title) > 42:
        title = title[:42]
        notes.append(f"{out_id}：站点 title 由 thesis 截断生成（{len(thesis)} 字 → 42 字），建议人工补短标题")


    # 站点 revisions 是「置信度链」：首条 from=null，后续 from=上一条 to，末条 to=confidence
    seq = build_confidence_chain(first_seen, stage, confidence, tr.get("revisions") or [], conf_source)

    indicators = [parse_indicator(x) for x in (tr.get("indicators") or [])]
    indicators = [x for x in indicators if x]
    if not indicators:
        notes.append(f"{out_id}：indicators 为空，站点校验会失败")

    tail = counter_tail(tr, tr_id)
    out = {
        "id": out_id,
        "title": title,
        "thesis": thesis,
        "domain": trend_domain(tr),
        "firstSeen": first_seen,
        "status": stage,
        "confidence": confidence,
        "indicators": indicators,
        "judgment": raw_judgment + tail,
        "revisions": seq,
        "_v1_id": tr_id,
        "_confidence_source": conf_source,
        "_counter_tail": tail,
    }
    verify_by = date10(tr.get("reviewBy"))
    if verify_by:
        out["verifyBy"] = verify_by
    if merged_into:
        out["_merged_into"] = merged_into
        notes.append(f"{out_id}：v1 趋势 {tr_id} 与站点已发布趋势语义重复，合并进 {out_id}，不新增条目")
    return out


def convert_forecast(fc: dict, event_titles: dict, notes: list, trend_id_map: dict | None = None) -> dict:
    fc_id = str(fc.get("id") or "").strip()
    slug = slugify(fc_id[3:] if fc_id.startswith("fc_") else fc_id)
    made_at = date10(fc.get("createdAt"))
    if not made_at:
        raise ExportError(f"判断 {fc_id} 缺少 createdAt")
    out_id = f"fc-{made_at[:7].replace('-', '')}-{slug}"
    confidence = max(0, min(100, js_round(float(fc.get("probability") or 0) * 100)))

    v1_status = str(fc.get("status") or "open").strip()
    resolution = fc.get("resolution") or {}
    outcome = str(resolution.get("outcome") or "").strip()
    if v1_status == "resolved":
        status = {"hit": "supported", "partial": "weakened", "miss": "falsified", "unresolved": "unresolved"}.get(
            outcome, "unresolved"
        )
    elif v1_status == "void":
        status = "unresolved"
        notes.append(f"{out_id}：v1 status=void 无对应站点状态，映射为 unresolved")
    else:
        status = "open" if v1_status == "open" else v1_status
    if status not in FORECAST_STATUS:
        status = "open"

    support = []
    for ref in fc.get("evidenceRefs") or []:
        eid = str(ref or "").strip()
        support.append(event_titles.get(eid) or eid)

    linked = [str(x) for x in (fc.get("linkedTrendIds") or []) if str(x).strip()]
    trend_id = None
    if linked:
        trend_id = (trend_id_map or {}).get(linked[0]) or f"tr-{slugify(linked[0][3:] if linked[0].startswith('tr_') else linked[0])}"

    history = []
    for rev in fc.get("revisions") or []:
        rdate = date10(rev.get("at"))
        if not rdate:
            continue
        history.append(
            {
                "date": rdate,
                "confidence": confidence,
                "reason": str(rev.get("note") or "更新").strip()[:200],
            }
        )
    if not history:
        history.append({"date": made_at, "confidence": confidence, "reason": "首次提出"})
    # 末条必须与 confidence 一致（站点校验 CHAIN）
    history[-1]["confidence"] = confidence

    out = {
        "id": out_id,
        "claim": str(fc.get("statement") or "").strip(),
        "madeAt": made_at,
        "dueBy": date10(fc.get("resolveBy")),
        "confidence": confidence,
        "status": status,
        "support": support,
        "counter": [],
        "history": history,
    }
    if trend_id:
        out["trendId"] = trend_id
    if fc.get("indicators"):
        notes.append(f"{out_id}：v1 indicators 在站点契约中无字段（站点把证据写在 support/counter）")
    return out


# ---------------------------------------------------------------- 合并与校验


def load_site(site: Path) -> dict:
    data_dir = site / "src" / "data"
    if not data_dir.is_dir():
        raise ExportError(f"站点数据目录不存在：{data_dir}")
    missing = [f for f in SITE_FILES if not (data_dir / f).exists()]
    if missing:
        raise ExportError(f"站点 src/data 缺少文件：{', '.join(missing)}")
    return {
        "dir": data_dir,
        "meta": load_json(data_dir / "meta.json"),
        "events": load_json(data_dir / "events.json"),
        "trends": load_json(data_dir / "trends.json"),
        "forecasts": load_json(data_dir / "forecasts.json"),
        "snapshots": load_json(data_dir / "snapshots.json"),
    }


def merge_append_only(site_items: list, new_items: list, conflicts: list, kind: str) -> list:
    seen = {str(x.get("id")) for x in site_items}
    merged = list(site_items)
    for item in new_items:
        iid = str(item.get("id"))
        if iid in seen:
            conflicts.append(f"{kind}：{iid} 已存在于站点，保留站点版本，本批同 id 未写入")
            continue
        seen.add(iid)
        merged.append(item)
    return merged


def absorb_trends(site_trends: list, absorbed: dict, notes: list) -> tuple:
    """语义重复趋势并入站点已发布条目：只加指标与反证段，不动站点置信度与 revisions。"""
    by_id = {str(t.get("id")): t for t in site_trends}
    log: list = []
    for target_id, item in absorbed.items():
        base = by_id.get(target_id)
        if base is None:
            raise ExportError(f"趋势合并目标 {target_id} 不在站点数据里")
        added = 0
        existing = {str(x) for x in (base.get("indicators") or [])}
        for ind in item.get("indicators") or []:
            if ind and ind not in existing:
                base.setdefault("indicators", []).append(ind)
                existing.add(ind)
                added += 1
        tail = str(item.get("_counter_tail") or "")
        if tail and tail not in str(base.get("judgment") or ""):
            base["judgment"] = (str(base.get("judgment") or "").rstrip() + tail).strip()
        log.append(
            {
                "v1_id": item.get("_v1_id"),
                "target": target_id,
                "indicators_added": added,
                "counter_added": bool(tail),
                "site_confidence": base.get("confidence"),
                "v1_confidence": item.get("confidence"),
            }
        )
        notes.append(
            f"{target_id}：已吸收 {item.get('_v1_id')} 的 {added} 条指标"
            + ("与反证段" if tail else "")
            + f"；置信度沿用站点已发布 {base.get('confidence')}"
        )
    return site_trends, log


def check_ids(items: list, regex, path: str, errors: list) -> None:
    seen = set()
    for i, item in enumerate(items):
        iid = str(item.get("id") or "")
        if not regex.match(iid):
            errors.append(f"{path}[{i}].id: 格式不符：{iid}")
        if iid in seen:
            errors.append(f"{path}[{i}].id: 重复：{iid}")
        seen.add(iid)


def validate(site_like: dict) -> list:
    """镜像站点 scripts/validate.mjs 的关键规则，落盘前先自检。"""
    errors: list = []
    events = site_like["events"]
    trends = site_like["trends"]
    forecasts = site_like["forecasts"]
    snapshots = site_like["snapshots"]
    meta = site_like["meta"]

    for name, arr in (("events", events), ("trends", trends), ("forecasts", forecasts), ("snapshots", snapshots)):
        if not isinstance(arr, list) or not arr:
            errors.append(f"{name}: 必须是非空数组")

    check_ids(events, EVENT_ID_RE, "events", errors)
    check_ids(trends, TREND_ID_RE, "trends", errors)
    check_ids(forecasts, FORECAST_ID_RE, "forecasts", errors)
    check_ids(snapshots, re.compile(r"^snap-\d{4}-\d{2}-\d{2}$"), "snapshots", errors)

    trend_ids = {str(t.get("id")) for t in trends}
    event_ids = {str(e.get("id")) for e in events}

    for i, e in enumerate(events):
        p = f"events[{i}]"
        if not str(e.get("title") or "").strip():
            errors.append(f"{p}.title: 不能为空")
        if len(str(e.get("title") or "")) > 42:
            errors.append(f"{p}.title: 超过 42 字（{len(str(e.get('title')))}）")
        for k in ("date", "reportedAt"):
            if not DATE_RE.match(str(e.get(k) or "")):
                errors.append(f"{p}.{k}: 不是 YYYY-MM-DD")
        if not DATE_RE.match(str(e.get("collectedAt") or "")):
            errors.append(f"{p}.collectedAt: 不是 YYYY-MM-DD")
        if not (isinstance(e.get("scores"), dict) and set(DIMS) <= set(e["scores"])):
            errors.append(f"{p}.scores: 缺六维")
        else:
            for d in DIMS:
                v = e["scores"][d]
                if not isinstance(v, int) or not 0 <= v <= 100:
                    errors.append(f"{p}.scores.{d}: 必须是 0–100 整数")
            total = e["scores"].get("total")
            if not isinstance(total, int) or not 0 <= total <= 100:
                errors.append(f"{p}.scores.total: 必须是 0–100 整数（当前分唯一来源）")
            elif total != fmt_total(e["scores"]):
                errors.append(f"{p}.scores.total: {total} ≠ 六维加权 {fmt_total(e['scores'])}")
            history = e.get("scoreHistory") or []
            if not history:
                errors.append(f"{p}.scoreHistory: 不能为空")
            else:
                last = history[-1]
                if not DATE_RE.match(str(last.get("date") or "")):
                    errors.append(f"{p}.scoreHistory[last].date: 格式错误")
                expected = fmt_total(e["scores"])
                if last.get("total") != expected:
                    errors.append(f"{p}.scoreHistory[last].total: {last.get('total')} ≠ 公式 {expected}")
        if e.get("credibility") not in ("high", "medium", "low"):
            errors.append(f"{p}.credibility: 非法")
        if e.get("status") not in ("confirmed", "partial", "pending"):
            errors.append(f"{p}.status: 非法")
        if e.get("credibility") == "low" and e.get("status") != "pending":
            errors.append(f"{p}.status: credibility=low 必须 pending")
        if not isinstance(e.get("facts"), list) or len(e["facts"]) < 2:
            errors.append(f"{p}.facts: 至少 2 条")
        if not isinstance(e.get("sources"), list) or not e["sources"]:
            errors.append(f"{p}.sources: 至少 1 个")
        for j, s in enumerate(e.get("sources") or []):
            if not re.match(r"^https?://\S+$", str(s.get("url") or ""), re.I):
                errors.append(f"{p}.sources[{j}].url: 必须是 http(s)")
            if not str(s.get("name") or "").strip():
                errors.append(f"{p}.sources[{j}].name: 不能为空")
            if s.get("date") and not DATE_RE.match(str(s["date"])):
                errors.append(f"{p}.sources[{j}].date: 格式错误")
        for j, t in enumerate(e.get("trends") or []):
            if t.get("id") not in trend_ids:
                errors.append(f"{p}.trends[{j}].id: 悬空趋势 {t.get('id')}")
            if t.get("effect") not in ("support", "weaken", "challenge"):
                errors.append(f"{p}.trends[{j}].effect: 非法")
        if meta.get("window", {}).get("from") and str(e.get("date")) < meta["window"]["from"]:
            errors.append(f"{p}.date: 早于窗口 {meta['window']['from']}")
        if meta.get("window", {}).get("to") and str(e.get("date")) > meta["window"]["to"]:
            errors.append(f"{p}.date: 晚于窗口 {meta['window']['to']}")

    for i, t in enumerate(trends):
        p = f"trends[{i}]"
        for k in ("title", "thesis", "judgment"):
            if not str(t.get(k) or "").strip():
                errors.append(f"{p}.{k}: 不能为空")
        if not DATE_RE.match(str(t.get("firstSeen") or "")):
            errors.append(f"{p}.firstSeen: 格式错误")
        if t.get("status") not in ("emerging", "strengthening", "stable", "weakening", "falsified"):
            errors.append(f"{p}.status: 非法")
        if not isinstance(t.get("confidence"), int) or not 0 <= t["confidence"] <= 100:
            errors.append(f"{p}.confidence: 必须是 0–100 整数")
        if not isinstance(t.get("indicators"), list) or not t["indicators"]:
            errors.append(f"{p}.indicators: 至少 1 个")
        revs = t.get("revisions") or []
        if not revs:
            errors.append(f"{p}.revisions: 不能为空")
        else:
            for j, r in enumerate(revs):
                if not DATE_RE.match(str(r.get("date") or "")):
                    errors.append(f"{p}.revisions[{j}].date: 格式错误")
                if not isinstance(r.get("to"), int) or not 0 <= r["to"] <= 100:
                    errors.append(f"{p}.revisions[{j}].to: 必须是 0–100 整数")
                if j > 0 and r.get("from") != revs[j - 1].get("to"):
                    errors.append(f"{p}.revisions[{j}].from: 与上一条 to 不连续")
                if j == 0 and r.get("from") is not None:
                    errors.append(f"{p}.revisions[0].from: 必须为 null")
            if revs[-1].get("to") != t.get("confidence"):
                errors.append(f"{p}.confidence: 与最后一条修订不一致")

    for i, f in enumerate(forecasts):
        p = f"forecasts[{i}]"
        if not str(f.get("claim") or "").strip():
            errors.append(f"{p}.claim: 不能为空")
        for k in ("madeAt", "dueBy"):
            if not DATE_RE.match(str(f.get(k) or "")):
                errors.append(f"{p}.{k}: 格式错误")
        if str(f.get("dueBy") or "") < str(f.get("madeAt") or ""):
            errors.append(f"{p}.dueBy: 早于 madeAt")
        if f.get("status") not in FORECAST_STATUS:
            errors.append(f"{p}.status: 非法")
        if not isinstance(f.get("confidence"), int) or not 0 <= f["confidence"] <= 100:
            errors.append(f"{p}.confidence: 必须是 0–100 整数")
        if f.get("trendId") and f["trendId"] not in trend_ids:
            errors.append(f"{p}.trendId: 悬空趋势 {f['trendId']}")
        history = f.get("history") or []
        if not history:
            errors.append(f"{p}.history: 不能为空")
        elif history[-1].get("confidence") != f.get("confidence"):
            errors.append(f"{p}.confidence: 与 history 末条不一致")
        if not isinstance(f.get("support"), list):
            errors.append(f"{p}.support: 必须是数组")
        if not isinstance(f.get("counter"), list):
            errors.append(f"{p}.counter: 必须是数组")

    for i, s in enumerate(snapshots):
        p = f"snapshots[{i}]"
        if not DATE_RE.match(str(s.get("date") or "")):
            errors.append(f"{p}.date: 格式错误")
        if s.get("period") not in ("weekly", "monthly"):
            errors.append(f"{p}.period: 非法")
        if not str(s.get("title") or "").strip():
            errors.append(f"{p}.title: 不能为空")
        for j, eid in enumerate(s.get("eventIds") or []):
            if eid not in event_ids:
                errors.append(f"{p}.eventIds[{j}]: 悬空事件 {eid}")

    counts = meta.get("counts") or {}
    if counts.get("events") != len(events):
        errors.append("meta.counts.events: 与实际不符")
    if counts.get("trends") != len(trends):
        errors.append("meta.counts.trends: 与实际不符")
    if counts.get("forecasts") != len(forecasts):
        errors.append("meta.counts.forecasts: 与实际不符")
    return errors


# ---------------------------------------------------------------- 字段级对照


def keys_of(items: list, nested: str | None = None) -> set:
    out = set()
    for item in items:
        if nested:
            for sub in item.get(nested) or []:
                if isinstance(sub, dict):
                    out |= set(sub)
        else:
            out |= set(item or {})
    return out


def field_diff(site_items: list, new_items: list) -> dict:
    """返回站点字段/本次字段的交叉差异（含一层嵌套）。"""
    site_keys, new_keys = keys_of(site_items), keys_of(new_items)
    report = {
        "top": {
            "site_only": sorted(site_keys - new_keys),
            "export_only": sorted(new_keys - site_keys),
        }
    }
    for nested in ("sources", "trends", "scores", "revisions", "history", "scoreHistory"):
        s, n = keys_of(site_items, nested), keys_of(new_items, nested)
        if s or n:
            report[nested] = {
                "site_only": sorted(s - n),
                "export_only": sorted(n - s),
            }
    return report


# ---------------------------------------------------------------- 报告


def render_report(ctx: dict) -> str:
    lines: list = []
    add = lines.append
    add("# 站点导出对照表（v1 → 契约 v1）")
    add("")
    add(f"- 生成时间：{ctx['generatedAt']}")
    if ctx.get("contentDigest"):
        add(f"- 内容快照：content@{ctx['contentDigest']}")
    add(f"- 输入（v1）：`{ctx['data_dir']}`")
    add(f"- 站点基线：`{ctx['site_dir']}`（只读）")
    add(f"- 输出：`{ctx['out_dir']}`" + ("（已另写 src/data）" if ctx["wrote_src"] else "（暂存，未写 src/data）"))
    add("")
    add("## 一、数量概览")
    add("")
    add("| 类型 | 站点现有 | 本次新增 | 合并后 | 同 id 冲突 |")
    add("|---|---|---|---|---|")
    for kind in ("events", "trends", "forecasts"):
        stat = ctx["counts"][kind]
        add(f"| {kind} | {stat['site']} | {stat['new']} | {stat['merged']} | {stat['conflicts']} |")
    add(f"| snapshots | {ctx['snapshots']} | 0（原样沿用） | {ctx['snapshots']} | 0 |")
    add("")
    add("## 二、本批新增条目")
    add("")
    for kind, label in (("events", "事件"), ("trends", "趋势"), ("forecasts", "判断")):
        add(f"### {label}（{len(ctx['new'][kind])}）")
        add("")
        if not ctx["new"][kind]:
            add("- 无")
        for item in ctx["new"][kind]:
            add(f"- `{item['id']}` {item.get('title') or item.get('claim') or ''}")
        add("")
    add("## 三、若不合并、直接写 src/data 会被覆盖的条目")
    add("")
    add(f"当前合并策略为「只增不删」，以下站点现有 {len(ctx['overwrite'])} 条均保留、不覆盖；`--no-merge` 仅用于对照。")
    add("")
    for iid in ctx["overwrite"]:
        add(f"- `{iid}`")
    add("")
    add("## 四、字段级差异（站点现有 vs 本批新增）")
    add("")
    if not ctx["diffs"]:
        add("- 无差异")
    for kind, diff in ctx["diffs"].items():
        add(f"### {kind}")
        add("")
        add("| 层级 | 站点有 / 本导出无 | 本导出有 / 站点无 |")
        add("|---|---|---|")
        for level, pair in diff.items():
            so = "、".join(pair["site_only"]) or "—"
            eo = "、".join(pair["export_only"]) or "—"
            add(f"| {level} | {so} | {eo} |")
        add("")
    add("说明：`metrics` / `timeline` / `watch` 是站点可选字段，本期 v1 无对应数据；`backfill` 仅在 v1 `retro.isRetro=true` 时输出。其余层级未列差异即完全一致。")
    add("")
    add("## 五、派生字段与换算口径（需人工确认）")
    add("")
    add("- **id**：`ev_<slug>` + occurredAt → `ev-YYYYMMDD-<slug>`；`tr_<slug>` → `tr-<slug>`；`fc_<slug>` + createdAt → `fc-YYYYMM-<slug>`。")
    add("- **scores**：v1 六维（25/20/20/15/10/10 分制）线性放大到 0–100——structural×4、scope×5、signal×5、delta×6.67、timeliness×10、crossDomain×10，取整；**`scores.total` 是站点当前分的唯一来源**（commit 31b7629 起硬校验），按六维加权取整写入，scoreHistory 末条与之相同。")
    add("- **scoreHistory**：站点要求非空且末条 total 等于六维加权公式；本期按换算后的六维重算，写入一条 dated 事件收录日。")
    add("- **credibility**：verified→high、partial→medium、unverified→low；**status**：published 且 high→confirmed、published 且 medium→partial、其余→pending（low 强制 pending）。")
    add("- **趋势 title**：v1 无 title 字段，取 thesis 前 42 字；**domain**：按 id 前缀（cn-/global-）与关键词推断。")
    add("- **趋势 confidence**：从 `currentJudgment` 的措辞取（高 75 / 中高 65 / 中 55 / 中低 45 / 低 35），取不到再退 stage（45/65/70/40/15）。合并进站点的那两条沿用站点已发布值，不覆盖。")
    add("- **趋势 revisions**：只写置信度真实变化的节点；首条 from=null、末条 to=confidence，`from==to` 的空转不写。")
    add("- **反证**：站点无 counterEvidence 字段，追加到 `judgment` 末尾「反证：…（n 条，见 content/trends/<id>.json）」，不静默丢。")
    add("- **趋势合并**：语义重复的 v1 趋势并入站点已发布 id（id 不改），只追加指标与反证段。")
    add("- **判断**：`statement→claim`、`createdAt→madeAt`、`resolveBy→dueBy`、`probability×100→confidence`；`resolution.outcome` hit→supported / partial→weakened / miss→falsified / unresolved→unresolved；`void`→unresolved。`counter` 站点要求数组，v1 无对应字段，暂为空数组。")
    add("")
    add("### 趋势合并明细")
    add("")
    if ctx.get("trend_merges"):
        add("| v1 趋势 | 并入站点 | 新增指标 | 反证段 | 置信度 |")
        add("|---|---|---|---|---|")
        for row in ctx["trend_merges"]:
            add(
                f"| `{row['v1_id']}` | `{row['target']}` | {row['indicators_added']} | "
                f"{'是' if row['counter_added'] else '否'} | 站点 {row['site_confidence']}（v1 口径 {row['v1_confidence']}，不覆盖） |"
            )
    else:
        add("- 无（本轮无合并）")
    add("")
    add("### 趋势置信度来源")
    add("")
    if ctx.get("confidence_rows"):
        add("| 站点 id | confidence | 来源 |")
        add("|---|---|---|")
        for row in ctx["confidence_rows"]:
            add(f"| `{row['id']}` | {row['value']} | {row['source']} |")
    else:
        add("- 无新增趋势")
    add("")
    add("")
    add("## 六、内容与契约缺口（未自动改，等人工）")
    add("")
    add("### 未导出字段清单（v1 有、站点契约无落点）")
    add("")
    add("| 层 | v1 字段 | 站点契约 | 处理 |")
    add("|---|---|---|---|")
    add("| event | `analysis` | 无对应字段 | 不导出（长文判断留在 v1，站点只放 summary + facts） |")
    add("| event | `dedupeKey` | 无 | 不导出（内部去重键，不外泄） |")
    add("| event | `display` | 无 | 不导出（站点按 status / scores.total 自己算门禁） |")
    add("| event | `metrics` | `EraEvent.metrics` 存在 | **暂不导出**：v1 schema 未收编（validate 报未知字段），内容侧保留，等后端决定收编或移出 |")
    add("| trend | `evidenceRefs` | 无 | 不导出；站点通过 `events[].trends[]` 反查关联事件 |")
    add("| trend | `counterEvidence` | 无 | 已写进 `judgment` 末尾「反证：…」段，不丢 |")
    add("| forecast | `indicators` | 无 | 不导出；站点证据位只有 `support` / `counter` |")
    add("| forecast | `resolution.note` / `evidenceUrls` | 无 | 本批 2 条均为 open，未出现；resolve 后需人工定落点 |")
    add("")
    add("### 逐条提示")
    add("")
    if ctx["notes"]:
        for n in ctx["notes"]:
            add(f"- {n}")
    else:
        add("- 无")
    add("")
    add("## 七、站点校验")
    add("")
    if ctx["errors"]:
        add(f"自检未通过，{len(ctx['errors'])} 条：")
        add("")
        for e in ctx["errors"]:
            add(f"- {e}")
    else:
        add("- 自检通过（镜像 `scripts/validate.mjs` 规则）；建议再跑一次真实校验：")
        add("")
        add("```bash")
        add(f"cd {ctx['site_dir']} && node scripts/validate.mjs --data {ctx['out_dir']}")
        add("```")
    add("")
    return "\n".join(lines) + "\n"



# ---------------------------------------------------------------- 文本保形写盘（只增不删）


def _compact(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(", ", ": "))


def _append_blocks(text: str, items: list) -> str:
    """在 JSON 数组末尾追加元素，保留原文其余字节。"""
    if not items:
        return text
    lines = text.rstrip("\n").split("\n")
    close = max(i for i, l in enumerate(lines) if l.rstrip() == "]")
    head = lines[:close]
    while head and head[-1].strip() == "":
        head.pop()
    if head and head[-1].rstrip().endswith("}"):
        head[-1] = head[-1].rstrip() + ","
    for k, obj in enumerate(items):
        for line in json.dumps(obj, ensure_ascii=False, indent=2).split("\n"):
            head.append("  " + line)
        if k < len(items) - 1:
            head[-1] += ","
    return "\n".join(head + ["]"]) + "\n"


def _entry_bounds(lines: list, entry_id: str):
    id_line = None
    for i, l in enumerate(lines):
        if l.strip().startswith(f'"id": "{entry_id}"'):
            id_line = i
            break
    if id_line is None:
        return None
    start = id_line
    while start > 0 and lines[start] != "  {":
        start -= 1
    end = id_line
    while end < len(lines) and lines[end] != "  }":
        end += 1
    if lines[start] != "  {" or end >= len(lines):
        return None
    return start, end


def _patch_trend_entry(block: list, new: dict) -> list:
    """只改 indicators / judgment / revisions 三者，其余行不动。"""
    out = list(block)
    for i, l in enumerate(out):
        s = l.strip()
        if s.startswith('"indicators":'):
            suffix = "," if s.endswith(",") else ""
            out[i] = '    "indicators": ' + _compact(new["indicators"]) + suffix
        elif s.startswith('"judgment":'):
            suffix = "," if s.endswith(",") else ""
            out[i] = '    "judgment": ' + json.dumps(new["judgment"], ensure_ascii=False) + suffix
    start = next((i for i, l in enumerate(out) if l.strip().startswith('"revisions": [')), None)
    if start is not None:
        close = next((i for i in range(start + 1, len(out)) if out[i].strip() == "]"), None)
        if close is not None:
            extra = new["revisions"][len(json.loads("[" + ",".join(x.strip().rstrip(",") for x in out[start + 1:close]) + "]")):]
            if extra:
                if out[close - 1].strip().endswith("}"):
                    out[close - 1] = out[close - 1].rstrip().rstrip(",") + ","
                for k, item in enumerate(extra):
                    line = "      " + _compact(item)
                    if k < len(extra) - 1:
                        line += ","
                    out.insert(close + k, line)
    return out


def _patch_meta_text(text: str, meta: dict) -> str:
    lines = text.rstrip("\n").split("\n")
    for i, l in enumerate(lines):
        s = l.strip()
        if s.startswith('"generatedAt":'):
            lines[i] = '  "generatedAt": ' + json.dumps(meta["generatedAt"], ensure_ascii=False) + ("," if s.endswith(",") else "")
        elif s.startswith('"counts":'):
            c = meta["counts"]
            lines[i] = '  "counts": { "events": %d, "trends": %d, "forecasts": %d }%s' % (
                c["events"], c["trends"], c["forecasts"], "," if s.endswith(",") else ""
            )
        elif s.startswith('"window":') and isinstance(meta.get("window"), dict):
            w = meta["window"]
            lines[i] = '  "window": { "from": %s, "to": %s }%s' % (
                json.dumps(w.get("from")), json.dumps(w.get("to")), "," if s.endswith(",") else ""
            )
    return "\n".join(lines) + "\n"


def preserve_file(baseline_text: str, key: str, payload: list | dict) -> tuple[str, bool]:
    """把 payload 保形写入 baseline_text；返回 (文本, 是否完全保形)。"""
    try:
        old = json.loads(baseline_text)
    except json.JSONDecodeError:
        return json.dumps(payload, ensure_ascii=False, indent=2) + "\n", False
    if key == "meta":
        text = _patch_meta_text(baseline_text, payload)
        return text, json.loads(text) == payload
    items = payload or []
    if old == payload:
        return baseline_text, True
    new_items = [x for x in items if x not in old]
    text = _append_blocks(baseline_text, new_items)
    try:
        if json.loads(text) != items:
            return json.dumps(items, ensure_ascii=False, indent=2) + "\n", False
    except json.JSONDecodeError:
        return json.dumps(items, ensure_ascii=False, indent=2) + "\n", False
    return text, True


def preserve_trends(baseline_text: str, items: list) -> tuple[str, bool]:
    old = json.loads(baseline_text)
    old_by = {o["id"]: o for o in old}
    text = baseline_text
    for item in items:
        prev = old_by.get(item["id"])
        if prev is None or prev == item:
            continue
        lines = text.rstrip("\n").split("\n")
        b = _entry_bounds(lines, item["id"])
        if not b:
            return json.dumps(items, ensure_ascii=False, indent=2) + "\n", False
        start, end = b
        patched = _patch_trend_entry(lines[start:end + 1], item)
        text = "\n".join(lines[:start] + patched + lines[end + 1:]) + "\n"
    new_items = [x for x in items if x["id"] not in old_by]
    text = _append_blocks(text, new_items)
    try:
        return text, json.loads(text) == items
    except json.JSONDecodeError:
        return json.dumps(items, ensure_ascii=False, indent=2) + "\n", False

# ---------------------------------------------------------------- 主流程


def build(args) -> dict:
    data_dir = Path(args.data).expanduser().resolve()
    site_dir = Path(args.site).expanduser().resolve()
    out_dir = Path(args.out).expanduser().resolve()
    report_path = Path(args.report).expanduser().resolve()

    raw_events = v1_list(load_json(data_dir / "events.json"), "events")
    raw_trends = v1_list(load_json(data_dir / "trends.json"), "trends")
    raw_forecasts = v1_list(load_json(data_dir / "forecasts.json"), "forecasts")
    site = load_site(site_dir)

    notes: list = []
    if getattr(args, "only_published", False):
        kept = [e for e in raw_events if str(e.get("status")) == "published"]
        skipped = len(raw_events) - len(kept)
        if skipped:
            notes.append(f"--only-published：跳过 {skipped} 条非 published 事件（站点只收已发布）")
        raw_events = kept
    conf_path = Path(getattr(args, "confidence", None) or DEFAULT_CONFIDENCE).expanduser()
    manual_conf = load_confidence(conf_path)
    if not conf_path.exists():
        notes.append(f"未找到人工置信度文件 {conf_path}，回退 currentJudgment / stage 映射")
    trend_id_map = {str(t.get("id")): trend_site_id(str(t.get("id"))) for t in raw_trends}
    new_events = [convert_event(e, notes, trend_id_map) for e in raw_events]
    trend_items = [(str(t.get("id")), convert_trend(t, notes, manual_conf.get(str(t.get("id"))))) for t in raw_trends]
    new_trends = [item for _, item in trend_items if "_merged_into" not in item]
    absorbed_items = [item for _, item in trend_items if "_merged_into" in item]
    event_titles = {str(raw.get("id")): new["title"] for raw, new in zip(raw_events, new_events)}
    event_titles.update({e["id"]: e["title"] for e in site["events"]})
    new_forecasts = [convert_forecast(f, event_titles, notes, trend_id_map) for f in raw_forecasts]

    conflicts: list = []
    merges_log: list = []
    if args.no_merge:
        merged = {
            "events": new_events,
            "trends": new_trends + absorbed_items,
            "forecasts": new_forecasts,
            "snapshots": [],
        }
    else:
        merged_trends, merges_log = absorb_trends(
            site["trends"], {str(item["_merged_into"]): item for item in absorbed_items}, notes
        )
        merged_trends = merge_append_only(merged_trends, new_trends, conflicts, "trends")
        merged = {
            "events": merge_append_only(site["events"], new_events, conflicts, "events"),
            "trends": merged_trends,
            "forecasts": merge_append_only(site["forecasts"], new_forecasts, conflicts, "forecasts"),
            "snapshots": site["snapshots"],
        }

    dates = [e["date"] for e in merged["events"] if DATE_RE.match(str(e.get("date")))]
    window = dict(site["meta"].get("window") or {})
    if dates:
        window["from"] = min([window.get("from") or dates[0]] + dates)
        window["to"] = max([window.get("to") or dates[-1]] + dates)

    meta = dict(site["meta"])
    meta["generatedAt"] = now_iso()
    meta["window"] = window
    meta["counts"] = {
        "events": len(merged["events"]),
        "trends": len(merged["trends"]),
        "forecasts": len(merged["forecasts"]),
    }

    payload = {**merged, "meta": meta}
    confidence_rows = [
        {"id": t["id"], "value": t["confidence"], "source": t.get("_confidence_source", "")}
        for t in new_trends
    ]
    for item in new_trends + absorbed_items:
        for key in ("_v1_id", "_confidence_source", "_counter_tail", "_merged_into"):
            item.pop(key, None)
    errors = validate(payload)

    def diff_kind(kind: str) -> dict:
        return field_diff(site[kind], {"events": new_events, "trends": new_trends, "forecasts": new_forecasts}[kind])

    ctx = {
        "contentDigest": str(getattr(args, "content_digest", "") or ""),
        "generatedAt": meta["generatedAt"],
        "data_dir": str(data_dir),
        "site_dir": str(site_dir),
        "out_dir": str(out_dir),
        "wrote_src": False,
        "snapshots": len(merged["snapshots"]),
        "counts": {
            kind: {
                "site": len(site[kind]),
                "new": len({"events": new_events, "trends": new_trends, "forecasts": new_forecasts}[kind]),
                "merged": len(merged[kind]),
                "conflicts": sum(1 for c in conflicts if c.startswith(kind)),
            }
            for kind in ("events", "trends", "forecasts")
        },
        "new": {"events": new_events, "trends": new_trends, "forecasts": new_forecasts},
        "overwrite": [str(x.get("id")) for x in site["events"]],
        "diffs": {kind: diff_kind(kind) for kind in ("events", "trends", "forecasts")},
        "trend_merges": merges_log,
        "confidence_rows": confidence_rows,
        "notes": notes,
        "errors": errors,
        "payload": payload,
        "report_path": report_path,
        "out_dir_path": out_dir,
    }
    return ctx


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="ERA v1 → 站点契约 v1 导出（默认只出暂存，不写 src/data）"
    )
    parser.add_argument("--data", default=str(DEFAULT_DATA), help="v1 导出目录（含 events/trends/forecasts.json）")
    parser.add_argument("--site", default=str(DEFAULT_SITE), help="站点仓库根目录（读 src/data 作为基线）")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help="暂存输出目录")
    parser.add_argument("--report", default=str(DEFAULT_REPORT), help="对照表 Markdown 输出路径")
    parser.add_argument("--confidence", default=str(DEFAULT_CONFIDENCE), help="人工置信度 JSON（默认 reports/trend-confidence.json）")
    parser.add_argument("--only-published", action="store_true", help="站点只收 status=published 的事件（默认全收）")
    parser.add_argument("--write-src", action="store_true", help="人工放行后再写站点 src/data（仍按只增不删合并）")
    parser.add_argument("--no-merge", action="store_true", help="只输出本批（用于看覆盖效果，不合并站点现有条目）")
    parser.add_argument("--dry-run", action="store_true", help="只出对照表，不写任何 JSON")
    parser.add_argument("--content-digest", default="", help="content 树哈希，写进对照表表头（如 c928890acdb8e762）")
    parser.add_argument("--preserve-format", action="store_true", help="写盘时保留基线文件原文，只追加/最小改动（git diff 只出新增）")
    parser.add_argument("--json", dest="as_json", action="store_true", help="机器可读输出（人类提示走 stderr）")
    args = parser.parse_args(argv)

    try:
        ctx = build(args)
    except ExportError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return USAGE_ERROR

    report = render_report(ctx)
    ctx["report_path"].parent.mkdir(parents=True, exist_ok=True)
    ctx["report_path"].write_text(report, encoding="utf-8")

    wrote: list = []
    if not args.dry_run:
        out_dir = ctx["out_dir_path"]
        def emit(dest: Path) -> None:
            if args.preserve_format:
                base = Path(args.site).expanduser().resolve() / "src" / "data"
                for name, key in (("events.json", "events"), ("trends.json", "trends"), ("forecasts.json", "forecasts"), ("snapshots.json", "snapshots")):
                    bpath = base / name
                    if bpath.exists():
                        raw = bpath.read_text(encoding="utf-8")
                        if key == "trends":
                            text, ok = preserve_trends(raw, ctx["payload"][key])
                        else:
                            text, ok = preserve_file(raw, key, ctx["payload"][key])
                        if not ok:
                            print(f"WARN: {name} 保形失败，退回整文件重写", file=sys.stderr)
                        dest.mkdir(parents=True, exist_ok=True)
                        (dest / name).write_text(text, encoding="utf-8")
                    else:
                        write_json(dest / name, ctx["payload"][key])
                mpath = base / "meta.json"
                if mpath.exists():
                    text, ok = preserve_file(mpath.read_text(encoding="utf-8"), "meta", ctx["payload"]["meta"])
                    if not ok:
                        print("WARN: meta.json 保形失败，退回整文件重写", file=sys.stderr)
                    dest.mkdir(parents=True, exist_ok=True)
                    (dest / "meta.json").write_text(text, encoding="utf-8")
                else:
                    write_json(dest / "meta.json", ctx["payload"]["meta"])
            else:
                for name, key in (("events.json", "events"), ("trends.json", "trends"), ("forecasts.json", "forecasts"), ("snapshots.json", "snapshots")):
                    write_json(dest / name, ctx["payload"][key])
                write_json(dest / "meta.json", ctx["payload"]["meta"])

        emit(out_dir)
        wrote.append(str(out_dir))
        if args.write_src:
            src = Path(args.site).expanduser().resolve() / "src" / "data"
            emit(src)
            ctx["wrote_src"] = True
            wrote.append(str(src))
            ctx["report_path"].write_text(render_report(ctx), encoding="utf-8")

    if args.as_json:
        print(
            json.dumps(
                {
                    "ok": not ctx["errors"],
                    "counts": ctx["counts"],
                    "errors": ctx["errors"],
                    "wrote": wrote,
                    "report": str(ctx["report_path"]),
                    "writeSrc": ctx["wrote_src"],
                    "contentDigest": ctx.get("contentDigest", ""),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        say(args, f"站点现有：events {ctx['counts']['events']['site']} / trends {ctx['counts']['trends']['site']} / forecasts {ctx['counts']['forecasts']['site']}")
        say(args, f"本批新增：events {ctx['counts']['events']['new']} / trends {ctx['counts']['trends']['new']} / forecasts {ctx['counts']['forecasts']['new']}")
        say(args, f"合并后：events {ctx['counts']['events']['merged']} / trends {ctx['counts']['trends']['merged']} / forecasts {ctx['counts']['forecasts']['merged']} / snapshots {ctx['snapshots']}")
        if ctx["errors"]:
            say(args, f"自检未通过：{len(ctx['errors'])} 条（详见对照表）")
            for e in ctx["errors"][:10]:
                say(args, f"  - {e}")
        else:
            say(args, "自检通过（镜像站点 validate.mjs 规则）")
        say(args, f"对照表：{ctx['report_path']}")
        if wrote:
            say(args, "已写出：" + "、".join(wrote))
        else:
            say(args, "dry-run：未写任何 JSON")

    return VALIDATION_ERROR if ctx["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
