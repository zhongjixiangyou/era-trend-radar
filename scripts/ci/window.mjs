#!/usr/bin/env node
/**
 * scripts/ci/window.mjs — 解析本轮窗口，并把它钉成后续所有阶段的环境变量。
 *
 * 用法：
 *   node scripts/ci/window.mjs --json                 # 打印 JSON（人看/调试）
 *   node scripts/ci/window.mjs --github-env           # 追加到 $GITHUB_ENV（CI 用）
 *   node scripts/ci/window.mjs --as-of 2026-11-14T12:00:00+08:00 --json
 *
 * 为什么单独一步：collect / promote / validate / export 必须用同一个 asOf，
 * 各自取 now() 会在跨午夜时拆成两个「今天」。
 */
import fs from "node:fs";
import { loadConfig, parseArgs } from "./lib/common.mjs";
import { resolveWindow } from "./lib/window.mjs";

const args = parseArgs(process.argv.slice(2));

const cfg = loadConfig(args.config || "pipeline.config.json");
const win = resolveWindow(cfg, {
  asOf: args["as-of"] || null,
  maxTs: args["max-ts"] || null,
  mode: args.mode || null,
  days: args.days || null,
});

const payload = {
  mode: win.mode,
  from: win.from,
  to: win.to,
  days: win.days,
  timezone: win.timezone,
  asOf: win.asOf,
  asOfSource: win.asOfSource,
  asOfFrom: win.asOfFrom,
  upper: win.upper,
  fromTs: win.fromTs,
  ...(win.frozenAt ? { frozenAt: win.frozenAt } : {}),
};

if (args["github-env"]) {
  const file = process.env.GITHUB_ENV;
  if (!file) {
    console.error("--github-env 需要 GITHUB_ENV（只能在 Actions 里用）");
    process.exit(2);
  }
  const lines = [
    `ERA_AS_OF=${payload.asOf}`,
    `ERA_WINDOW_FROM=${payload.from}`,
    `ERA_WINDOW_TO=${payload.to}`,
    `ERA_WINDOW_MODE=${payload.mode}`,
    `ERA_WINDOW_DAYS=${payload.days ?? ""}`,
    `ERA_WINDOW_UPPER=${payload.upper}`,
    `ERA_WINDOW_AS_OF_FROM=${payload.asOfFrom}`,
  ];
  fs.appendFileSync(file, lines.join("\n") + "\n", "utf8");
  console.log(`window: ${payload.mode} ${payload.from} → ${payload.to} (asOf ${payload.asOf}, ${payload.asOfSource}/${payload.asOfFrom})`);
  process.exit(0);
}

console.log(JSON.stringify(payload, null, 2));
