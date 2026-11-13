#!/usr/bin/env node
// scripts/ci/build.mjs — 构建静态站到 artifacts/site。
// 一致性：站点里实际渲染的数据文件哈希必须等于 content/ 源哈希；不等就失败（防「校验的目录站点不读」）。
// 边界：_intake/ 不得出现在产物里（双向检查：源目录与产物目录）。
import fs from "node:fs";
import path from "node:path";
import { loadConfig, parseArgs, readJson, writeJson, resolvePath, ensureDir, relative, sha256, run } from "./lib/common.mjs";
import { emitStatus, exitFor } from "./lib/status.mjs";
import { entryOf } from "./lib/entry.mjs";

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
      const er = run(exportCmd[0], exportCmd.slice(1), { timeoutMs: 5 * 60 * 1000 });
      exportExit = er.status;
      exportErr = (er.stderr || "").split("\n").slice(-5).join("\n");
      if (er.status === 0) {
        for (const rel of walk(tmpDir)) {
          compared.push(rel);
          const mine = path.join(tmpDir, rel);
          const theirs = path.join(derivedDir, rel);
          if (!fs.existsSync(theirs)) drift.push(`缺 ${rel}`);
          else if (sha256(fs.readFileSync(mine)) !== sha256(fs.readFileSync(theirs))) drift.push(`漂移 ${rel}`);
        }
      }
    }

    const res = run(entry.argv[0], entry.argv.slice(1), { timeoutMs: 18 * 60 * 1000 });
    const files = walk(distDir);
    const intakeHits = files.filter((p) => p.includes("_intake"));
    const hasIndex = files.includes("index.html");
    const ok =
      (exportCmd.length === 0 || exportExit === 0) &&
      drift.length === 0 &&
      res.status === 0 &&
      intakeHits.length === 0 &&
      hasIndex;

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
      parity: drift.length === 0 && compared.length > 0 ? "derived==content" : null,
      status: ok ? "healthy" : "failed",
      message: ok
        ? `入口构建 ${files.length} 文件；${compared.length} 个派生文件与 content/ 无漂移`
        : [
            exportExit !== 0 && exportCmd.length ? `导出失败 exit=${exportExit} ${exportErr}` : "",
            drift.length ? `派生层漂移 ${drift.length} 个：${drift.slice(0, 5).join(", ")}` : "",
            res.status !== 0 ? `构建失败 exit=${res.status} ${(res.stderr || "").split("\n").slice(-3).join(" ")}` : "",
            !hasIndex ? "产物缺 index.html" : "",
            intakeHits.length ? `产物泄漏 _intake：${intakeHits.slice(0, 3).join(",")}` : "",
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
