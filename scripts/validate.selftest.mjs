#!/usr/bin/env node
/**
 * validate.mjs 的自测 —— 用内置夹具，不读 src/data。
 *
 * 为什么不用真实数据：真实数据会被别的组随时替换（2026-11-13 一天内换了两套口径），
 * 数据一脏自测先变红，就分不清是校验器错了还是数据错了。
 * 自测只回答「校验器本身还对不对」。
 *
 * 夹具口径 = era-schema v1：id 用下划线（ev_/tr_/fc_），
 * 六维是分项得分（满分 25/20/20/15/10/10），total 直接相加，不加权、不取整。
 *
 *   node scripts/validate.selftest.mjs            全部用例
 *   node scripts/validate.selftest.mjs --verbose  打印每例命中的错误码
 *
 * 退出码：0 全部通过 / 1 有用例不符预期
 */
import { execFileSync } from 'node:child_process';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const verbose = process.argv.includes('--verbose');

// ────────────────────────── 夹具：最小合法数据集（v1 口径） ──────────────────────────
// Alpha：18+16+17+13+9+8 = 81   Beta：15+12+10+8+6+5 = 56
const FIXTURE = {
  'meta.json': {
    site: 'ERA · 时代趋势雷达（自测夹具）',
    issue: '第 1 期',
    generatedAt: '2026-01-10T09:00:00+08:00',
    window: { from: '2025-01-01', to: '2026-12-31' },
    counts: { events: 2, trends: 2, forecasts: 1, snapshots: 1 },
    runStatus: 'ok',
    lastSuccessfulRun: '2026-01-10T09:00:00+08:00',
    editorNote: '自测夹具，不是真实数据，不要发布。',
  },
  'events.json': [
    {
      id: 'ev_fixture-alpha',
      title: '自测夹具事件 Alpha',
      date: '2026-01-05',
      reportedAt: '2026-01-05',
      collectedAt: '2026-01-10',
      region: 'global',
      category: 'technology',
      summary: '这是一条用于自测的合成事件，不指向任何真实主体，也不构成任何判断。',
      facts: ['事实一：夹具数据，用于测试校验器', '事实二：夹具数据，用于测试校验器'],
      metrics: [{ label: '夹具指标', value: '1', note: '夹具' }],
      scores: {
        structural: 18,
        scope: 16,
        signal: 17,
        novelty: 13,
        timeliness: 9,
        cross: 8,
        total: 81,
        rationale: {
          structural: '夹具：结构性影响的分项理由',
          scope: '夹具：影响范围的分项理由',
          signal: '夹具：信号价值的分项理由',
          novelty: '夹具：信息增量的分项理由',
          timeliness: '夹具：时效性的分项理由',
          cross: '夹具：跨领域的分项理由',
        },
      },
      credibility: 'high',
      status: 'confirmed',
      sources: [{ name: '夹具来源', url: 'https://example.com/fixture-alpha', date: '2026-01-05', official: true }],
      trends: [{ id: 'tr_fixture-alpha', effect: 'support', reason: '夹具关系' }],
      timeline: [{ date: '2026-01-05', title: '夹具', note: '夹具' }],
      scoreHistory: [{ date: '2026-01-10', total: 81, reason: '回溯首评：六维分项 18+16+17+13+9+8=81' }],
      watch: '夹具，无需观察。',
      backfill: true,
    },
    {
      id: 'ev_fixture-beta',
      title: '自测夹具事件 Beta',
      date: '2026-01-06',
      reportedAt: '2026-01-06',
      collectedAt: '2026-01-10',
      region: 'china',
      category: 'industry',
      summary: '第二条合成事件，用来让夹具里有多条事件、多个趋势引用，验证批量路径。',
      facts: ['事实一：夹具数据', '事实二：夹具数据'],
      metrics: [{ label: '夹具指标', value: '2', note: '夹具' }],
      scores: { structural: 15, scope: 12, signal: 10, novelty: 8, timeliness: 6, cross: 5, total: 56 },
      credibility: 'medium',
      status: 'confirmed',
      sources: [{ name: '夹具来源', url: 'https://example.org/fixture-beta', date: '2026-01-06', official: false }],
      trends: [{ id: 'tr_fixture-beta', effect: 'weaken', reason: '夹具关系' }],
      timeline: [{ date: '2026-01-06', title: '夹具', note: '夹具' }],
      scoreHistory: [{ date: '2026-01-10', total: 56 }],
      watch: '夹具，无需观察。',
      backfill: true,
    },
  ],
  'trends.json': [
    {
      id: 'tr_fixture-alpha',
      title: '夹具趋势 Alpha',
      thesis: '夹具论点，用于测试修订链与置信度一致性。',
      domain: 'technology',
      firstSeen: '2026-01-01',
      status: 'strengthening',
      confidence: 55,
      indicators: ['夹具指标一'],
      judgment: '夹具判断，可被证伪。',
      verifyBy: '2026-12-31',
      revisions: [{ date: '2026-01-01', from: null, to: 55, reason: '首次建立' }],
    },
    {
      id: 'tr_fixture-beta',
      title: '夹具趋势 Beta',
      thesis: '第二条夹具论点。',
      domain: 'industry',
      firstSeen: '2026-01-02',
      status: 'emerging',
      confidence: 40,
      indicators: ['夹具指标二'],
      judgment: '夹具判断，可被证伪。',
      verifyBy: '2026-12-31',
      revisions: [{ date: '2026-01-02', from: null, to: 40, reason: '首次建立' }],
    },
  ],
  'forecasts.json': [
    {
      id: 'fc_2026-fixture',
      claim: '夹具判断：用于测试置信度与历史末条一致。',
      madeAt: '2026-01-02',
      dueBy: '2026-06-30',
      confidence: 62,
      status: 'open',
      trendId: 'tr_fixture-alpha',
      support: ['夹具支持'],
      counter: ['夹具反证'],
      history: [{ date: '2026-01-02', confidence: 62, reason: '首次提出' }],
    },
  ],
  'snapshots.json': [
    {
      id: 'snap-2026-01-10',
      period: 'weekly',
      date: '2026-01-10',
      title: '第 1 周 · 夹具快照',
      summary: '夹具快照摘要。',
      eventIds: ['ev_fixture-alpha'],
      trendChanges: [{ trendId: 'tr_fixture-beta', from: 'emerging', to: 'strengthening', reason: '夹具变更' }],
    },
  ],
};

