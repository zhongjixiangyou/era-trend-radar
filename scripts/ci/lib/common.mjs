// scripts/ci/lib/common.mjs — CI 公共工具，只用 Node 标准库（不新增依赖）。
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { createHash } from "node:crypto";
import fs from "node:fs";
import path from "node:path";

export const ROOT = process.env.ERA_ROOT
  ? path.resolve(process.env.ERA_ROOT)
  : process.cwd();

/** 脚本所在目录，与 ROOT（工作/沙盒目录）解耦；沙盒里也能找到同包脚本。 */
export const SCRIPTS = path.resolve(path.dirname(fileURLToPath(import.meta.url)), ".."); // → scripts/ci

export function resolvePath(p) {
  return path.isAbsolute(p) ? p : path.resolve(ROOT, p);
}

export function ensureDir(p) {
  fs.mkdirSync(resolvePath(p), { recursive: true });
}

export function readJson(p, fallback) {
  const abs = resolvePath(p);
  if (!fs.existsSync(abs)) {
    if (fallback !== undefined) return fallback;
    throw new Error(`文件不存在: ${abs}`);
  }
  try {
    return JSON.parse(fs.readFileSync(abs, "utf8"));
  } catch (err) {
    throw new Error(`JSON 解析失败 ${abs}: ${err.message}`);
  }
}

export function writeJson(p, obj) {
  const abs = resolvePath(p);
  ensureDir(path.dirname(abs));
  fs.writeFileSync(abs, JSON.stringify(obj, null, 2) + "\n", "utf8");
  return abs;
}

export function loadConfig(p = process.env.ERA_CONFIG || "pipeline.config.json") {
  const cfg = readJson(p);
  for (const key of ["schemaVersion", "scoringVersion", "modelVersion"]) {
    if (!cfg[key]) throw new Error(`pipeline.config.json 缺少 ${key}`);
  }
  return cfg;
}

export function loadConfigFrom(file) {
  return loadConfig(file);
}

export function nowIso() {
  return new Date().toISOString();
}

export function dateOnly(value) {
  return String(value || "").slice(0, 10);
}

export function sha256(text) {
  return createHash("sha256").update(String(text), "utf8").digest("hex");
}

export function sha256File(file) {
  return createHash("sha256").update(fs.readFileSync(resolvePath(file))).digest("hex");
}

export function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

/** 预算是硬闸：超过就直接抛错，让 job 失败，而不是默默超支。 */
export class Budget {
  constructor(limit, label) {
    this.limit = Number(limit) || 0;
    this.label = label || "budget";
    this.used = 0;
    this.log = [];
  }
  spend(n = 1, note = "") {
    if (this.limit > 0 && this.used + n > this.limit) {
      throw new Error(`[budget] ${this.label} 超限：已用 ${this.used} + ${n} > ${this.limit}`);
    }
    this.used += n;
    this.log.push({ at: nowIso(), n, note });
  }
  report() {
    return { label: this.label, limit: this.limit, used: this.used, log: this.log };
  }
}

/** 重试：retries=2 表示总共 3 次尝试；指数退避 + 抖动。 */
export async function withRetry(fn, opts = {}) {
  const {
    retries = 2,
    baseDelayMs = 800,
    maxDelayMs = 6000,
    label = "operation",
    shouldRetry = () => true,
  } = opts;
  const attempts = retries + 1;
  let lastErr;
  for (let i = 0; i < attempts; i += 1) {
    try {
      return await fn(i);
    } catch (err) {
      lastErr = err;
      if (i === attempts - 1 || !shouldRetry(err)) break;
      const delay = Math.min(maxDelayMs, baseDelayMs * 2 ** i) + Math.floor(Math.random() * 200);
      console.log(`[retry] ${label} 第 ${i + 1}/${attempts} 次失败：${err.message}；${delay}ms 后重试`);
      await sleep(delay);
    }
  }
  throw lastErr;
}

export function run(cmd, args = [], opts = {}) {
  const res = spawnSync(cmd, args, {
    cwd: opts.cwd ? resolvePath(opts.cwd) : ROOT,
    encoding: "utf8",
    timeout: opts.timeoutMs || 20 * 60 * 1000,
    env: { ...process.env, ...(opts.env || {}) },
    stdio: opts.stdio || "pipe",
  });
  return {
    status: res.status,
    signal: res.signal,
    stdout: res.stdout || "",
    stderr: res.stderr || "",
    error: res.error,
  };
}

export function mustRun(cmd, args = [], opts = {}) {
  const res = run(cmd, args, opts);
  if (res.error) throw new Error(`执行失败 ${cmd}: ${res.error.message}`);
  if (res.status !== 0) {
    const tail = (res.stderr || res.stdout || "").split("\n").slice(-25).join("\n");
    throw new Error(`命令失败（exit ${res.status}）: ${cmd} ${args.join(" ")}\n${tail}`);
  }
  return res;
}

/** GitHub Actions 注解；本地跑时就是普通输出。 */
export function notice(msg) {
  console.log(`::notice::${msg}`);
}
export function warning(msg) {
  console.log(`::warning::${msg}`);
}
export function errorAnnotation(msg) {
  console.log(`::error::${msg}`);
}

/** 追加 job summary；本地没有 GITHUB_STEP_SUMMARY 时只在终端打印。 */
export function appendSummary(markdown) {
  const file = process.env.GITHUB_STEP_SUMMARY;
  if (file) fs.appendFileSync(file, markdown.endsWith("\n") ? markdown : markdown + "\n", "utf8");
  console.log(`\n[summary]\n${markdown}`);
}

/** 只报告 key 是否设置，绝不回显值。 */
export function secretState(envName) {
  const v = process.env[envName];
  return v ? `set(len=${String(v).length})` : "unset";
}

export function redact(text, envNames = []) {
  let out = String(text);
  for (const name of envNames) {
    const v = process.env[name];
    if (v && String(v).length >= 6) out = out.split(v).join("[REDACTED]");
  }
  return out;
}

export function parseArgs(argv = process.argv.slice(2)) {
  const out = { _: [] };
  for (let i = 0; i < argv.length; i += 1) {
    const a = argv[i];
    if (a.startsWith("--")) {
      const key = a.slice(2);
      const next = argv[i + 1];
      if (next && !next.startsWith("--")) {
        out[key] = next;
        i += 1;
      } else {
        out[key] = true;
      }
    } else {
      out._.push(a);
    }
  }
  return out;
}

export function relative(p) {
  return path.relative(ROOT, resolvePath(p));
}
