// ERA 验收主脚本：8 条路由 200 / 子路径资源 / 390×844 无溢出 / 筛选 / 时间线 / 截图
// 用法：
//   BASE_URL 已起服务：ORIGIN=http://127.0.0.1:4321 node playwright/acceptance.mjs
//   脚本自己起 preview：node playwright/acceptance.mjs --serve
// 退出码：0 全过 / 1 有失败 / 2 环境问题（服务连不上、dist 不存在）
import { spawn } from 'node:child_process';
import { existsSync } from 'node:fs';
import path from 'node:path';
import { chromium } from 'playwright';
import { getRoutes } from './routes.mjs';
import {
  BASE_PATH, ORIGIN, findUnprefixedAssets, makeChecker, resolveBase, saveScreenshot,
  scanOverflow, writeReport,
} from './lib.mjs';

const REPO = path.resolve(import.meta.dirname, '..', '..');
const { pass, fail, skip, checks, failed } = makeChecker();

const SEL = {
  rows: ['[data-testid="event-row"]', '.event-row', 'li[data-event-id]', 'article[data-event-id]'],
  chips: ['[data-testid="filter-chip"]', '[data-testid="filters"] button', '[data-testid="filters"] a', 'button[data-filter]', 'a[data-filter]'],
  search: ['[data-testid="search"]', 'input[type="search"]', 'input[placeholder*="搜索"]', 'input[placeholder*="Search"]'],
  sort: ['[data-testid="sort"]', 'select[name="sort"]', 'select[data-testid="sort"]'],
  tlToggle: ['[data-testid="timeline-toggle"]', 'button[aria-expanded]'],
  tlBody: ['[data-testid="timeline-body"]', '[data-timeline-body]'],
};

async function firstMatch(page, selectors) {
  for (const s of selectors) {
    const loc = page.locator(s);
    if (await loc.count()) return { loc, selector: s };
  }
  return null;
}

function startPreview(port) {
  const child = spawn('pnpm', ['exec', 'astro', 'preview', '--port', String(port), '--host', '127.0.0.1'], {
    cwd: REPO, stdio: 'ignore', detached: false,
  });
  child.unref?.();
  return child;
}

async function waitForServer(url, timeoutMs = 30000) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeoutMs) {
    try {
      const r = await fetch(url, { redirect: 'manual' });
      if (r.status > 0) return true;
    } catch { /* not ready */ }
    await new Promise((r) => setTimeout(r, 500));
  }
  return false;
}

// ---------- 主流程 ----------
const serve = process.argv.includes('--serve');
let previewProc = null;

let base;
if (serve) {
  if (!existsSync(path.join(REPO, 'dist'))) {
    console.error('[环境] 没有 dist/，先跑 pnpm build');
    process.exit(2);
  }
  const port = Number(new URL(ORIGIN).port || 4321);
  previewProc = startPreview(port);
  const ok = await waitForServer(`${ORIGIN}${BASE_PATH}/`);
  if (!ok) {
    console.error(`[环境] preview 起在 ${ORIGIN} 没响应`);
    previewProc?.kill();
    process.exit(2);
  }
}

try {
  base = await resolveBase();
} catch {
  base = { mode: 'none', basePath: '', reason: '探测异常' };
}

if (base.mode === 'none') {
  console.error(`[环境] ${base.reason}`);
  await writeReport({ ok: false, env: 'unreachable', base, checks });
  previewProc?.kill();
  process.exit(2);
}

const rootPath = base.basePath; // '' 或 '/era-trend-radar'
const routes = getRoutes();
// 路由表里是站内相对路径；拼到 rootPath 后面
const urlFor = (p) => `${ORIGIN}${rootPath}${p === '/' ? '/' : p}`;

console.log(`[基址] mode=${base.mode} root=${ORIGIN}${rootPath || '/'}  路由 ${routes.length} 条\n`);

const browser = await chromium.launch({ headless: true });
const context = await browser.newContext({
  viewport: { width: 390, height: 844 },
  deviceScaleFactor: 2,
  isMobile: true,
  hasTouch: true,
  locale: 'zh-CN',
});
const page = await context.newPage();

const consoleErrors = [];
const pageErrors = [];
page.on('console', (m) => { if (m.type() === 'error') consoleErrors.push(m.text().slice(0, 300)); });
page.on('pageerror', (e) => pageErrors.push(String(e).slice(0, 300)));

