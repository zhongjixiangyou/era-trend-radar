#!/usr/bin/env node
// scripts/ci/build.mjs — 构建静态站到 artifacts/site。
// 一致性：站点里实际渲染的数据文件哈希必须等于 content/ 源哈希；不等就失败（防「校验的目录站点不读」）。
// 边界：_intake/ 不得出现在产物里（双向检查：源目录与产物目录）。
import fs from "node:fs";
import path from "node:path";
import { loadConfig, parseArgs, readJson, writeJson, resolvePath, ensureDir, relative, sha256, run } from "./lib/common.mjs";
import { emitStatus, exitFor } from "./lib/status.mjs";
import { entryOf } from "./lib/entry.mjs";
import { previewState, checkPreviewGuards } from "./lib/preview-guards.mjs";

// 与 scripts/ci/leakcheck.mjs 同一检测集：events / trends / forecasts 三类的非 published ID。
// 以前只扫 events，趋势和判断泄漏了也算 healthy。
function pendingIds(contentDir) {
  const ids = new Set();
  for (const kind of ["events", "trends", "forecasts"]) {
    const dir = path.join(contentDir, kind);
    if (!fs.existsSync(dir)) continue;
    for (const f of fs.readdirSync(dir)) {
      if (!f.endsWith(".json")) continue;
      let j;
      try { j = JSON.parse(fs.readFileSync(path.join(dir, f), "utf8")); } catch (_) { continue; }
      const recs = Array.isArray(j) ? j : (Array.isArray(j[kind]) ? j[kind] : [j]);
      // 只认「显式声明了非 published 状态」的记录：trends/forecasts 往往没有 status 字段，
      // 而它们会随已发布事件合法进入公开产物（按引用收进投影），当成 pending 会误报整站泄漏。
      for (const r of recs) {
        if (r && typeof r.id === "string" && typeof r.status === "string" && r.status !== "published") ids.add(r.id);
      }
    }
  }
  return ids;
}
function scanLeak(distDir, ids) {
  const hits = [];
  if (!ids.size) return hits;
  for (const rel of walk(distDir)) {
    if (!/\.(html|json|js|mjs|cjs|css|map|csv|txt|xml|svg)$/.test(rel)) continue;
    const abs = path.join(distDir, rel);
    let text;
    try { text = fs.readFileSync(abs, "utf8"); } catch (_) { continue; }
    for (const id of ids) if (text.includes(id)) hits.push(`${rel}:${id}`);
  }
  return hits;
}

function canonicalize(v) {
  if (Array.isArray(v)) return v.map(canonicalize);
  if (v && typeof v === "object") {
    const out = {};
    // 窗口对象（带 days 的 from/to）随 asOf 滑动，属于非确定时间，不算漂移。
    const isWindow = typeof v.days === "number" && ("from" in v || "to" in v);
    for (const k of Object.keys(v).sort()) {
      if (k === "generatedAt" || k === "generated_at" || k === "asOf" ||
          (isWindow && (k === "from" || k === "to"))) { out[k] = "<volatile>"; continue; }
      out[k] = canonicalize(v[k]);
    }
    return out;
  }
  return v;
}
function markVolatile(v) {
  // asOf 只有明确标为运行时刻（asOfSource=run_time）时才算非确定字段；
  // 显式 --as-of 或数据回放得到的 asOf 属于内容，必须参与比对。
  if (v && typeof v === "object" && !Array.isArray(v)) {
    if (v.asOfSource === "run_time" && Object.prototype.hasOwnProperty.call(v, "asOf")) v.asOf = "<volatile>";
  }
  return v;
}
function canonHash(file) {
  const text = fs.readFileSync(file, "utf8");
  let parsed;
  try { parsed = JSON.parse(text); } catch (_) { return sha256(text); }
  return sha256(JSON.stringify(canonicalize(markVolatile(parsed))));
}
function shapeKind(file) {
  try { const j = JSON.parse(fs.readFileSync(file, "utf8")); return Array.isArray(j) ? "array" : (j && typeof j === "object" ? "object" : "scalar"); }
  catch (_) { return "raw"; }
}

const TOOL = "build";

function walk(dir, base = dir, out = []) {
  if (!fs.existsSync(dir)) return out;
  for (const name of fs.readdirSync(dir)) {
    const abs = path.join(dir, name);
    const st = fs.statSync(abs);
    if (st.isDirectory()) walk(abs, base, out);
    else out.push(path.relative(base, abs).split(path.sep).join("/"));
  }
  return out;
}

