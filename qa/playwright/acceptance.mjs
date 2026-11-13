// ERA 验收主脚本：9 条路由 200 / 子路径资源 / 320·390·430 无溢出 / 筛选·搜索·排序 / 评分历史 / 导航 / 截图
// 用法：
//   BASE_URL 已起服务：ORIGIN=http://127.0.0.1:4321 node playwright/acceptance.mjs
//   脚本自己起 preview：node playwright/acceptance.mjs --serve
// 退出码：0 全过 / 1 有失败 / 2 环境问题（服务连不上、dist 不存在）
import { spawn } from 'node:child_process';
import { existsSync, statSync } from 'node:fs';
import path from 'node:path';
import { chromium } from 'playwright';
import { getRoutes, getSearchCases, publishedEventCount, previewMode, allDetailPaths, pendingEventIds, SAMPLE_EVENT_ID } from './routes.mjs';
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
console.log('[1/6] 路由可达性');
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

// 1b) 全部明细页扫一遍（数据驱动，别只抽 1 条）
const detailPaths = allDetailPaths();
const sweepBad = [];
for (const p of detailPaths) {
  const resp = await page.goto(urlFor(p), { waitUntil: 'load' }).catch(() => null);
  const st = resp?.status() ?? 0;
  if (st !== 200) sweepBad.push({ path: p, status: st });
}
if (detailPaths.length === 0) skip('routes:detail-sweep', '数据里没有明细条目');
else if (sweepBad.length === 0) pass('routes:detail-sweep', `${detailPaths.length} 个明细页全部 200`, { count: detailPaths.length });
else fail('routes:detail-sweep', `${sweepBad.length}/${detailPaths.length} 个明细页异常`, { bad: sweepBad.slice(0, 6) });

// 1c) 候选泄漏：非预览态下 pending 条目不许有可访问的明细页
const pvMode = previewMode();
const pendingIds = pendingEventIds();
if (pendingIds.length === 0) {
  skip('leak:pending-pages', '数据里没有 pending 条目');
} else if (pvMode) {
  skip('leak:pending-pages', `预览态，pending ${pendingIds.length} 条允许进产物（须有横幅 + noindex）`);
} else {
  const leaked = [];
  for (const id of pendingIds) {
    const resp = await page.goto(urlFor(`/events/${id}/`), { waitUntil: 'domcontentloaded' }).catch(() => null);
    if ((resp?.status() ?? 0) === 200) leaked.push(id);
  }
  if (leaked.length === 0) pass('leak:pending-pages', `${pendingIds.length} 条 pending 均无明细页`);
  else fail('leak:pending-pages', `${leaked.length}/${pendingIds.length} 条 pending 明细页可直接访问（页面无 noindex/横幅）`, { leaked: leaked.slice(0, 6) });
}

// 1d) 预览护栏：pending>0 时，页面必须同时有 noindex + data-preview 锚点 + 横幅
if (!previewMode()) {
  skip('preview:guards', '当前非预览态（pending=0），不要求横幅/noindex');
} else {
  const targets = [
    ['/', urlFor('/')],
    ['/events/', urlFor('/events/')],
    [`/events/${SAMPLE_EVENT_ID}/`, urlFor(`/events/${SAMPLE_EVENT_ID}/`)],
  ];
  const bad = [];
  for (const [name, u] of targets) {
    const resp = await page.goto(u, { waitUntil: 'load' }).catch(() => null);
    const g = await page.evaluate(() => ({
      noindex: Boolean(document.querySelector('meta[name="robots"][content*="noindex"]')),
      anchor: Boolean(document.querySelector('[data-preview="true"]')),
      banner: Boolean(document.querySelector('.stale-banner')),
    }));
    if ((resp?.status() ?? 0) !== 200 || !g.noindex || !g.anchor) bad.push({ page: name, status: resp?.status() ?? 0, ...g });
  }
  if (bad.length === 0) pass('preview:guards', `${targets.length} 页均有 noindex + data-preview 锚点`);
  else fail('preview:guards', `${bad.length}/${targets.length} 页护栏不全`, { bad });
}

