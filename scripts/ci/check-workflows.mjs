#!/usr/bin/env node
// scripts/ci/check-workflows.mjs — 工作流静态验收：YAML 可解析 + 权限最小化 + 定时与 config 对齐 +
// action 固定到 commit SHA + 单 job timeout ≤ 20min + concurrency 互斥 + 脚本引用存在。
import fs from "node:fs";
import path from "node:path";
import { ROOT, SCRIPTS, loadConfig, parseArgs, readJson, writeJson, relative } from "./lib/common.mjs";
import { parseYaml } from "./lib/yaml.mjs";

const cfg = loadConfig();
const WF_DIR = path.join(ROOT, ".github/workflows");

const EXPECT = {
  "collect.yml": {
    perms: { contents: "write" },
    group: "era-research",
    jobs: 1,
    crons: cfg.schedule.collect,
    script: "scripts/ci/collect.mjs",
  },
  "analyze.yml": {
    perms: { contents: "write" },
    group: "era-research",
    jobs: 1,
    crons: [cfg.schedule.analyze],
    script: "scripts/ci/analyze.mjs",
  },
  "deploy.yml": {
    perms: { contents: "read", pages: "write", "id-token": "write" },
    group: "pages",
    jobs: 2,
    crons: [],
    script: "scripts/ci/build.mjs",
  },
  "monthly-review.yml": {
    perms: { contents: "write" },
    group: "era-research",
    jobs: 1,
    crons: [cfg.schedule.monthlyReview],
    script: "scripts/ci/monthly-review.mjs",
  },
};

const rows = [];
const errors = [];
function check(name, ok, detail) {
  rows.push({ name, ok, detail: String(detail) });
  if (!ok) errors.push(`${name}：${detail}`);
}

for (const [file, expect] of Object.entries(EXPECT)) {
  const full = path.join(WF_DIR, file);
  if (!fs.existsSync(full)) {
    check(`${file} 存在`, false, "文件缺失");
    continue;
  }
  const text = fs.readFileSync(full, "utf8");
  const { value, errors: yamlErrors } = parseYaml(text, file);
  check(`${file} YAML 解析`, yamlErrors.length === 0, yamlErrors.map((e) => `L${e.line} ${e.message}`).join("; ") || "0 error");
  if (!value || typeof value !== "object") continue;

  check(`${file} name`, typeof value.name === "string" && value.name.length > 0, value.name);
  check(`${file} on`, value.on && typeof value.on === "object", JSON.stringify(Object.keys(value.on || {})));

  // 权限精确匹配（不允许缺失、不允许多余）。
  const perms = value.permissions || {};
  const permKeys = Object.keys(perms).sort().join(",");
  const wantKeys = Object.keys(expect.perms).sort().join(",");
  const permValsOk = Object.entries(expect.perms).every(([k, v]) => perms[k] === v);
  check(`${file} 权限`, permKeys === wantKeys && permValsOk, JSON.stringify(perms));

  // concurrency 互斥。
  check(`${file} concurrency`, value.concurrency?.group === expect.group && value.concurrency?.["cancel-in-progress"] === false, JSON.stringify(value.concurrency));

  // 定时与 config 对齐。
  const crons = (value.on?.schedule || []).map((s) => s?.cron).filter(Boolean);
  check(`${file} cron`, JSON.stringify(crons) === JSON.stringify(expect.crons), `${JSON.stringify(crons)} vs config ${JSON.stringify(expect.crons)}`);
  check(`${file} workflow_dispatch`, Object.prototype.hasOwnProperty.call(value.on || {}, "workflow_dispatch"), "手动触发");

  // job 数与 timeout。
  const jobs = value.jobs || {};
  check(`${file} job 数`, Object.keys(jobs).length === expect.jobs, `${Object.keys(jobs).length} vs ${expect.jobs}`);
  for (const [jid, job] of Object.entries(jobs)) {
    const t = job?.["timeout-minutes"];
    check(`${file}#${jid} timeout`, typeof t === "number" && t > 0 && t <= 20, t);
    check(`${file}#${jid} runs-on`, typeof job?.["runs-on"] === "string", job?.["runs-on"]);
    for (const step of job.steps || []) {
      if (step?.uses) {
        const m = String(step.uses).match(/^([^@]+)@([a-f0-9]{40})$/);
        check(`${file} action 固定`, Boolean(m), step.uses);
      }
      const run = String(step?.run || "");
      if (run.includes("}${{ secrets") || /\$\{\{\s*secrets\.[^}]+\}\}/.test(run) && /echo/.test(run)) {
        check(`${file} 不打印密钥`, false, `${step.name || "step"} 直接把 secrets 拼进 shell`);
      }
    }
  }

  // 引用的 CI 脚本必须存在。
  const scriptRefs = [...text.matchAll(/node\s+(scripts\/ci\/[\w.-]+\.mjs)/g)].map((m) => m[1]);
  check(`${file} 入口脚本`, scriptRefs.includes(expect.script), scriptRefs.join(", ") || "未引用");
  for (const rel of new Set(scriptRefs)) {
    check(`${file} 脚本存在 ${rel}`, fs.existsSync(path.join(ROOT, rel)), rel);
  }

  // 不用 PR 触发写权限（避免 fork 提权）。
  check(`${file} 无 PR 触发`, !value.on?.pull_request && !value.on?.pull_request_target, "ok");

  // 不允许把 python 命令写死在 YAML 里（入口只在 config.entries）。
  check(`${file} 不硬编码 python`, !/python3\s/.test(text), "python 入口只在 config.entries");
}

// 四份文件都要在，且部署必须带 environment: github-pages。
const deploy = fs.existsSync(path.join(WF_DIR, "deploy.yml")) ? parseYaml(fs.readFileSync(path.join(WF_DIR, "deploy.yml"), "utf8"), "deploy").value : null;
check("deploy environment", Boolean(deploy?.jobs?.deploy?.environment?.name === "github-pages"), JSON.stringify(deploy?.jobs?.deploy?.environment));

const report = {
  tool: "check-workflows",
  status: errors.length ? "failed" : "healthy",
  workflows: Object.keys(EXPECT),
  checks: rows.length,
  failed: errors.length,
  errors,
  rows,
  finishedAt: new Date().toISOString(),
};
const out = parseArgs().report || "reports/workflows.json";
writeJson(out, report);

console.log("| 检查 | 结果 | 详情 |\n|---|---|---|");
for (const r of rows) console.log(`| ${r.name} | ${r.ok ? "✅" : "❌"} | ${r.detail.replace(/\|/g, "／").slice(0, 140)} |`);
console.log(`\n[check-workflows] ${report.status}：${rows.length} 项检查，失败 ${errors.length}`);
if (errors.length) {
  console.log(errors.map((e) => ` - ${e}`).join("\n"));
  process.exit(1);
}
process.exit(0);