const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);

function render(cfg, doc) {
  const events = (doc.events || []).slice().sort((a, b) => b.score.displayedTotal - a.score.displayedTotal);
  const trends = doc.trends || [];
  const cards = events
    .map(
      (e) => `<article class="card"><h2>${esc(e.title)}</h2><p class="meta">${esc(e.dates?.occurred || "")} · 评分 ${esc(e.score.displayedTotal)} · ${esc((e.sources || []).map((s) => s.publisher || "source").join(" / "))}</p><p>${esc(e.summary || "")}</p><p class="links">${(e.trendLinks || []).map((l) => `<span>${esc(l.trendId)} ${esc(l.direction)}</span>`).join("")}</p></article>`,
    )
    .join("\n");
  const list = trends.map((t) => `<li><strong>${esc(t.name)}</strong> — ${esc(t.firstJudgment?.text || "")}</li>`).join("\n");
  return `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>ERA 趋势雷达</title><style>body{margin:0;font:16px/1.7 -apple-system,system-ui,"PingFang SC",sans-serif;background:#0e1116;color:#e8eaed}main{max-width:820px;margin:0 auto;padding:40px 20px}h1{font-size:26px;letter-spacing:.02em}.card{border:1px solid #26303d;border-radius:12px;padding:18px 20px;margin:16px 0;background:#141922}.meta{color:#8b98a8;font-size:13px}.links span{display:inline-block;margin-right:8px;font-size:12px;color:#7fd1c0}.trends{color:#b9c4d0;font-size:14px}footer{color:#6b7787;font-size:12px;margin-top:32px}</style></head><body><main><h1>ERA 时代趋势雷达</h1><p class="meta">窗口 ${esc(cfg.window.start)} → ${esc(cfg.window.end)} · 生成于 ${esc(doc.generatedAt)} · schema ${esc(cfg.schemaVersion)}</p>${cards}<h2>长期趋势</h2><ul class="trends">${list}</ul><footer>数据校验与四态运行日志见 reports/；本页只渲染 displayedTotal，不重算。</footer></main></body></html>`;
}