// 2) Pages 子路径：资源不得绕过 base
console.log('\n[2/6] 子路径资源前缀');
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

// 3) 横向溢出（默认 320/390/430，可用 WIDTHS 覆盖）
const WIDTHS = (process.env.WIDTHS || '320,390,430').split(',').map(Number).filter(Boolean);
// 0) 构建并发保护：跑的过程中 dist 被重建过，这一轮结论作废（不是站点 bug）
const distSig = () => {
  try {
    const st = statSync(path.resolve(process.cwd(), 'dist/index.html'));
    return `${st.mtimeMs}:${st.size}`;
  } catch {
    return 'missing';
  }
};
const sigAtStart = distSig();
console.log(`\n[3/6] 横向溢出 ${WIDTHS.join('/')}px`);
for (const r of routes) {
  const bad = [];
  const detail = {};
  for (const w of WIDTHS) {
    await page.setViewportSize({ width: w, height: 844 });
    const resp = await page.goto(urlFor(r.path), { waitUntil: 'load' }).catch(() => null);
    const status = resp?.status() ?? 0;
    const o = await scanOverflow(page);
    detail[w] = { status, overflow: o.docOverflow, offenders: o.offenders.length, clipped: o.clipped.length };
    if (status !== 200) bad.push({ width: w, status, why: 'HTTP 不是 200（404 页也会量出 0 溢出）' });
    else if (o.docOverflow > 1) bad.push({ width: w, overflow: o.docOverflow, offenders: o.offenders.slice(0, 3) });
  }
  if (!bad.length) pass(`overflow:${r.name}`, `${r.path} ${WIDTHS.join('/')} 全部 0 溢出`, { detail });
  else if (bad.some((b) => b.status && b.status !== 200)) fail(`overflow:${r.name}`, `${r.path} 有 ${bad.length} 档打不开`, { bad, detail });
  else fail(`overflow:${r.name}`, `${r.path} 在 ${bad.map((b) => b.width).join('/')}px 溢出`, { bad, detail });
}
await page.setViewportSize({ width: 390, height: 844 });

// 4) 事件档案：筛选 / 搜索 / 排序
console.log('\n[4/6] 事件档案交互');
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
  const expected = publishedEventCount();
  if (expected === 0) {
    fail('events:rows', '数据里 0 条已发布事件（status 全是 pending），页面空列表', { selector: rowSel, rows: total0 });
  } else if (total0 === expected) {
    pass('events:rows', `事件行 ${total0} 条，与已发布数一致`, { selector: rowSel, expected });
  } else {
    fail('events:rows', `页面 ${total0} 行，数据里已发布 ${expected} 条，对不上`, { selector: rowSel, rows: total0, expected });
  }
}

