#!/usr/bin/env python3
"""生成 reports/content-manifest.json（content/ 清单与内容指纹）。

contentHash = sha256("\n".join(f"{path}:{sha256}"))，路径按字典序，覆盖 content/ 下全部 .json。
只读 content/，不修改任何内容文件。
"""
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONTENT = ROOT / "content"
OUT = ROOT / "reports" / "content-manifest.json"

files = sorted(p for p in CONTENT.rglob("*.json") if p.is_file())
entries = []
for p in files:
    raw = p.read_bytes()
    entries.append({
        "path": str(p.relative_to(ROOT)),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw),
    })

joined = "\n".join(f"{e['path']}:{e['sha256']}" for e in entries)
content_hash = hashlib.sha256(joined.encode("utf-8")).hexdigest()

events = {"pending": 0, "published": 0}
trends = {}
forecasts = {"open": 0}
for p in files:
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        continue
    rel = str(p.relative_to(CONTENT))
    if rel.startswith("events/"):
        events["published" if d.get("status") == "published" else "pending"] += 1
    elif rel.startswith("trends/"):
        trends["(none)"] = trends.get("(none)", 0) + 1
    elif rel.startswith("forecasts/"):
        if d.get("status") != "resolved":
            forecasts["open"] += 1

manifest = {
    "schema": "era-content-manifest/v1",
    "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "contentHash": content_hash,
    "fileCount": len(entries),
    "mode": "public",
    "counts": {"events": events, "trends": trends, "forecasts": forecasts},
    "files": entries,
}
OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(json.dumps({"fileCount": len(entries), "contentHash": content_hash, "counts": manifest["counts"]}, ensure_ascii=False))
