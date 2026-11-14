#!/usr/bin/env node
/**
 * scripts/ci/test-window.mjs — 窗口解析的边界自测（Node 侧）。
 * 与 Python 侧 scripts/tests/test_window.py 互为对照：同一组用例，两边结果必须一致。
 *
 * 跑法：node scripts/ci/test-window.mjs
 */
import assert from "node:assert/strict";
import { resolveWindow, judge, currentWindow } from "./lib/window.mjs";

const rollingCfg = { window: { mode: "rolling", days: 180, timezone: "Asia/Shanghai" } };
const fixedCfg = {
  window: {
    mode: "fixed",
    start: "2026-05-13",
    end: "2026-11-13",
    frozen: { start: "2026-05-13", end: "2026-11-13", frozenAt: "2026-11-13" },
  },
};

let pass = 0;
const ok = (name, fn) => {
  try { fn(); pass++; console.log(`✅ ${name}`); }
  catch (e) { console.error(`❌ ${name}\n   ${e.message}`); process.exitCode = 1; }
};

const clearEnv = () => { delete process.env.ERA_AS_OF; delete process.env.ERA_MAX_TS; };

ok("rolling: 2026-11-14 12:00+08 → 05-19 ~ 11-14，180 天", () => {
  clearEnv();
  const w = resolveWindow(rollingCfg, { asOf: "2026-11-14T12:00:00+08:00" });
  assert.equal(w.mode, "rolling");
  assert.equal(w.from, "2026-05-19");
  assert.equal(w.to, "2026-11-14");
  assert.equal(w.days, 180);
  assert.equal(w.upper, new Date("2026-11-14T12:00:00+08:00").toISOString());
});

ok("rolling: 当天 00:00:01 与 23:59:59 得到同一个 to", () => {
  clearEnv();
  const a = resolveWindow(rollingCfg, { asOf: "2026-11-14T00:00:01+08:00" });
  const b = resolveWindow(rollingCfg, { asOf: "2026-11-14T23:59:59+08:00" });
  assert.equal(a.to, "2026-11-14");
  assert.equal(b.to, "2026-11-14");
  assert.equal(a.from, b.from);
});

ok("rolling: 跨过午夜后窗口整体平移一天", () => {
  clearEnv();
  const b = resolveWindow(rollingCfg, { asOf: "2026-11-15T00:00:00+08:00" });
  assert.equal(b.to, "2026-11-15");
  assert.equal(b.from, "2026-05-20");
});

ok("rolling: 上界是 asOf 时刻，不是当天 23:59:59", () => {
  clearEnv();
  const w = resolveWindow(rollingCfg, { asOf: "2026-11-14T12:00:00+08:00" });
  assert.equal(judge("2026-11-14T11:59:59+08:00", w), "ok");
  assert.equal(judge("2026-11-14T12:00:00+08:00", w), "ok");
  assert.equal(judge("2026-11-14T12:00:01+08:00", w), "future");
  assert.equal(judge("2026-11-14T23:00:00+08:00", w), "future");
});

ok("rolling: 日期型边界（当天合法、次日非法、起点前算旧）", () => {
  clearEnv();
  const w = resolveWindow(rollingCfg, { asOf: "2026-11-14T12:00:00+08:00" });
  assert.equal(judge("2026-05-19", w), "ok");
  assert.equal(judge("2026-05-18", w), "old");
  assert.equal(judge("2026-11-14", w), "ok");
  assert.equal(judge("2026-11-15", w), "future");
});

ok("rolling: 起点当天 00:00:00 合法，前一秒算旧", () => {
  clearEnv();
  const w = resolveWindow(rollingCfg, { asOf: "2026-11-14T12:00:00+08:00" });
  assert.equal(judge("2026-05-19T00:00:00+08:00", w), "ok");
  assert.equal(judge("2026-05-18T23:59:59+08:00", w), "old");
});

ok("fixed: 与冻结时完全一致（05-13 ~ 11-13，上界 23:59:59.999+08）", () => {
  clearEnv();
  const w = resolveWindow(fixedCfg, { asOf: "2026-11-14T12:00:00+08:00" });
  assert.equal(w.mode, "fixed");
  assert.equal(w.from, "2026-05-13");
  assert.equal(w.to, "2026-11-13");
  assert.equal(w.frozenAt, "2026-11-13");
  assert.equal(w.upper, new Date("2026-11-13T23:59:59.999+08:00").toISOString());
  assert.equal(judge("2026-11-13T23:59:59+08:00", w), "ok");
  assert.equal(judge("2026-11-14T00:00:00+08:00", w), "future");
});

ok("优先级：ERA_AS_OF > ERA_MAX_TS", () => {
  process.env.ERA_AS_OF = "2026-11-14T12:00:00+08:00";
  process.env.ERA_MAX_TS = "2026-11-10T00:00:00+08:00";
  const w = resolveWindow(rollingCfg, {});
  assert.equal(w.to, "2026-11-14");
  assert.equal(w.asOfSource, "env");
  clearEnv();
});

ok("优先级：--as-of 压过 ERA_MAX_TS", () => {
  process.env.ERA_MAX_TS = "2026-11-10T00:00:00+08:00";
  const w = resolveWindow(rollingCfg, { asOf: "2026-11-14T12:00:00+08:00" });
  assert.equal(w.to, "2026-11-14");
  assert.equal(w.asOfSource, "cli");
  clearEnv();
});

ok("currentWindow：CI 钉好的 ERA_WINDOW_* 优先", () => {
  process.env.ERA_WINDOW_FROM = "2026-05-19";
  process.env.ERA_WINDOW_TO = "2026-11-14";
  process.env.ERA_WINDOW_MODE = "rolling";
  const w = currentWindow(rollingCfg);
  assert.equal(w.from, "2026-05-19");
  assert.equal(w.to, "2026-11-14");
  assert.equal(w.mode, "rolling");
  delete process.env.ERA_WINDOW_FROM; delete process.env.ERA_WINDOW_TO; delete process.env.ERA_WINDOW_MODE;
});

console.log(`\n${pass} 项通过${process.exitCode ? "，有失败" : "，全部通过"}`);