// 1) 8 条路由 200
console.log('[1/5] 路由可达性');
for (const r of routes) {
  const url = urlFor(r.path);
  consoleErrors.length = 0;
  pageErrors.length = 0;
  let status = 0;
  let err = null;
  try {
    const res = await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 20000 });
    status = res ? res.status() : 0;
  } catch (e) {
    err = String(e).slice(0, 200);
  }
  const title = await page.title().catch(() => '');
  if (status === 200) {
    pass(`route:${r.name}`, `${r.path} → 200`, { url, title });
  } else if (err) {
    fail(`route:${r.name}`, `${r.path} 打不开`, { url, error: err });
  } else {
    fail(`route:${r.name}`, `${r.path} → ${status}`, { url, title });
  }
  if (pageErrors.length) fail(`js:${r.name}`, `${r.path} 有未捕获异常`, { pageErrors: [...pageErrors] });
  else if (consoleErrors.length) fail(`console:${r.name}`, `${r.path} 控制台报错`, { consoleErrors: [...consoleErrors] });

  // 等待客户端筛选脚本就绪（有就绪信号时）
  await page.waitForTimeout(150);
}

// 2) Pages 子路径：资源不得绕过 base
console.log('\n[2/5] 子路径资源前缀');
for (const r of routes) {
  await page.goto(urlFor(r.path), { waitUntil: 'domcontentloaded' }).catch(() => {});
  const bad = await findUnprefixedAssets(page, rootPath);
  if (!rootPath) {
    skip(`assets:${r.name}`, `${r.path} 当前是根路径模式，无需前缀检查`, { mode: base.mode });
  } else if (bad.length === 0) {
    pass(`assets:${r.name}`, `${r.path} 资源前缀正确`);
  } else {
    fail(`assets:${r.name}`, `${r.path} 有 ${bad.length} 个资源没带 ${rootPath}`, { sample: bad.slice(0, 5) });
  }
}

// 3) 390×844 无横向溢出
console.log('\n[3/5] 390×844 横向溢出');
for (const r of routes) {
  await page.goto(urlFor(r.path), { waitUntil: 'load' }).catch(() => {});
  const o = await scanOverflow(page);
  if (o.docOverflow <= 1) pass(`overflow:${r.name}`, `${r.path} 溢出 ${o.docOverflow}px`);
  else fail(`overflow:${r.name}`, `${r.path} 溢出 ${o.docOverflow}px`, o);
}

// 4) 事件档案：筛选 / 搜索 / 排序
console.log('\n[4/5] 事件档案交互');
const eventsUrl = urlFor('/events/');
await page.goto(eventsUrl, { waitUntil: 'load' }).catch(() => {});

const rows = await firstMatch(page, SEL.rows);
const countRows = async () => (rows ? rows.loc.count() : 0);
const total0 = await countRows();

if (!rows) {
  skip('events:rows', '找不到事件行（需要 data-testid="event-row"）');
} else {
  pass('events:rows', `事件行 ${total0} 条`);
}

const chips = await firstMatch(page, SEL.chips);
if (!chips) {
  skip('events:filter', '找不到筛选 chip（需要 data-testid="filter-chip"）');
} else {
  const n = await chips.loc.count();
  let changed = 0;
  let tried = 0;
  for (let i = 0; i < n; i++) {
    const chip = chips.loc.nth(i);
    if (!(await chip.isVisible())) continue;
    const before = await countRows();
    const urlBefore = page.url();
    await chip.click({ timeout: 5000 }).catch(() => {});
    await page.waitForTimeout(200);
    const after = await countRows();
    if (after !== before || page.url() !== urlBefore) changed++;
    tried++;
  }
  if (tried === 0) skip('events:filter', '筛选 chip 全部不可见');
  else if (changed > 0) pass('events:filter', `${changed}/${tried} 个 chip 点击后列表/URL 有变化`);
  else fail('events:filter', `${tried} 个 chip 点击后列表和 URL 都没变`, { selector: chips.selector, total0 });
}

