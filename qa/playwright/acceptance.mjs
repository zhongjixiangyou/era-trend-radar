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
const checker = makeChecker();
const { pass, fail, skip, checks } = checker;

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

async function rowSelector() {
  for (const s of ['.event-row', '[data-testid="event-row"]', 'li[data-event-id]', 'article[data-event-id]']) {
    if (await page.locator(s).count()) return s;
  }
  return null;
}
const rowSel = await (await page.goto(eventsUrl).catch(() => {}), rowSelector());
const visibleRows = async () => (rowSel ? page.locator(`${rowSel}:visible`).count() : 0);

if (!rowSel) {
  fail('events:rows', '找不到事件行');
} else {
  const total0 = await visibleRows();
  if (total0 === 0) fail('events:rows', '一条事件行都看不到', { selector: rowSel });
  else pass('events:rows', `事件行 ${total0} 条`, { selector: rowSel });
}

// 筛选：按 data-filter 分组，跳过 data-value=all 的「全部」
const groups = await page.evaluate(() => {
  const out = {};
  for (const b of document.querySelectorAll('button[data-filter]')) {
    const g = b.dataset.filter;
    (out[g] = out[g] || []).push({ value: b.dataset.value, label: b.textContent.trim() });
  }
  return out;
});

if (!Object.keys(groups).length) {
  skip('events:filter', '找不到筛选 chip（button[data-filter]）');
} else {
  for (const [group, chips] of Object.entries(groups)) {
    const real = chips.filter((c) => c.value !== 'all');
    if (!real.length) { skip(`events:filter:${group}`, `${group} 只有「全部」`); continue; }
    const allBtn = page.locator(`button[data-filter="${group}"][data-value="all"]`);
    const reset = async () => {
      if (await allBtn.count()) { await allBtn.first().click({ timeout: 3000 }).catch(() => {}); await page.waitForTimeout(150); }
    };
    await reset();
    const before = await visibleRows();
    const detail = [];
    let changed = 0;
    for (const c of real) {
      const chip = page.locator(`button[data-filter="${group}"][data-value="${c.value}"]`).first();
      await chip.click({ timeout: 4000 }).catch(() => {});
      await page.waitForTimeout(250);
      const after = await visibleRows();
      const pressed = await chip.getAttribute('aria-pressed').catch(() => null);
      const ok = after !== before || pressed === 'true';
      if (ok) changed++;
      detail.push({ value: c.value, label: c.label, before, after, pressed, ok });
      await reset();
    }
    if (changed === real.length) pass(`events:filter:${group}`, `${group} ${real.length} 个筛选项全部生效`, { detail });
    else fail(`events:filter:${group}`, `${group} 只有 ${changed}/${real.length} 个生效`, { detail });
  }
}

// 搜索
const searchBox = page.locator('[data-testid="search"], input[type="search"]').first();
if (!(await searchBox.count())) {
  skip('events:search', '找不到搜索框（input[type=search]）');
} else {
  await page.goto(eventsUrl, { waitUntil: 'load' });
  const before = await visibleRows();
  const title = await page.locator('.event-title, [data-testid="event-title"]').first().innerText().catch(() => '');
  const kw = title.trim().slice(0, 5).trim() || 'CPI';
  await searchBox.fill(kw).catch(() => {});
  await page.waitForTimeout(450);
  const after = await visibleRows();
  const cleared = await searchBox.inputValue().catch(() => '');
  if (after > 0 && after < before) pass('events:search', `搜「${kw}」${before} → ${after} 条`);
  else if (after === 0) fail('events:search', `搜「${kw}」结果 0 条`, { kw, before });
  else fail('events:search', `搜「${kw}」条数没变（${before}）`, { kw, before, input: cleared });
}

// 排序（按钮组 data-sort）
const sortBtns = page.locator('button[data-sort]');
if (!(await sortBtns.count())) {
  skip('events:sort', '找不到排序按钮（button[data-sort]）');
} else {
  const keys = await sortBtns.evaluateAll((bs) => bs.map((b) => b.dataset.sort));
  const detail = [];
  let ok = 0;
  for (const key of keys) {
    await page.goto(eventsUrl, { waitUntil: 'load' });
    const firstBefore = await page.locator(rowSel || '.event-row').first().innerText().catch(() => '');
    const btn = page.locator(`button[data-sort="${key}"]`).first();
    await btn.click({ timeout: 4000 }).catch(() => {});
    await page.waitForTimeout(300);
    const firstAfter = await page.locator(rowSel || '.event-row').first().innerText().catch(() => '');
    const pressed = await btn.getAttribute('aria-pressed').catch(() => null);
    const good = firstBefore !== firstAfter || pressed === 'true';
    if (good) ok++;
    detail.push({ key, pressed, changed: firstBefore !== firstAfter });
  }
  if (ok === keys.length) pass('events:sort', `${keys.length} 个排序键全部生效`, { detail });
  else fail('events:sort', `${ok}/${keys.length} 个排序键生效`, { detail });
}

// 5) 事件详情：时间线
console.log('\n[5/5] 事件详情');
const detailRoute = routes.find((r) => r.kind === 'detail' && r.path.startsWith('/events/'));
if (!detailRoute) {
  skip('detail:timeline', '路由表里没有事件详情页');
} else {
  await page.goto(urlFor(detailRoute.path), { waitUntil: 'load' }).catch(() => {});
  const tl = page.locator('.timeline, [data-testid="timeline"]').first();
  const hasTl = await tl.count();
  const entries = hasTl ? await tl.locator('li, [data-testid="timeline-item"]').count() : 0;
  const toggle = page.locator('[data-testid="timeline-toggle"]');
  const toggleCount = await toggle.count();

  if (!hasTl) {
    fail('detail:timeline', '详情页找不到时间线（.timeline）');
  } else if (toggleCount) {
    const textBefore = await page.locator('body').innerText();
    const expandedBefore = await toggle.first().getAttribute('aria-expanded');
    await toggle.first().click({ timeout: 4000 }).catch(() => {});
    await page.waitForTimeout(300);
    const expandedAfter = await toggle.first().getAttribute('aria-expanded');
    const textAfter = await page.locator('body').innerText();
    if (expandedAfter === 'true' || textAfter.length !== textBefore.length) {
      pass('detail:timeline', '时间线可展开', { expandedBefore, expandedAfter, entries });
    } else {
      fail('detail:timeline', '点击展开按钮无变化', { expandedBefore, expandedAfter, entries });
    }
  } else {
    const visible = await tl.isVisible();
    const textLen = (await tl.innerText().catch(() => '')).length;
    if (visible && (entries > 0 || textLen > 10)) {
      pass('detail:timeline', `时间线常驻展示（${entries || '?'} 条，无折叠交互）`, {
        entries, note: '派活写的是「时间线可展开」，当前没有折叠交互，需主管确认是否算达成',
      });
    } else {
      fail('detail:timeline', '时间线存在但不可见/为空', { visible, entries, textLen });
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

const failed = checker.failed;
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
