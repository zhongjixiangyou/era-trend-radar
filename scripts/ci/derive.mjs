#!/usr/bin/env node
/**
 * 派生层入口（CI 用）：content/ → v1 导出 → 站点投影。
 *
 * 为什么要有这一步：export_astro.py 产的是 v1 契约（dict 包 events[]），
 * 站点吃的是投影后的数组；build.mjs 要求 src/data 与导出产物逐字节一致，
 * 所以两边必须走同一条链：先导到 .era-tmp/v1，再投影到 .era-tmp/derived。
 * 这里只写临时目录，不碰 src/data；src/data 由 `node scripts/from-export.mjs --dir .era-tmp/v1` 同步。
 */
import { spawnSync } from 'node:child_process';
import { rmSync, mkdirSync, cpSync, readdirSync, readFileSync, writeFileSync } from 'node:fs';

const run = (cmd, argv) => {
  const r = spawnSync(cmd, argv, { stdio: 'inherit' });
  if (r.status !== 0) process.exit(r.status ?? 1);
};

const modeArg = process.argv.indexOf('--mode');
const MODE = modeArg >= 0 ? process.argv[modeArg + 1] : 'public';
if (MODE !== 'public' && MODE !== 'preview') {
  console.error(`--mode 只能是 public|preview，收到 ${MODE}`);
  process.exit(2);
}

rmSync('.era-tmp', { recursive: true, force: true });
mkdirSync('.era-tmp/v1', { recursive: true });
mkdirSync('.era-tmp/derived', { recursive: true });

// 公开导出写 v1/ 根；内部预览写 v1/preview/（export_astro.py --mode both 两份都写）。
run('python3', ['scripts/export_astro.py', '--content', 'content', '--out', '.era-tmp/v1', '--schema', 'schema', '--json', '--mode', 'both']);
const srcDir = MODE === 'preview' ? '.era-tmp/v1/preview' : '.era-tmp/v1';
run(process.execPath, ['scripts/from-export.mjs', '--dir', srcDir, '--out', '.era-tmp/derived', '--mode', MODE]);

// meta.json 保留参与比对：只有 generatedAt 这类非确定时间字段被 build.mjs 归一，
// runStatus/counts/mode/哈希仍然要一致（Codex 最终集成边界）。
// 异军突起：公开模式只能看 published 事件，否则 pending 的活动量会泄漏成「加速」。
let emergingContent = 'content';
if (MODE === 'public') {
  emergingContent = '.era-tmp/content-public';
  mkdirSync(emergingContent + '/events', { recursive: true });
  for (const name of readdirSync('content/events')) {
    if (!name.endsWith('.json')) continue;
    const doc = JSON.parse(readFileSync('content/events/' + name, 'utf8'));
    if (doc.status === 'published') cpSync('content/events/' + name, emergingContent + '/events/' + name);
  }
}
run('python3', ['scripts/emerging.py', '--content', emergingContent, '--out', '.era-tmp/derived/emerging.json']);

console.log(`[derive] mode=${MODE}`);
