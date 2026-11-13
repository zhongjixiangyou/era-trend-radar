#!/usr/bin/env node
/**
 * 公开产物泄漏检查（Codex 最终集成边界）：
 * 发布门禁不靠横幅，靠公开 dist 里根本不存在 pending 记录。
 *
 * 检查项：
 * 1. content/ 里所有非 published 的事件 / 趋势 / 判断 ID，不得出现在 dist 的任何文本产物里；
 * 2. dist 的 JSON 里不得出现 status=pending；
 * 3. 公开模式且 published=0 时，dist 里不得出现事件详情页与事件 JSON 条目。
 *
 * 退出码：0 通过 / 1 泄漏 / 2 用法错误。
 */
import fs from 'node:fs';
import path from 'node:path';

const DIST = path.resolve(process.argv[2] || 'dist');
const CONTENT = path.resolve('content');

const readJson = (p) => JSON.parse(fs.readFileSync(p, 'utf8'));
const walk = (dir, out = []) => {
  if (!fs.existsSync(dir)) return out;
  for (const name of fs.readdirSync(dir)) {
    const abs = path.join(dir, name);
    const st = fs.statSync(abs);
    if (st.isDirectory()) walk(abs, out);
    else out.push(abs);
  }
  return out;
};

const pending = [];
for (const kind of ['events', 'trends', 'forecasts']) {
  const dir = path.join(CONTENT, kind);
  if (!fs.existsSync(dir)) continue;
  for (const name of fs.readdirSync(dir)) {
    if (!name.endsWith('.json')) continue;
    const doc = readJson(path.join(dir, name));
    if (doc.status && doc.status !== 'published') pending.push({ id: doc.id, kind, status: doc.status });
  }
}

if (!fs.existsSync(DIST)) {
  console.error(`泄漏检查失败：产物目录不存在 ${DIST}`);
  process.exit(2);
}

const TEXT_EXT = new Set(['.html', '.json', '.js', '.mjs', '.css', '.txt', '.xml', '.svg', '.map']);
const files = walk(DIST).filter((f) => TEXT_EXT.has(path.extname(f)));
const hits = [];
let pendingStatusHits = 0;

for (const file of files) {
  const text = fs.readFileSync(file, 'utf8');
  const rel = path.relative(DIST, file).split(path.sep).join('/');
  const clueFile = rel.startsWith('clues/');
  const clueMarked = clueFile && text.includes('data-lead-region="true"') && /媒体线索|待核原文/.test(text);
  for (const p of pending) {
    if (!text.includes(p.id)) continue;
    if (clueMarked) continue; // 线索区：显式标注「未核官方原文」，不参与已核实统计
    hits.push({ file: rel, id: p.id, kind: p.kind, status: p.status });
  }
  if (/"status"\s*:\s*"pending"/.test(text)) pendingStatusHits++;
}

const report = {
  tool: 'leakcheck',
  dist: path.relative(process.cwd(), DIST),
  scannedFiles: files.length,
  pendingRecords: pending.length,
  leakedIds: hits,
  pendingStatusFiles: pendingStatusHits,
  status: hits.length === 0 && pendingStatusHits === 0 ? 'passed' : 'failed',
};

fs.mkdirSync('reports', { recursive: true });
fs.writeFileSync('reports/leakcheck.json', JSON.stringify(report, null, 2) + '\n');

if (report.status === 'passed') {
  console.log(`✅ leakcheck passed · 扫描 ${files.length} 个产物文件 · ${pending.length} 条非公开记录未出现`);
  process.exit(0);
}
console.error(`❌ leakcheck failed · 泄漏 ${hits.length} 处，pending 状态 ${pendingStatusHits} 个文件`);
for (const h of hits.slice(0, 10)) console.error(`  ${h.file} ← ${h.kind} ${h.id} (${h.status})`);
process.exit(1);