const FILES = Object.keys(FIXTURE);

/** 跑一次校验：写临时目录 → 执行 validate.mjs → 解析结果 */
function run({ mutate, omit = [], raw = {}, args = [] } = {}) {
  const data = structuredClone(FIXTURE);
  if (mutate) mutate(data);
  const dir = mkdtempSync(join(tmpdir(), 'era-selftest-'));
  try {
    for (const f of FILES) {
      if (omit.includes(f)) continue;
      writeFileSync(join(dir, f), raw[f] ?? JSON.stringify(data[f], null, 2));
    }
    try {
      const stdout = execFileSync(process.execPath, [join(here, 'validate.mjs'), '--data', dir, ...args], {
        encoding: 'utf8',
        stdio: ['ignore', 'pipe', 'pipe'],
      });
      return { code: 0, out: JSON.parse(stdout), raw: '' };
    } catch (e) {
      if (e.status === undefined) throw e;
      const stderr = (e.stderr || '').trim();
      let parsed = null;
      try {
        parsed = JSON.parse(stderr);
      } catch {
        /* 崩溃也要报出来 */
      }
      return { code: e.status, out: parsed, raw: stderr };
    }
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}

const ev = (d) => d['events.json'];
const tr = (d) => d['trends.json'];
const fc = (d) => d['forecasts.json'];
const sn = (d) => d['snapshots.json'];
const codes = (r) => (r.out?.errors ?? []).map((x) => x.code);
const paths = (r) => (r.out?.errors ?? []).map((x) => x.path);
const warnCodes = (r) => (r.out?.warnings ?? []).map((x) => x.code);

// ────────────────────────────── 用例 ──────────────────────────────
// expect: { code 退出码, code_ 错误码, path 错误路径, warn_ 警告码 }
const cases = [
  { name: '夹具数据应通过', expect: { code: 0 }, mutate: null },

  // id
  { name: 'id 格式不对（大写）', expect: { code: 1, code_: 'FORMAT' }, mutate: (d) => { ev(d)[0].id = 'EV_bad-id'; } },
  { name: 'id 重复', expect: { code: 1, code_: 'DUPLICATE' }, mutate: (d) => { ev(d)[1].id = ev(d)[0].id; } },

  // 总分与评分历史（v1：直接相加）
  { name: 'scores.total 缺失', expect: { code: 1, code_: 'RANGE' }, mutate: (d) => delete ev(d)[0].scores.total },
  { name: 'total 与六维合计不符（81 写成 80）', expect: { code: 1, code_: 'TOTAL_MISMATCH' }, mutate: (d) => { ev(d)[0].scores.total = 80; } },
  { name: 'structural 超 25', expect: { code: 1, code_: 'RANGE' }, mutate: (d) => { ev(d)[0].scores.structural = 26; } },
  { name: 'scope 超 20（各维上限不同）', expect: { code: 1, code_: 'RANGE' }, mutate: (d) => { ev(d)[0].scores.scope = 21; } },
  { name: '同日两条评分（非递减，允许）', expect: { code: 0 }, mutate: (d) => {
    const e = ev(d)[0];
    e.scoreHistory.push({ date: e.scoreHistory[0].date, total: 81, reason: '同一天二次修订' });
  } },
  { name: '改过分却没写 reason（只警告，exit 0）', expect: { code: 0, warn_: 'HISTORY_REASON' }, mutate: (d) => {
    const e = ev(d)[0];
    e.scoreHistory.push({ date: e.scoreHistory[0].date, total: 81 });
  } },
  { name: '评分历史日期倒退', expect: { code: 1, code_: 'ORDER' }, mutate: (d) => {
    ev(d)[0].scoreHistory.push({ date: '2025-12-01', total: 81, reason: '比前一条更早' });
  } },
  { name: '篡改历史末条（不追加）', expect: { code: 1, code_: 'HISTORY_TAIL' }, mutate: (d) => {
    ev(d)[0].scoreHistory[0].total = 80;
  } },
  { name: '首条评分日期不等于采集日', expect: { code: 1, code_: 'HISTORY_HEAD' }, mutate: (d) => {
    ev(d)[0].scoreHistory[0].date = '2026-01-01';
  } },
  { name: 'rationale 用旧键名只警告（exit 0）', expect: { code: 0, warn_: 'RATIONALE_ALIAS' }, mutate: (d) => {
    const r = ev(d)[0].scores.rationale;
    r.delta = r.novelty;
    r.crossDomain = r.cross;
    delete r.novelty;
    delete r.cross;
  } },
  { name: 'rationale 缺分项只警告（exit 0）', expect: { code: 0, warn_: 'RATIONALE' }, mutate: (d) => {
    delete ev(d)[0].scores.rationale.structural;
  } },

  // 时间链
  { name: 'reportedAt 早于 date', expect: { code: 1, code_: 'ORDER' }, mutate: (d) => { ev(d)[0].reportedAt = '2020-01-01'; } },
  { name: 'collectedAt 早于 reportedAt', expect: { code: 1, code_: 'ORDER' }, mutate: (d) => { ev(d)[0].collectedAt = '2020-01-01'; } },

  // 四类引用
  { name: '事件→趋势断链', expect: { code: 1, code_: 'DANGLING' }, mutate: (d) => { ev(d)[0].trends[0].id = 'tr_nope'; } },
  { name: '判断→趋势断链', expect: { code: 1, code_: 'DANGLING' }, mutate: (d) => { fc(d)[0].trendId = 'tr_nope'; } },
  { name: '快照→事件断链', expect: { code: 1, code_: 'DANGLING' }, mutate: (d) => { sn(d)[0].eventIds.push('ev_nope'); } },
  { name: '快照→趋势变更断链', expect: { code: 1, code_: 'DANGLING' }, mutate: (d) => { sn(d)[0].trendChanges[0].trendId = 'tr_nope'; } },
  { name: '快照 summary 为空', expect: { code: 1, code_: 'REQUIRED' }, mutate: (d) => { sn(d)[0].summary = '  '; } },
  { name: 'snapshots 为空数组允许（exit 0）', expect: { code: 0 }, mutate: (d) => {
    d['snapshots.json'] = [];
    d['meta.json'].counts.snapshots = 0;
  } },

  // 修订链三条
  { name: '修订 from 与上一条 to 不连续', expect: { code: 1, code_: 'REVISION_CHAIN' }, mutate: (d) => {
    tr(d)[0].revisions.push({ date: '2026-01-03', from: 50, to: 58, reason: '跳号' });
  } },
  { name: '首条修订 from 必须是 null', expect: { code: 1, code_: 'REVISION_CHAIN' }, mutate: (d) => {
    tr(d)[0].revisions[0].from = 20;
  } },
  { name: 'confidence 与末条修订 to 不一致', expect: { code: 1, code_: 'REVISION_CHAIN' }, mutate: (d) => {
    tr(d)[0].revisions.push({ date: '2026-01-03', from: 55, to: 58, reason: '上调' });
  } },
  { name: '同日两次修订（非递减，允许）', expect: { code: 0 }, mutate: (d) => {
    const t = tr(d)[0];
    t.revisions.push({ date: t.revisions[0].date, from: 55, to: 58, reason: '同一天二次修订' });
    t.confidence = 58;
  } },
  { name: '修订日期倒退', expect: { code: 1, code_: 'ORDER' }, mutate: (d) => {
    const t = tr(d)[0];
    t.revisions.push({ date: '2025-12-01', from: 55, to: 58, reason: '比前一条更早' });
    t.confidence = 58;
  } },
  { name: 'revisions.from 越界', expect: { code: 1, code_: 'RANGE' }, mutate: (d) => {
    tr(d)[0].revisions.push({ date: '2026-01-03', from: 150, to: 58, reason: '越界' });
  } },
  { name: '趋势 confidence 越界', expect: { code: 1, code_: 'RANGE' }, mutate: (d) => { tr(d)[0].confidence = 120; } },

  // 判断
  { name: '判断 history.confidence 越界', expect: { code: 1, code_: 'RANGE' }, mutate: (d) => { fc(d)[0].history[0].confidence = 130; } },
  { name: '判断 confidence 与末条历史不一致', expect: { code: 1, code_: 'REVISION_CHAIN' }, mutate: (d) => { fc(d)[0].confidence = 70; } },
  { name: 'dueBy 早于 madeAt', expect: { code: 1, code_: 'ORDER' }, mutate: (d) => { fc(d)[0].dueBy = '2025-01-01'; } },

  // meta
  { name: 'meta.runStatus 非法', expect: { code: 1, code_: 'ENUM' }, mutate: (d) => { d['meta.json'].runStatus = 'green'; } },
  { name: 'meta.counts 与实际不符', expect: { code: 1, code_: 'COUNT' }, mutate: (d) => { d['meta.json'].counts.events += 1; } },
  { name: 'meta.generatedAt 不是 ISO（2026/01/10）', expect: { code: 1, code_: 'DATE' }, mutate: (d) => { d['meta.json'].generatedAt = '2026/01/10'; } },

  // 警告不拦发布
  { name: '孤儿趋势只警告（exit 0）', expect: { code: 0, warn_: 'ORPHAN' }, mutate: (d) => {
    const t = structuredClone(tr(d)[0]);
    t.id = 'tr_fixture-orphan';
    t.title = '没人引用的夹具趋势';
    tr(d).push(t);
    d['meta.json'].counts.trends += 1;
  } },

  // URL 收紧
  { name: 'URL 带 userinfo 被拒', expect: { code: 1, code_: 'URL' }, mutate: (d) => {
    ev(d)[0].sources[0].url = 'https://user:pass@example.com/x';
  } },
  { name: 'URL 带 token 参数被拒', expect: { code: 1, code_: 'URL' }, mutate: (d) => {
    ev(d)[0].sources[0].url = 'https://example.com/x?token=abc123';
  } },
  { name: 'URL 带 sig 参数被拒', expect: { code: 1, code_: 'URL' }, mutate: (d) => {
    ev(d)[0].sources[0].url = 'https://example.com/x?sig=abc123';
  } },
  { name: 'URL 普通查询参数放行', expect: { code: 0 }, mutate: (d) => {
    ev(d)[0].sources[0].url = 'https://example.com/report?id=7&lang=zh';
  } },
  { name: 'URL 非 http 协议被拒', expect: { code: 1, code_: 'URL' }, mutate: (d) => {
    ev(d)[0].sources[0].url = 'ftp://example.com/x';
  } },

  // 用法与文件
  { name: '缺数据文件 → 退出码 2', expect: { code: 2, code_: 'MISSING_FILE' }, omit: ['meta.json'] },
  { name: '坏 JSON → 退出码 2', expect: { code: 2, code_: 'PARSE' }, raw: { 'events.json': '[{ 不是 JSON' } },
  { name: '未知参数 → 退出码 2', expect: { code: 2, code_: 'USAGE' }, args: ['--bogus'] },
];

let failed = 0;
for (const c of cases) {
  const r = run(c);
  const want = c.expect ?? {};
  const problems = [];
  if (r.code !== want.code) problems.push(`退出码 ${r.code} ≠ ${want.code}`);
  if (want.code_ && !codes(r).includes(want.code_)) problems.push(`缺错误码 ${want.code_}（实际 ${codes(r).join(',') || '无'}）`);
  if (want.path && !paths(r).includes(want.path)) problems.push(`缺错误路径 ${want.path}（实际 ${paths(r).slice(0, 3).join(',') || '无'}）`);
  if (want.warn_ && !warnCodes(r).includes(want.warn_)) problems.push(`缺警告 ${want.warn_}`);
  if (want.code === 0 && codes(r).length > 0) problems.push(`预期通过却报了 ${codes(r).join(',')}`);
  if (problems.length) {
    failed += 1;
    console.error(`✗ ${c.name}`);
    for (const p of problems) console.error(`    ${p}`);
    if (r.raw) console.error(`    stderr: ${r.raw.slice(0, 300)}`);
  } else {
    console.log(`✓ ${c.name}`);
    if (verbose && codes(r).length) console.log(`    ${codes(r).join(', ')}`);
  }
}

console.log(`\n${cases.length - failed}/${cases.length} 通过`);
process.exit(failed === 0 ? 0 : 1);