function main() {
  const args = parseArgs();
  for (const k of ["content", "out", "report"]) if (args[k] === true) process.exit(exitForSafe("usage_error", 2));
  // --expect 写错（--expect=xxx、裸 --expect）必须当场拒绝，不能退回「产物自证形态」：
  // 这条门禁存在的意义就是不让产物决定自己该长什么样。放在构建之前，坏参数不浪费一次全量构建。
  {
    const hasExpect = Object.prototype.hasOwnProperty.call(args, "expect");
    const raw = args.expect;
    if (hasExpect && !(typeof raw === "string" && (raw === "public" || raw === "preview"))) {
      console.error(`✗ --expect 只能是 public|preview（收到 ${JSON.stringify(raw ?? null)}）`);
      process.exit(exitForSafe("usage_error", 2));
    }
  }
  const cfg = loadConfig(args.config || undefined);
  const contentDir = resolvePath(args.content || cfg.data.dir);
  const dataFile = path.join(contentDir, cfg.data.file);
  const outDir = resolvePath(args.out || "artifacts/site");
  const reportPath = args.report || cfg.entries?.build?.report || "reports/build.json";

  // 外部构建入口（一组 Astro）：权威是 content/ 目录树，派生是 src/data/。
  // 门禁 = ①从 content/ 重新导出到临时目录，与 src/data 逐文件比对无漂移；②构建成功；③产物含 index.html 且未泄漏 _intake。
  const entry = entryOf(cfg, "build");
  if (entry) {
    const ecfg = cfg.entries.build || {};
    const distDir = resolvePath(args.out || ecfg.out || "dist");
    const derivedDir = resolvePath(ecfg.derivedDir || "src/data");
    const exportCmd = Array.isArray(ecfg.exportCmd) ? ecfg.exportCmd : [];
    const tmpDir = resolvePath(ecfg.exportOut || ".era-tmp/derived");

    let exportExit = null;
    let exportErr = "";
    const drift = [];
    const compared = [];
    if (exportCmd.length) {
      fs.rmSync(tmpDir, { recursive: true, force: true });
      ensureDir(tmpDir);
      const chain = Array.isArray(ecfg.exportChain) && ecfg.exportChain.length ? ecfg.exportChain : [exportCmd];
      let chainOk = true;
      for (const step of chain) {
        const er = run(step[0], step.slice(1), { timeoutMs: 5 * 60 * 1000 });
        exportExit = er.status;
        if (er.status !== 0) {
          chainOk = false;
          exportErr = `步骤 ${step[0]} 失败：` + (er.stderr || "").split("\n").slice(-5).join("\n");
          break;
        }
      }
      if (chainOk) {
        for (const rel of walk(tmpDir)) {
          compared.push(rel);
          const mine = path.join(tmpDir, rel);
          const theirs = path.join(derivedDir, rel);
          if (!fs.existsSync(theirs)) drift.push(`缺 ${rel}`);
          else if (canonHash(mine) !== canonHash(theirs)) {
            if (process.env.ERA_DEBUG_DRIFT) console.error(`[drift] ${rel} a=${canonHash(mine).slice(0,10)} b=${canonHash(theirs).slice(0,10)}`);
            const a = shapeKind(mine), b = shapeKind(theirs);
            drift.push(a !== b ? `阶段不一致 ${rel}（导出版本 ${a} / 仓库 ${b}）` : `漂移 ${rel}`);
          }
        }
      }
    }

    const res = run(entry.argv[0], entry.argv.slice(1), { timeoutMs: 18 * 60 * 1000 });
    const files = walk(distDir);
    const intakeHits = files.filter((p) => p.includes("_intake"));
    const hasIndex = files.includes("index.html");
    const pending = pendingIds(resolvePath("content"));
    const leakHits = scanLeak(distDir, pending);
    // 产物形态由 dist 自己说了算：全站都没有横幅 = 公开产物（此时一条 pending 都不许泄漏）；
    // 否则是预览产物（必须每页横幅 + noindex，允许 pending 进产物）。
    // 不能拿 src/data 的 meta 判：构建期 src/data 被临时换成公开投影、退出时又还原，
    // 门禁读 meta 时两边口径会打架（2026-11-13 实测：公开产物 10 页被判「缺横幅」而失败）。
    const pv = previewState(derivedDir);
    const guards = checkPreviewGuards(distDir, files, pending);
    const noBannerAtAll = guards.pages > 0 && guards.noBanner.length === guards.pages;
    const looksPublic = noBannerAtAll && guards.noNoindex.length === guards.pages;
    const artifactPreview = !looksPublic;
    // 形态由调用方声明（deploy 传 --expect public），不让产物自证：
    // 公开投影里只要混进一条 pending，全站就会渲染横幅，靠产物反推会把该失败的构建归成「预览产物」放行。
    // 传没传和传得对不对要分开判：裸 `--expect` 解析成 true、写错值也是个字符串，
    // 只判「是不是合法字符串」会让「传了但传错」退回成「没传」，门禁静默失效。
    const hasExpect = Object.prototype.hasOwnProperty.call(args, "expect");
    const expect = typeof args.expect === "string" ? args.expect : null;
    const expectOk = !hasExpect || expect === "public" || expect === "preview";
    const classOk = !hasExpect || (expect === "public" ? looksPublic : artifactPreview);
    const leakOk = looksPublic ? leakHits.length === 0 : guards.ok;
    // 导出入口没配、或导出 0 个文件，等于门禁什么都没比，不能算 healthy（以前这种是静默放行）。
    const exportConfigured = exportCmd.length > 0;
    const exportOk = exportConfigured && exportExit === 0 && compared.length > 0;
    const ok =
      exportOk &&
      drift.length === 0 &&
      res.status === 0 &&
      intakeHits.length === 0 &&
      leakOk &&
      hasIndex &&
      classOk &&
      expectOk;

    const report = {
      tool: TOOL,
      mode: "entry",
      argv: entry.argv,
      exportArgv: exportCmd,
      exportExit,
      derivedDir: relative(derivedDir),
      compared: compared.length,
      drift,
      buildExit: res.status,
      dist: relative(distDir),
      files: files.length,
      hasIndex,
      intakeLeak: intakeHits,
      expect,
      classOk,
      preview: artifactPreview,
      previewState: { published: pv.published, pending: pv.pending, artifact: looksPublic ? "public" : "preview" },
      previewGuards: artifactPreview ? guards : null,
      pendingChecked: pending.size,
      // 预览产物里 pending 是放行项，不要把几十处当成错误读
      pendingLeak: artifactPreview ? [] : leakHits.slice(0, 10),
      pendingSeen: artifactPreview ? leakHits.length : 0,
      parity: drift.length === 0 && compared.length > 0 ? "derived==content" : null,
      status: ok ? "healthy" : "failed",
      message: ok
        ? `入口构建 ${files.length} 文件；${compared.length} 个派生文件与 content/ 无漂移` +
          (artifactPreview ? `；预览产物放行 ${leakHits.length} 处 pending（横幅/noindex 已校验）` : `；公开产物无 pending 泄漏`)
        : [
            !expectOk ? `--expect 只能是 public|preview，收到 ${JSON.stringify(args.expect ?? null)}` : "",
            !classOk ? `产物形态与 --expect ${expect} 不符（${looksPublic ? "公开" : "预览"}产物）` : "",
            !exportConfigured ? "导出入口未配置（entries.build.exportCmd），门禁未做任何比对" : "",
            exportConfigured && exportExit === 0 && compared.length === 0 ? "导出未产出任何文件，无从比对" : "",
            artifactPreview && !leakOk ? `预览产物护栏不全：${guards.summary}` : "",
            exportExit !== 0 && exportCmd.length ? `导出失败 exit=${exportExit} ${exportErr}` : "",
            drift.length ? `派生层漂移 ${drift.length} 个：${drift.slice(0, 5).join(", ")}` : "",
            res.status !== 0 ? `构建失败 exit=${res.status} ${(res.stderr || "").split("\n").slice(-3).join(" ")}` : "",
            !hasIndex ? "产物缺 index.html" : "",
            intakeHits.length ? `产物泄漏 _intake：${intakeHits.slice(0, 3).join(",")}` : "",
            looksPublic && leakHits.length ? `公开产物泄漏未发布记录 ${leakHits.length} 处：${leakHits.slice(0, 5).join(",")}` : "",
          ]
            .filter(Boolean)
            .join("；"),
    };
    writeJson(reportPath, report);
    emitStatus(cfg, TOOL, report.status, { message: report.message });
    process.exit(ok ? 0 : exitFor(cfg, "validation_failed"));
  }

  if (!fs.existsSync(dataFile)) {
    const message = `构建失败：数据文件不存在 ${relative(dataFile)}`;
    emitStatus(cfg, TOOL, "failed", { message });
    writeJson(reportPath, { tool: TOOL, status: "failed", message });
    process.exit(exitFor(cfg, "validation_failed"));
  }
  const sourceHash = sha256(fs.readFileSync(dataFile));
  const intakeInSource = walk(contentDir).filter((p) => p.startsWith("_intake/") || p.includes("/_intake/"));
  const doc = readJson(dataFile);

  // 内置最小静态构建（离线/一组源码未到之前）。
  ensureDir(outDir);
  fs.writeFileSync(path.join(outDir, "index.html"), render(cfg, doc), "utf8");
  fs.copyFileSync(dataFile, path.join(outDir, cfg.data.file));
  fs.writeFileSync(path.join(outDir, "content-hash.txt"), `${sourceHash}  ${relative(dataFile)}\n`, "utf8");
  const builtHash = sha256(fs.readFileSync(path.join(outDir, cfg.data.file)));
  const files = walk(outDir);
  const intakeHits = files.filter((p) => p.includes("_intake"));
  const ok = builtHash === sourceHash && intakeHits.length === 0;
  const report = {
    tool: TOOL,
    mode: "fallback",
    source: relative(dataFile),
    sourceHash,
    builtHash,
    parity: builtHash === sourceHash,
    out: relative(outDir),
    files,
    intakeLeak: intakeHits,
    intakeInSource: intakeInSource.length,
    status: ok ? "degraded" : "failed",
    message: ok
      ? `内置构建产物 ${files.length} 个，数据哈希一致；Astro 入口未配置，产物仅供离线验证（degraded）`
      : `构建一致性失败：source=${sourceHash} built=${builtHash} intake=${intakeHits.join(",")}`,
  };
  writeJson(reportPath, report);
  emitStatus(cfg, TOOL, report.status, { message: report.message });
  process.exit(ok ? 0 : exitFor(cfg, "validation_failed"));
}

function exitForSafe(key, dflt) {
  try {
    return Number(loadConfig()?.exitCodes?.[key] ?? dflt);
  } catch {
    return dflt;
  }
}

try {
  main();
} catch (err) {
  const cfg = (() => {
    try {
      return loadConfig();
    } catch {
      return {};
    }
  })();
  emitStatus(cfg, TOOL, "failed", { message: err.message });
  console.error(`[build] ${err.stack || err.message}`);
  process.exit(1);
}
