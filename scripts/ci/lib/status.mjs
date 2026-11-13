// scripts/ci/lib/status.mjs — 四态运行结果：healthy / no_change / degraded / failed。
// 状态只写 artifacts/status/<tool>.json 并打 job summary，不参与业务数据。
import fs from "node:fs";
import { appendSummary, resolvePath, writeJson, nowIso } from "./common.mjs";

export const STATUSES = ["healthy", "no_change", "degraded", "failed"];

export function exitFor(cfg, key) {
  return Number(cfg?.exitCodes?.[key] ?? 1);
}

export function emitStatus(cfg, tool, status, detail = {}) {
  if (!STATUSES.includes(status)) throw new Error(`非法运行状态 ${status}`);
  const record = { tool, status, at: nowIso(), ...detail };
  writeJson(`artifacts/status/${tool}.json`, record);
  const icon = { healthy: "✅", no_change: "➖", degraded: "⚠️", failed: "❌" }[status];
  appendSummary(`### ${icon} ${tool}: ${status}\n\n${detail.message || ""}\n`);
  console.log(`[status] ${tool}=${status}${detail.message ? ` · ${detail.message}` : ""}`);
  return record;
}

export function readStatus(tool) {
  const p = resolvePath(`artifacts/status/${tool}.json`);
  if (!fs.existsSync(p)) return null;
  try {
    return JSON.parse(fs.readFileSync(p, "utf8"));
  } catch {
    return null;
  }
}
