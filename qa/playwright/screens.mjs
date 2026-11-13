// 只截图：390×844 手机 + 1440×900 桌面，存 qa/screens/
import { chromium } from 'playwright';
import { getRoutes } from './routes.mjs';
import { ORIGIN, saveScreenshot, resolveBase } from './lib.mjs';

const base = await resolveBase();
if (base.mode === 'none') {
  console.error(`[环境] ${base.reason}`);
  process.exit(2);
}

const root = base.basePath;
const routes = getRoutes();
const browser = await chromium.launch({ headless: true });

for (const [label, vp] of [['mobile', { width: 390, height: 844 }], ['desktop', { width: 1440, height: 900 }]]) {
  const ctx = await browser.newContext({
    viewport: vp,
    deviceScaleFactor: label === 'mobile' ? 2 : 1,
    isMobile: label === 'mobile',
    hasTouch: label === 'mobile',
    locale: 'zh-CN',
  });
  const page = await ctx.newPage();
  for (const r of routes) {
    const url = `${ORIGIN}${root}${r.path === '/' ? '/' : r.path}`;
    await page.goto(url, { waitUntil: 'load', timeout: 20000 }).catch(() => {});
    await page.waitForTimeout(150);
    const f = await saveScreenshot(page, `${label}-${r.name}`);
    console.log(`${f}`);
  }
  await ctx.close();
}

await browser.close();
