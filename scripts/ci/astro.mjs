#!/usr/bin/env node
// 用满足 Astro 要求的 Node 跑 astro 子命令：node scripts/ci/astro.mjs build
import { spawnSync } from 'node:child_process';
import { resolveNode, nodeEnv } from './node-runtime.mjs';

const args = process.argv.slice(2);
const found = resolveNode();
if (!found) {
  console.error('✗ 找不到 Node >=22.23.4（Astro 7.4.11 要求）。装一个 LTS，或设 ERA_NODE=/path/to/node。');
  process.exit(1);
}
const r = spawnSync('pnpm', ['exec', 'astro', ...args], { stdio: 'inherit', env: nodeEnv(found.bin) });
process.exit(r.status ?? 1);
