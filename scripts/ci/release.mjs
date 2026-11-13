#!/usr/bin/env node
/**
 * 公开发布构建（CI / Pages 唯一入口）：
 *   content/ → 导出 v1 → 公开投影写 src/data → Astro build → 泄漏检查
 *
 * 关键边界（Codex 最终集成边界）：
 * - 只跑 public 模式：published=0 就产出 0 条，不「等以后有 published 再过滤」；
 * - 预览横幅不是保护，保护是公开产物里根本没有 pending ID；
 * - 本脚本只做构建，不改 content/、不提升发布状态（publish_check 只读）。
 */
import { spawnSync } from 'node:child_process';
import { resolveNode } from './node-runtime.mjs';
import { copyFileSync, cpSync, rmSync, existsSync, mkdtempSync } from 'node:fs';
import os from 'node:os';
import path from 'node:path';

// 公开投影会覆盖 src/data（Astro 的 import 路径固定在这里），但那是「构建期间」的事，
// 不是仓库状态：构建结束（成功、失败、中途 exit）一律把 src/data 还原，
// 免得下一次预览、下一个人的验收、git diff 都踩着 3 条/0 条这种投影残骸。
// 备份必须放在仓库外：derive.mjs 开头就 rm -rf .era-tmp，放里面会被自己删掉（踩过）。
const TMPROOT = mkdtempSync(path.join(os.tmpdir(), 'era-src-data-'));
const BACKUP = path.join(TMPROOT, 'data');
const hadData = existsSync('src/data');
if (hadData) cpSync('src/data', BACKUP, { recursive: true });
process.on('exit', () => {
  if (!hadData) return;
  if (!existsSync(path.join(BACKUP, 'events.json'))) {
    console.error(`\n⚠ 备份缺失（${BACKUP}），src/data 未还原；请手动 git checkout -- src/data`);
    return;
  }
  try {
    rmSync('src/data', { recursive: true, force: true });
    cpSync(BACKUP, 'src/data', { recursive: true });
    console.log('\n↩ src/data 已还原（公开投影只是构建期状态）');
  } catch (e) {
    console.error(`\n⚠ 还原 src/data 失败，请手动 git checkout -- src/data（${e.message}）`);
  }
});

const step = (title, cmd, argv) => {
  console.log(`\n▶ ${title}`);
  const r = spawnSync(cmd, argv, { stdio: 'inherit' });
  if (r.status !== 0) {
    console.error(`✗ ${title} 失败 exit=${r.status}`);
    process.exit(r.status ?? 1);
  }
};

step('导出 v1 + 公开投影到 .era-tmp/derived', process.execPath, ['scripts/ci/derive.mjs', '--mode', 'public']);
step('公开投影写入 src/data', process.execPath, [
  'scripts/from-export.mjs', '--dir', '.era-tmp/v1', '--out', 'src/data', '--mode', 'public',
]);
step('异军突起数据同步到 src/data', process.execPath, ['-e', "import('node:fs').then(fs=>fs.copyFileSync('.era-tmp/derived/emerging.json','src/data/emerging.json'))"]);
const nodeBin = resolveNode();
if (!nodeBin) {
  console.error('✗ 找不到 Node >=22.23.4（Astro 7.4.11 要求），无法构建。');
  process.exit(1);
}
console.log(`\n▶ Astro 构建（node ${nodeBin.version} · ${nodeBin.bin}）`);
step('Astro 构建', nodeBin.bin, ['scripts/ci/astro.mjs', 'build']);
step('公开产物泄漏检查', process.execPath, ['scripts/ci/leakcheck.mjs', 'dist']);

console.log('\n✅ release build 完成：public 模式，pending 未进入 dist');
