#!/usr/bin/env node
/**
 * 预览态判据 —— build.mjs 的门禁和人工核查共用这一份，避免两边口径漂移。
 *
 * 背景（2026-11-13 定）：published=0 / pending>0 时整站是「内部预览」，
 * 候选内容必然进产物，拿「公开站不许泄漏 pending」去卡它是口径错位。
 * 预览态放行的条件是三条护栏同时在：
 *   ① 每个页面都挂预览横幅（<… data-preview="true">）
 *   ② 每个页面都有 <meta name="robots" content="…noindex…">
 *   ③ 产物里没有 sitemap，或 sitemap 不包含任何 pending id
 * 公开态（published>0）仍然一条 pending 都不许泄漏，规则不变。
 *
 *   node scripts/ci/lib/preview-guards.mjs <dist 目录>   手工核查
 */
import fs from 'node:fs';
import path from 'node:path';

/** 与发布口径对齐：只有「一条都没发布、却有候选」时整站才是内部预览态。
 *  2026-11-13 修正：此前按 pending>0 判定，会让「3 条已发布 + 8 条候选」的公开构建
 *  被要求挂横幅/noindex，而 release.mjs 产出的公开产物本就不含 pending——两套口径打架。
 *  公开构建（published>0）走严格泄漏检查；预览站（published=0）走横幅/noindex 三条护栏。 */
export function previewState(derivedDir) {
  let m = null;
  try {
    m = JSON.parse(fs.readFileSync(path.join(derivedDir, 'meta.json'), 'utf8'));
  } catch {
    return { preview: false, published: null, pending: null };
  }
  const published = m?.preview?.published ?? m?.counts?.published ?? null;
  const pending = m?.preview?.pending ?? m?.counts?.pending ?? null;
  const publishedN = published ?? 0;
  return { preview: publishedN === 0 && (pending ?? 0) > 0, published, pending };
}

function walk(dir, base = dir, out = []) {
  if (!fs.existsSync(dir)) return out;
  for (const name of fs.readdirSync(dir)) {
    const abs = path.join(dir, name);
    const st = fs.statSync(abs);
    if (st.isDirectory()) walk(abs, base, out);
    else out.push(path.relative(base, abs).split(path.sep).join('/'));
  }
  return out;
}

/**
 * @param {string} distDir
 * @param {string[]} [files] 预先遍历好的相对路径，省一次 walk
 * @param {Set<string>} [ids] pending id，用来查 sitemap
 */
export function checkPreviewGuards(distDir, files = walk(distDir), ids = new Set()) {
  const pages = files.filter((f) => f.endsWith('.html'));
  const noBanner = [];
  const noNoindex = [];
  for (const rel of pages) {
    const text = fs.readFileSync(path.join(distDir, rel), 'utf8');
    if (!/data-preview="true"/.test(text)) noBanner.push(rel);
    if (!/<meta[^>]+name="robots"[^>]+content="[^"]*noindex/i.test(text)) noNoindex.push(rel);
  }

  const sitemaps = files.filter((f) => /(^|\/)sitemap[^/]*\.xml$/i.test(f));
  const sitemapLeak = [];
  for (const rel of sitemaps) {
    const text = fs.readFileSync(path.join(distDir, rel), 'utf8');
    for (const id of ids) if (text.includes(id)) sitemapLeak.push(`${rel}:${id}`);
  }

  return {
    pages: pages.length,
    noBanner,
    noNoindex,
    sitemaps,
    sitemapLeak,
    ok: pages.length > 0 && noBanner.length === 0 && noNoindex.length === 0 && sitemapLeak.length === 0,
    summary: [
      noBanner.length ? `${noBanner.length} 个页面没挂预览横幅` : '',
      noNoindex.length ? `${noNoindex.length} 个页面缺 noindex` : '',
      sitemapLeak.length ? `sitemap 泄漏 ${sitemapLeak.length} 处 pending` : '',
    ].filter(Boolean).join('；'),
  };
}

if (process.argv[1] && import.meta.url === `file://${path.resolve(process.argv[1])}`) {
  const dist = path.resolve(process.argv[2] || 'dist');
  const derived = path.resolve(process.argv[3] || process.env.ERA_DERIVED_DIR || 'src/data');
  const pv = previewState(derived);
  const files = walk(dist);
  if (!files.length) {
    console.error(`目录不存在或为空：${dist}`);
    process.exit(2);
  }
  const guards = checkPreviewGuards(dist, files);
  const out = { preview: pv.preview, published: pv.published, pending: pv.pending, dist, ...guards };
  console.log(JSON.stringify(out, null, 2));
  if (!pv.preview) {
    console.error('当前不是预览态（published>0），本工具只核预览态护栏。');
    process.exit(0);
  }
  console.log(guards.ok ? '✓ 预览态护栏齐全' : `✗ 预览态护栏缺失：${guards.summary}`);
  process.exit(guards.ok ? 0 : 1);
}