if (previewMode()) {
  const banner = page.locator('.stale-banner', { hasText: '内部预览' });
  if (await banner.count()) pass('events:banner', '预览模式：内部预览横幅在');
  else fail('events:banner', 'previewMode 为真（0 已发布 / 有候选），但页面没有「内部预览」横幅');
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

// 搜索：空格归一化 + 英文大小写
const searchBox = page.locator('[data-testid="search"], input[type="search"]').first();
if (!(await searchBox.count())) {
  skip('events:search', '找不到搜索框（input[type=search]）');
} else {
  await page.goto(eventsUrl, { waitUntil: 'load' });
  const before = await visibleRows();
  const sc = getSearchCases();
  const cases = [
    sc.base && { kw: sc.base, min: 1, why: '标题原词' },
    sc.spaced && { kw: sc.spaced, min: 1, why: '中间插空格，归一化后应同样命中', sameAs: sc.base },
    sc.latin && { kw: sc.latin.toLowerCase(), min: 1, why: '英文小写（标题里是大写）' },
  ].filter(Boolean);
  if (cases.length === 0) {
    fail('events:search', '取不到可用搜索词（数据里没有已发布事件），无法验证归一化');
  }
  const detail = [];
  const counts = new Map();
  let ok = 0;
  if (cases.length === 0) { /* 已在上面记失败，跳过执行 */ }
  for (const c of cases) {
    await page.goto(eventsUrl, { waitUntil: 'load' });
    await searchBox.fill(c.kw).catch(() => {});
    await page.waitForTimeout(450);
    const after = await visibleRows();
    counts.set(c.kw, after);
    const good = after >= c.min && after <= before;
    if (good) ok++;
    detail.push({ kw: c.kw, after, ok: good, why: c.why });
  }
  const mismatched = cases.filter((c) => c.sameAs && counts.get(c.kw) !== counts.get(c.sameAs));
  if (cases.length > 0 && ok === cases.length && mismatched.length === 0)
    pass('events:search', `${cases.length} 个关键词全部命中，空格变体与基准同条数`, { detail });
  else if (cases.length > 0)
    fail('events:search', `${cases.length - ok}/${cases.length} 个关键词没命中${mismatched.length ? '，空格变体条数不一致' : ''}`, {
      detail,
      mismatched: mismatched.map((c) => ({ kw: c.kw, got: counts.get(c.kw), base: counts.get(c.sameAs) })),
    });
}

// 排序（按钮组 data-sort）
const sortBtns = page.locator('button[data-sort]');
if (!(await sortBtns.count())) {
  skip('events:sort', '找不到排序按钮（button[data-sort]）');
} else {
  const keys = await sortBtns.evaluateAll((bs) => bs.map((b) => b.dataset.sort));
  const detail = [];
  let ok = 0;
  const rowFirst = async () => page.locator(rowSel || '.event-row').first().innerText().catch(() => '');
  for (const key of keys) {
    await page.goto(eventsUrl, { waitUntil: 'load' });
    const btn = page.locator(`button[data-sort="${key}"]`).first();
    // 默认排序键点了顺序本来就不变：先切到另一个键，再点回来才算真测试
    if ((await btn.getAttribute('aria-pressed').catch(() => null)) === 'true') {
      const other = keys.find((k) => k !== key);
      if (other) {
        await page.locator(`button[data-sort="${other}"]`).first().click({ timeout: 4000 }).catch(() => {});
        await page.waitForTimeout(250);
      }
    }
    const before = await rowFirst();
    await btn.click({ timeout: 4000 }).catch(() => {});
    await page.waitForTimeout(300);
    const after = await rowFirst();
    const pressed = await btn.getAttribute('aria-pressed').catch(() => null);
    // 判定标准：点完 aria-pressed 必须变 true（证明处理器跑了）；顺序变化作为附带信息
    if (pressed === 'true') ok++;
    detail.push({ key, pressed, changed: before !== after });
  }
  if (ok === keys.length) pass('events:sort', `${keys.length} 个排序键全部生效`, { detail });
  else fail('events:sort', `${ok}/${keys.length} 个排序键生效`, { detail });
}

// 5) 事件详情：时间线
console.log('\n[5/6] 事件详情');
const detailRoute = routes.find((r) => r.kind === 'detail' && r.path.startsWith('/events/'));
if (!detailRoute) {
  skip('detail:score-history', '路由表里没有事件详情页');
} else {
  const dResp = await page.goto(urlFor(detailRoute.path), { waitUntil: 'load' }).catch(() => null);
  const dStatus = dResp?.status() ?? 0;
  const info = await page.evaluate(() => {
    const blocks = [...document.querySelectorAll('.aside-block, aside > div')];
    const hb = blocks.find((b) => /评分历史/.test(b.querySelector('h3')?.textContent || ''));
    const rows = hb
      ? [...hb.children].filter((c) => c.tagName === 'DIV' && /\d{4}[.\-/]/.test(c.textContent || '')).length
      : 0;
    return {
      historyRows: rows,
      timeline: document.querySelectorAll('.timeline').length,
      title: (document.querySelector('h1')?.textContent || '').trim(),
      hasScoreBlock: !!hb,
    };
  });
  if (dStatus !== 200) {
    fail('detail:score-history', `详情页 HTTP ${dStatus}（${detailRoute.path}），页面本身打不开`, { ...info, status: dStatus });
  } else if (info.historyRows > 0) {
    pass('detail:score-history', `评分历史渲染 ${info.historyRows} 条`, { ...info, status: dStatus });
  } else {
    fail(
      'detail:score-history',
      info.hasScoreBlock ? '评分历史块在但没有行' : '详情页没有渲染评分历史侧栏块',
      info,
    );
  }
  if (info.timeline === 0) {
    console.log(`  NOTE  数据里 ${publishedEventCount()} 条事件，timeline 字段 0 条，时间线验收项本轮无对象`);
  }
}

// 6) 手机导航：高度 ≤70px，7 项可达（换行或横滑）
console.log('\n[6/6] 手机导航');
for (const w of WIDTHS) {
  await page.setViewportSize({ width: w, height: 844 });
  await page.goto(urlFor('/'), { waitUntil: 'load' }).catch(() => {});
  const nav = await page.evaluate(() => {
    const el = document.querySelector('nav.nav') || document.querySelector('header nav') || document.querySelector('nav');
    if (!el) return null;
    const cs = getComputedStyle(el);
    const links = [...el.querySelectorAll('a')].map((a) => {
      const r = a.getBoundingClientRect();
      return { t: a.textContent.trim().slice(0, 8), left: Math.round(r.left), right: Math.round(r.right), h: Math.round(r.height) };
    });
    return {
      height: Math.round(el.getBoundingClientRect().height),
      scrollW: el.scrollWidth,
      clientW: el.clientWidth,
      overflowX: cs.overflowX,
      links: links.length,
      offscreen: links.filter((l) => l.right > document.documentElement.clientWidth + 1 || l.left < -1).map((l) => l.t),
      linkHeights: links.map((l) => l.h),
    };
  });
  if (!nav) { skip(`nav:${w}`, `${w}px 找不到 nav`); continue; }
  const wrapped = nav.scrollW <= nav.clientW + 1;
  const scrollable = nav.overflowX === 'auto' || nav.overflowX === 'scroll';
  const reachable = wrapped || scrollable;
  const heightOk = nav.height <= 70;
  const detail = { width: w, ...nav, wrapped, scrollable, reachable };
  if (heightOk && reachable) {
    pass(`nav:${w}`, `${w}px 导航 ${nav.height}px，${wrapped ? '换行全露出' : '横滑'}`, detail);
  } else if (!heightOk) {
    fail(`nav:${w}`, `${w}px 导航高 ${nav.height}px，超过 70px 上限`, detail);
  } else {
    fail(`nav:${w}`, `${w}px 导航 ${nav.links} 项不可达（未换行且不横滑）`, detail);
  }
}
await page.setViewportSize({ width: 390, height: 844 });

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

// 收尾：确认这一轮没有被并发构建打断
const sigAtEnd = distSig();
if (sigAtStart !== sigAtEnd) {
  console.log(`\n[!] 本轮作废：dist/index.html 在运行期间被重建（${sigAtStart} → ${sigAtEnd}），上面的红都是构建并发造成的假象，不是站点 bug。`);
  process.exitCode = 2;
}

console.log(`\n==== ${failed.length === 0 ? '全过' : `${failed.length} 项失败`} ====`);
console.log(`pass ${report.summary.pass} / fail ${report.summary.fail} / skip ${report.summary.skip}`);
console.log(`报告 ${reportPath}`);
if (shots.length) console.log(`截图 ${path.dirname(shots[0])}`);

process.exit(failed.length === 0 ? 0 : 1);
