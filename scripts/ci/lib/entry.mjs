// scripts/ci/lib/entry.mjs — 外部入口解析：路径与参数全部来自 pipeline.config.json entries，
// 不在工作流里硬编码。约定：入口可跑、带 --help、出错用非 0 退出码，结构化报告写到 entries.<name>.report。
import fs from "node:fs";
import path from "node:path";
import { ROOT, resolvePath, readJson, run, sleep } from "./common.mjs";

export function entryOf(cfg, name) {
  const entry = cfg?.entries?.[name];
  if (!entry || !Array.isArray(entry.cmd) || entry.cmd.length === 0) return null;
  return { argv: entry.cmd, report: entry.report ? resolvePath(entry.report) : null };
}

export function describeEntry(cfg, name) {
  const e = entryOf(cfg, name);
  if (!e) return `entries.${name}.cmd 未配置（使用内置实现）`;
  return `entries.${name}.cmd = ${e.argv.join(" ")}`;
}

export function readEntryReport(cfg, name) {
  const e = entryOf(cfg, name);
  if (!e || !e.report || !fs.existsSync(e.report)) return null;
  try {
    return readJson(e.report);
  } catch {
    return null;
  }
}

/** 跑外部入口：最多重试 cfg.budgets.retry.retries 次，只对非 0 且有输出时重试一次以外的网络类错误由入口自己处理。 */
export async function runEntry(cfg, name, { extraArgs = [], timeoutMs = 18 * 60 * 1000, env = {} } = {}) {
  const e = entryOf(cfg, name);
  if (!e) return { ran: false, reason: "entry_not_configured", status: null, report: null };
  const retries = Number(cfg?.budgets?.retry?.retries ?? 2);
  let res;
  for (let i = 0; i <= retries; i += 1) {
    res = run(e.argv[0], [...e.argv.slice(1), ...extraArgs], { cwd: ROOT, timeoutMs, env });
    if (res.status === 0) break;
    if (i < retries) await sleep(Number(cfg?.budgets?.retry?.baseDelayMs ?? 800) * 2 ** i);
  }
  return {
    ran: true,
    argv: e.argv,
    status: res.status,
    stdout: res.stdout,
    stderr: res.stderr,
    report: readEntryReport(cfg, name),
  };
}

/** 入口存在但参数不合法时，给一条能直接定位的提示。 */
export function assertEntryHelp(cfg, name) {
  const e = entryOf(cfg, name);
  if (!e) return { ok: true, skipped: true };
  const r = run(e.argv[0], [...e.argv.slice(1), "--help"], { timeoutMs: 30000 });
  const ok = r.status === 0 && `${r.stdout}${r.stderr}`.trim().length > 0;
  return { ok, status: r.status, hint: ok ? "" : `${e.argv.join(" ")} --help 失败：${(r.stderr || r.stdout).slice(-300)}` };
}

export function ensureInsideRoot(p) {
  const abs = path.resolve(resolvePath(p));
  return abs === ROOT || abs.startsWith(ROOT + path.sep);
}