const search = await firstMatch(page, SEL.search);
if (!search) {
  skip('events:search', '找不到搜索框（需要 data-testid="search"）');
} else {
  await page.goto(eventsUrl, { waitUntil: 'load' }).catch(() => {});
  const before = await countRows();
  const firstTitle = rows ? (await rows.loc.first().innerText().catch(() => '')).slice(0, 8) : '';
  const keyword = firstTitle || 'a';
  await search.loc.first().fill(keyword).catch(() => {});
  await page.waitForTimeout(400);
  const after = await countRows();
  if (after > 0 && after < before) pass('events:search', `搜「${keyword}」${before} → ${after} 条`);
  else if (after === before) fail('events:search', `搜「${keyword}」条数没变（${before}）`, { keyword });
  else fail('events:search', `搜「${keyword}」条数变 0`, { before, after });
}

const sort = await firstMatch(page, SEL.sort);
if (!sort) {
  skip('events:sort', '找不到排序控件（需要 data-testid="sort"）');
} else {
  await page.goto(eventsUrl, { waitUntil: 'load' }).catch(() => {});
  const beforeFirst = rows ? await rows.loc.first().innerText().catch(() => '') : '';
  const options = await sort.loc.first().locator('option').count().catch(() => 0);
  if (options > 1) {
    await sort.loc.first().selectOption({ index: 1 }).catch(() => {});
    await page.waitForTimeout(300);
    const afterFirst = rows ? await rows.loc.first().innerText().catch(() => '') : '';
    if (afterFirst && afterFirst !== beforeFirst) pass('events:sort', '排序切换后首条变化');
    else fail('events:sort', '排序切换后首条没变', { before: beforeFirst.slice(0, 40) });
  } else {
    skip('events:sort', '排序控件没有可选项');
  }
}

// 5) 事件详情：时间线可展开
console.log('\n[5/5] 事件详情');
const detail = routes.find((r) => r.kind === 'detail' && r.path.startsWith('/events/'));
if (!detail) {
  skip('detail:timeline', '路由表里没有事件详情页');
} else {
  await page.goto(urlFor(detail.path), { waitUntil: 'load' }).catch(() => {});
  const toggle = await firstMatch(page, SEL.tlToggle);
  const body = await firstMatch(page, SEL.tlBody);
  if (!toggle) {
    skip('detail:timeline', '找不到时间线展开按钮（需要 data-testid="timeline-toggle"）');
  } else {
    const expandedBefore = await toggle.loc.first().getAttribute('aria-expanded').catch(() => null);
    const textBefore = await page.locator('body').innerText().catch(() => '');
    await toggle.loc.first().click({ timeout: 5000 }).catch(() => {});
    await page.waitForTimeout(300);
    const expandedAfter = await toggle.loc.first().getAttribute('aria-expanded').catch(() => null);
    const bodyVisible = body ? await body.loc.first().isVisible().catch(() => false) : false;
    const textAfter = await page.locator('body').innerText().catch(() => '');
    const grew = textAfter.length - textBefore.length;
    if (expandedAfter === 'true' || bodyVisible || grew > 20) {
      pass('detail:timeline', '点击后时间线展开', { expandedAfter, bodyVisible, grew });
    } else {
      fail('detail:timeline', '点击后没有展开迹象', { expandedBefore, expandedAfter, bodyVisible, grew });
    }
  }
}

// 截图留证
const shots = [];
for (const r of routes) {
  await page.goto(urlFor(r.path), { waitUntil: 'load' }).catch(() => {});
  await page.waitForTimeout(120);
  shots.push(await saveScreenshot(page, `390-${r.name}`));
}

await browser.close();
previewProc?.kill();

const report = {
  ok: failed.length === 0,
  generatedAt: new Date().toISOString(),
  base: { mode: base.mode, origin: ORIGIN, rootPath },
  summary: {
    total: checks.length,
    pass: checks.filter((c) => c.status === 'pass').length,
    fail: failed.length,
    skip: checks.filter((c) => c.status === 'skip').length,
  },
  checks,
  screens: shots,
};
const reportPath = await writeReport(report);

console.log(`\n==== ${failed.length === 0 ? '全过' : `${failed.length} 项失败`} ====`);
console.log(`pass ${report.summary.pass} / fail ${report.summary.fail} / skip ${report.summary.skip}`);
console.log(`报告 ${reportPath}`);
if (shots.length) console.log(`截图 ${path.dirname(shots[0])}`);

process.exit(failed.length === 0 ? 0 : 1);
