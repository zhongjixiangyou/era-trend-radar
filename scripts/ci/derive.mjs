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
import { rmSync, mkdirSync, existsSync, unlinkSync } from 'node:fs';

const run = (cmd, argv) => {
  const r = spawnSync(cmd, argv, { stdio: 'inherit' });
  if (r.status !== 0) process.exit(r.status ?? 1);
};

rmSync('.era-tmp', { recursive: true, force: true });
mkdirSync('.era-tmp/v1', { recursive: true });
mkdirSync('.era-tmp/derived', { recursive: true });

run('python3', ['scripts/export_astro.py', '--content', 'content', '--out', '.era-tmp/v1', '--schema', 'schema', '--json']);
run(process.execPath, ['scripts/from-export.mjs', '--dir', '.era-tmp/v1', '--out', '.era-tmp/derived']);

// meta.json 带本轮生成时间，天生每次不同；它不是事实数据，不参与漂移比对。
const metaOut = '.era-tmp/derived/meta.json';
if (existsSync(metaOut)) unlinkSync(metaOut);
