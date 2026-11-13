// ERA 验收：公共工具（基址探测、溢出扫描、资源前缀检查、截图）
import { mkdir, writeFile } from 'node:fs/promises';
import path from 'node:path';

export const BASE_PATH = (process.env.BASE_PATH || '/era-trend-radar').replace(/\/+$/, '');
export const ORIGIN = process.env.ORIGIN || 'http://127.0.0.1:4321';
export const SCREENS_DIR = process.env.SCREENS_DIR || path.resolve(import.meta.dirname, '..', 'screens');
export const REPORT_PATH = process.env.REPORT_PATH || path.resolve(import.meta.dirname, '..', 'report.json');

async function probe(url) {
  try {
    const res = await fetch(url, { redirect: 'manual' });
    return res.status;
  } catch {
    return 0;
  }
}

/**
 * dev / preview 两种模式下 base 是否生效不一样，这里实测决定。
 * 返回 { origin, basePath, mode }，mode=base|root|none
 */
export async function resolveBase() {
  const withBase = `${ORIGIN}${BASE_PATH}/`;
  const root = `${ORIGIN}/`;
  const s1 = await probe(withBase);
  const s2 = await probe(root);

  if (s2 === 0 && s1 === 0) {
    return { origin: ORIGIN, basePath: '', mode: 'none', reason: `连不上 ${ORIGIN}（服务没起？）` };
  }
  if (s1 === 200) return { origin: ORIGIN, basePath: BASE_PATH, mode: 'base' };
  if (s2 === 200) return { origin: ORIGIN, basePath: '', mode: 'root' };
  return { origin: ORIGIN, basePath: '', mode: 'none', reason: `根路径 ${s2}，base 路径 ${s1}` };
}

/** 页面里根绝对路径（/xxx）但没有 base 前缀的资源——Pages 子路径部署会 404 */
export async function findUnprefixedAssets(page, basePath) {
  if (!basePath) return [];
  const urls = await page.evaluate(() => {
    const out = [];
    for (const el of document.querySelectorAll('[href],[src]')) {
      for (const attr of ['href', 'src']) {
        const v = el.getAttribute(attr);
        if (v && v.startsWith('/') && !v.startsWith('//')) out.push({ attr, url: v, tag: el.tagName.toLowerCase() });
      }
    }
    return out;
  });
  return urls.filter((u) => u.url !== basePath && !u.url.startsWith(`${basePath}/`));
}

/** 390px 下找横向溢出元素 */
export async function scanOverflow(page) {
  return page.evaluate(() => {
    const vw = document.documentElement.clientWidth;
    const docOverflow = document.documentElement.scrollWidth - vw;
    const offenders = [];
    for (const el of document.querySelectorAll('body *')) {
      const r = el.getBoundingClientRect();
      if (r.width === 0 && r.height === 0) continue;
      const over = Math.max(r.right - vw, -r.left);
      if (over > 1) {
        const id = el.id ? `#${el.id}` : '';
        const cls = el.className && typeof el.className === 'string' ? `.${el.className.trim().split(/\s+/).slice(0, 2).join('.')}` : '';
        offenders.push({
          selector: `${el.tagName.toLowerCase()}${id}${cls}`,
          right: Math.round(r.right),
          left: Math.round(r.left),
          width: Math.round(r.width),
          over: Math.round(over),
          text: (el.textContent || '').trim().slice(0, 40),
        });
      }
    }
    offenders.sort((a, b) => b.over - a.over);
    return { vw, docOverflow, offenders: offenders.slice(0, 8) };
  });
}

export async function ensureDirs() {
  await mkdir(SCREENS_DIR, { recursive: true });
}

export async function saveScreenshot(page, name) {
  await ensureDirs();
  const file = path.join(SCREENS_DIR, `${name}.png`);
  await page.screenshot({ path: file, fullPage: false });
  return file;
}

export async function writeReport(report) {
  await mkdir(path.dirname(REPORT_PATH), { recursive: true });
  await writeFile(REPORT_PATH, JSON.stringify(report, null, 2), 'utf8');
  return REPORT_PATH;
}

export function makeChecker() {
  const checks = [];
  return {
    checks,
    pass(id, name, detail = {}) {
      checks.push({ id, name, status: 'pass', detail });
      console.log(`  PASS  ${id}  ${name}`);
    },
    fail(id, name, detail = {}) {
      checks.push({ id, name, status: 'fail', detail });
      console.log(`  FAIL  ${id}  ${name}  ${JSON.stringify(detail).slice(0, 300)}`);
    },
    skip(id, name, detail = {}) {
      checks.push({ id, name, status: 'skip', detail });
      console.log(`  SKIP  ${id}  ${name}  ${JSON.stringify(detail).slice(0, 200)}`);
    },
    get failed() {
      return checks.filter((c) => c.status === 'fail');
    },
  };
}
