#!/usr/bin/env node
/**
 * validate.mjs 的自测：把真实数据复制到临时目录，逐条制造一处违规，
 * 断言退出码和错误码都对得上。
 *
 *   node scripts/validate.selftest.mjs          全部用例
 *   node scripts/validate.selftest.mjs --verbose 打印每个用例的输出
 *
 * 退出码：0 全部通过 / 1 有用例不符预期 / 2 环境问题（找不到数据文件）
 */
import { execFileSync } from 'node:child_process';
import { cpSync, mkdtempSync, rmSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const repo = resolve(here, '..');
const dataSrc = join(repo, 'src', 'data');
const verbose = process.argv.includes('--verbose');

const FILES = ['meta.json', 'events.json', 'trends.json', 'forecasts.json', 'snapshots.json'];

function loadBase() {
  const base = {};
  for (const f of FILES) base[f] = JSON.parse(readFileSync(join(dataSrc, f), 'utf8'));
  return base;
}

/** 深拷贝一份数据，跑一次校验，返回 {code, out} */
function run(mutate) {
  const data = structuredClone(loadBase());
  if (mutate) mutate(data);
  const dir = mkdtempSync(join(tmpdir(), 'era-val-'));
  try {
    for (const f of FILES) writeFileSync(join(dir, f), JSON.stringify(data[f], null, 2));
    try {
      const stdout = execFileSync(process.execPath, [join(here, 'validate.mjs'), '--data', dir], {
        encoding: 'utf8',
        stdio: ['ignore', 'pipe', 'pipe'],
      });
      return { code: 0, out: JSON.parse(stdout) };
    } catch (e) {
      if (e.status === undefined) throw e;
      const raw = (e.stderr || '').trim();
      let parsed = null;
      try {
        parsed = JSON.parse(raw);
      } catch {
        /* 非 JSON 的崩溃也要报出来 */
      }
      return { code: e.status, out: parsed, raw };
    }
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}

const ev = (d) => d['events.json'];
const codes = (r) => (r.out?.errors ?? []).map((x) => x.code);
const paths = (r) => (r.out?.errors ?? []).map((x) => x.path);
const warnCodes = (r) => (r.out?.warnings ?? []).map((x) => x.code);

const cases = [
  {
    name: '干净数据应通过',
    expect: { code: 0 },
    mutate: null,
  },
  {
    name: 'scores.total 缺失',
    expect: { code: 1, code_: 'RANGE', path: 'events[0].scores.total' },
    mutate: (d) => delete ev(d)[0].scores.total,
  },
  {
    name: 'scores.total 与六维不符',
    expect: { code: 1, code_: 'TOTAL_MISMATCH' },
    mutate: (d) => {
      ev(d)[0].scores.total += 1;
    },
  },
  {
    name: '半值向下取整被拒（78.5 必须记 79）',
    expect: { code: 1, code_: 'TOTAL_MISMATCH' },
    // ev-20261110-us-cpi-oct 加权值恰为 78.5，改成 78 应报错
    mutate: (d) => {
      ev(d)[0].scores.total = 78;
    },
  },
  {
    name: 'six dims 越界',
    expect: { code: 1, code_: 'RANGE' },
    mutate: (d) => {
      ev(d)[0].scores.structural = 120;
    },
  },
  {
    name: '评分历史日期不递增',
    expect: { code: 1, code_: 'ORDER', path: 'events[0].scoreHistory[1].date' },
    mutate: (d) => {
      const e = ev(d)[0];
      e.scoreHistory.push({ date: e.scoreHistory[0].date, total: e.scores.total, reason: '重复日期' });
    },
  },
  {
    name: '篡改历史末条（不追加）',
    expect: { code: 1, code_: 'HISTORY_TAIL' },
    mutate: (d) => {
      const e = ev(d)[0];
      e.scoreHistory[e.scoreHistory.length - 1].total -= 1;
    },
  },
  {
    name: '首条评分日期不等于采集日',
    expect: { code: 1, code_: 'HISTORY_HEAD' },
    mutate: (d) => {
      ev(d)[0].scoreHistory[0].date = '2026-11-01';
    },
  },
  {
    name: 'reportedAt 早于 date',
    expect: { code: 1, code_: 'ORDER', path: 'events[0].reportedAt' },
    mutate: (d) => {
      ev(d)[0].reportedAt = '2020-01-01';
    },
  },
  {
    name: 'collectedAt 早于 reportedAt',
    expect: { code: 1, code_: 'ORDER', path: 'events[0].collectedAt' },
    mutate: (d) => {
      ev(d)[0].collectedAt = '2020-01-01';
    },
  },
  {
    name: '趋势引用断链',
    expect: { code: 1, code_: 'DANGLING', path: 'events[0].trends[0].id' },
    mutate: (d) => {
      ev(d)[0].trends[0].id = 'tr-does-not-exist';
    },
  },
  {
    name: '判断的 trendId 断链',
    expect: { code: 1, code_: 'DANGLING' },
    mutate: (d) => {
      d['forecasts.json'][0].trendId = 'tr-nope';
    },
  },
  {
    name: '快照 eventIds 断链',
    expect: { code: 1, code_: 'DANGLING' },
    mutate: (d) => {
      d['snapshots.json'][0].eventIds.push('ev-20260101-nope');
    },
  },
  {
    name: '快照 trendChanges 断链',
    expect: { code: 1, code_: 'DANGLING' },
    mutate: (d) => {
      d['snapshots.json'][0].trendChanges[0].trendId = 'tr-nope';
    },
  },
  {
    name: '快照 summary 为空',
    expect: { code: 1, code_: 'REQUIRED' },
    mutate: (d) => {
      d['snapshots.json'][0].summary = '  ';
    },
  },
  {
    name: '趋势 revisions.from 越界',
    expect: { code: 1, code_: 'RANGE' },
    mutate: (d) => {
      d['trends.json'][0].revisions[1].from = 150;
    },
  },
  {
    name: '趋势 confidence 越界',
    expect: { code: 1, code_: 'RANGE' },
    mutate: (d) => {
      d['trends.json'][0].confidence = 120;
    },
  },
  {
    name: '判断 history.confidence 越界',
    expect: { code: 1, code_: 'RANGE' },
    mutate: (d) => {
      d['forecasts.json'][0].history[0].confidence = 130;
    },
  },
  {
    name: 'meta.runStatus 非法',
    expect: { code: 1, code_: 'ENUM' },
    mutate: (d) => {
      d['meta.json'].runStatus = 'green';
    },
  },
  {
    name: 'meta.counts 与实际不符',
    expect: { code: 1, code_: 'COUNT' },
    mutate: (d) => {
      d['meta.json'].counts.events += 1;
    },
  },
  {
    name: 'meta.generatedAt 不是 ISO',
    expect: { code: 1, code_: 'DATE' },
    mutate: (d) => {
      d['meta.json'].generatedAt = '2026/11/13';
    },
  },
  {
    name: '孤儿趋势只警告不拦（exit 0）',
    expect: { code: 0, warn_: 'ORPHAN' },
    mutate: (d) => {
      const t = structuredClone(d['trends.json'][0]);
      t.id = 'tr-orphan-sample';
      t.title = '没人引用的趋势';
      d['trends.json'].push(t);
      d['meta.json'].counts.trends += 1;
    },
  },
];

let failed = 0;
for (const c of cases) {
  const r = run(c.mutate);
  const want = c.expect;
  const problems = [];
  if (r.code !== want.code) problems.push(`退出码 ${r.code} ≠ ${want.code}`);
  if (want.code_ && !codes(r).includes(want.code_)) problems.push(`缺错误码 ${want.code_}（实际 ${codes(r).join(',') || '无'}）`);
  if (want.path && !paths(r).includes(want.path)) problems.push(`缺错误路径 ${want.path}（实际 ${paths(r).slice(0, 3).join(',') || '无'}）`);
  if (want.warn_ && !warnCodes(r).includes(want.warn_)) problems.push(`缺警告 ${want.warn_}`);
  if (problems.length) {
    failed += 1;
    console.error(`✗ ${c.name}`);
    for (const p of problems) console.error(`    ${p}`);
    if (r.raw) console.error(`    stderr: ${r.raw.slice(0, 300)}`);
  } else {
    console.log(`✓ ${c.name}`);
    if (verbose && r.out?.errors?.length) console.log(`    ${codes(r).join(', ')}`);
  }
}

console.log(`\n${cases.length - failed}/${cases.length} 通过`);
process.exit(failed === 0 ? 0 : 1);
