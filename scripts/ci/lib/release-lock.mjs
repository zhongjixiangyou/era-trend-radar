// scripts/ci/lib/release-lock.mjs — src/data 写入的独占锁。
//
// 为什么需要：src/data 是构建期可变的投影目录，release（公开投影）和
// from-export（预览投影）都会原地重写它。两个写者并行时，后者会把
// 「已被投影的 src/data」当成原始备份，两边都打印「已还原」，仓库却停在投影上。
//
// 锁内容必须是 JSON 而不是裸 pid：如果进程被 SIGKILL 硬杀，信号处理器不跑，
// src/data 会永久停在投影。备份目录在 os.tmpdir() 下、SIGKILL 不会清掉，
// 所以把 backup 路径写进锁里，下一轮发现有死锁时可以先把数据救回来再继续。
import { writeFileSync, readFileSync, rmSync, cpSync, existsSync } from "node:fs";
import path from "node:path";

export const LOCK = path.join("src", ".release.lock");
const DATA = path.resolve(process.cwd(), "src", "data");

let held = false;

function pidAlive(pid) {
  if (!Number.isInteger(pid) || pid <= 0) return false;
  try {
    process.kill(pid, 0);
    return true;
  } catch (e) {
    return e.code === "EPERM";
  }
}

/** 读锁文件；兼容早期的裸 pid 格式。读不了返回 null。 */
function readLock() {
  try {
    const raw = readFileSync(LOCK, "utf8").trim();
    if (raw.startsWith("{")) return JSON.parse(raw);
    return { pid: Number(raw) };
  } catch (_) {
    return null;
  }
}

/**
 * 从备份目录还原 src/data：先拷到同级暂存目录再换入。
 * 先 rm 再 cp 中途挂掉会留下一个空 src/data（踩过）。
 */
export function restoreDataFrom(backupDir) {
  if (!backupDir || !existsSync(path.join(backupDir, "events.json"))) return false;
  const staging = path.join("src", ".data.restore");
  try {
    rmSync(staging, { recursive: true, force: true });
    cpSync(backupDir, staging, { recursive: true });
    rmSync(DATA, { recursive: true, force: true });
    cpSync(staging, DATA, { recursive: true });
    rmSync(staging, { recursive: true, force: true });
    return true;
  } catch (e) {
    console.error(`⚠ 从备份还原 src/data 失败（${e.message}），请手动 git checkout -- src/data`);
    return false;
  }
}

/**
 * 抢锁。backup 是本轮备份目录（可为 null），会写进锁文件供下一轮硬杀后救援。
 * 拿不到锁返回 false；调用方应当 exit(2)，且不要写 src/data。
 */
export function acquire({ backup = null, label = "release" } = {}) {
  for (let attempt = 0; attempt < 2; attempt += 1) {
    try {
      writeFileSync(
        LOCK,
        JSON.stringify({ pid: process.pid, label, backup, at: new Date().toISOString() }),
        { flag: "wx" }
      );
      held = true;
      return true;
    } catch (e) {
      if (e.code !== "EEXIST") {
        console.error(`✗ 无法创建 ${LOCK}（${e.message}），拒绝并发写 src/data`);
        return false;
      }
      const info = readLock() ?? {};
      if (pidAlive(info.pid)) {
        console.error(
          `✗ 另一个进程正在写 src/data（${info.label || "未知来源"} pid ${info.pid}，锁 ${LOCK}），拒绝并发`
        );
        return false;
      }
      // 僵尸锁：pid 已不在（SIGKILL / 崩溃）。备份还在就先救数据，再清锁重试。
      if (info.backup && restoreDataFrom(info.backup)) {
        console.error(`↩ 上次进程（${info.label || "未知来源"} pid ${info.pid}）被硬杀，已按备份还原 src/data`);
      }
      try {
        rmSync(LOCK, { force: true });
      } catch (_) {
        /* 清不掉就让下一轮重试 */
      }
    }
  }
  console.error(`✗ 拿不到 ${LOCK}，拒绝并发写 src/data`);
  return false;
}

/** 释放锁。挂到 exit / 信号上，重复调用无害。 */
export function releaseLock() {
  if (!held) return;
  held = false;
  try {
    rmSync(LOCK, { force: true });
  } catch (_) {
    /* 忽略：锁文件不在或权限问题都不该让进程失败 */
  }
}

/** out 目录是不是 src/data（只有它需要锁，导出到 .era-tmp 的不用）。 */
export function isDataDir(target) {
  return path.resolve(target) === DATA;
}

/** 调用方已经持锁（release 内部再调 from-export）时用，避免自锁。 */
export function lockHeldByParent() {
  return process.env.ERA_LOCK_HELD === "1";
}
