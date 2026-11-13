// scripts/ci/node-runtime.mjs
// 锁定 Astro 可用的 Node：Astro 7.4.11 要求 >=22.23.4。
// 本机默认 /usr/local/bin/node 是 22.19.0，直接跑会失败；这里显式挑一个满足要求的解释器，
// 让本地门禁与 CI 用同一判据，而不是靠调用者记得先 export PATH。
import { execFileSync } from 'node:child_process';
import path from 'node:path';

const MIN = [22, 23, 4];

function parse(v) {
  const m = /^v?(\d+)\.(\d+)\.(\d+)/.exec(String(v || ''));
  return m ? [Number(m[1]), Number(m[2]), Number(m[3])] : null;
}
function gte(a, b) {
  for (let i = 0; i < 3; i++) if (a[i] !== b[i]) return a[i] > b[i];
  return true;
}

export function resolveNode() {
  const seen = new Set();
  const fromPath = (process.env.PATH || '').split(path.delimiter).filter(Boolean).map((d) => path.join(d, 'node'));
  const candidates = [
    process.env.ERA_NODE,
    process.execPath,
    ...fromPath,
    '/opt/homebrew/bin/node',
    '/usr/local/bin/node',
  ].filter(Boolean);

  for (const bin of candidates) {
    if (seen.has(bin)) continue;
    seen.add(bin);
    try {
      const v = execFileSync(bin, ['-p', 'process.versions.node'], { encoding: 'utf8', timeout: 5000 }).trim();
      const p = parse(v);
      if (p && gte(p, MIN)) return { bin, version: v };
    } catch (_) {
      /* 不是可执行文件，继续找 */
    }
  }
  return null;
}

export function nodeEnv(bin) {
  return { ...process.env, PATH: `${path.dirname(bin)}${path.delimiter}${process.env.PATH || ''}` };
}
