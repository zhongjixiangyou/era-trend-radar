# 窗口契约 v1：rolling（近 180 个自然日）+ frozen（历史重现）

本文件是 collect / promote / validate / export / build 的唯一口径来源。任何阶段不得自己再算一套窗口。

## 1. 一个 as-of，跑完一整轮

一次流水线（一次 workflow_dispatch 或一次本地脚本串）只解析**一次**窗口，得到的 `asOf` 之后所有阶段复用：

- CI：第一步跑 `node scripts/ci/window.mjs --github-env`，把下面这组写进 `$GITHUB_ENV`，后续步骤全部继承：

  | 变量 | 含义 | 能不能拿来判定 |
  |---|---|---|
  | `ERA_AS_OF` | 本轮基准时刻（带时区） | **能**，判定都以它为准 |
  | `ERA_WINDOW_UPPER` | 上界时刻（rolling 下 = asOf；fixed 下 = to 23:59:59.999+08） | **能** |
  | `ERA_WINDOW_FROM` / `ERA_WINDOW_TO` | `YYYY-MM-DD` 日历日，**只用于展示/文案** | 不能 |
  | `ERA_WINDOW_MODE` / `ERA_WINDOW_DAYS` / `ERA_WINDOW_AS_OF_FROM` | 模式、天数、asOf 来自哪个参数 | 只读元数据 |

  `ERA_WINDOW_TO` 是**日期**不是时刻：某阶段拿它当上界比时刻，就会把 12:00 跑的 18:00 数据放过去。判定必须用 `ERA_AS_OF` 或 `ERA_WINDOW_UPPER`。
- 本地：`export ERA_AS_OF=2026-11-14T12:00:00+08:00`（不设 = 取当前时刻）。
- 禁止各阶段各自 `now()`：跨午夜跑一轮会得到两个不同的「今天」，是本次要修的 bug。

## 2. 模式

`pipeline.config.json` 的 `window`：

```json
{
  "mode": "rolling",
  "days": 180,
  "timezone": "Asia/Shanghai",
  "frozen": { "start": "2026-05-13", "end": "2026-11-13", "frozenAt": "2026-11-13" }
}
```

- `mode=rolling`（默认）：近 `days` 个自然日，**含当天**。
- `mode=fixed`（历史重现 / 旧契约）：用 `frozen.start ~ frozen.end`（老配置只有 `start/end` 时读 `start/end`），完全保持 rollout 前的行为。

### rolling 的边界定义

以 `asOf` 换算到 `Asia/Shanghai` 的日历日 `D`：

- `to = D`
- `from = D - (days - 1)` 天
- 下界：`from 00:00:00+08:00`
- 上界：`asOf` 这个**时刻本身**（不是当天 23:59:59）

所以 2026-11-14 12:00 的 180 天窗口是 `2026-05-19 00:00:00+08:00 ~ 2026-11-14 12:00:00+08:00`，站点展示的日期区间是 `2026-05-19 ~ 2026-11-14`。

### 未来时间

- 带时刻的记录：`ts > asOf` → **error**（`TS_FUTURE`）。（不允许用「整日上限」把 12:00 跑的 18:00 数据放过去。）
- 只有日期的记录：`date > to` → error；`date == to` 合法（当天的事当天可写）。
- 旧记录（`date < from`）：**warning，不报错**（`WINDOW_OUT`）。历史数据保留在 `content/`，只是不参与展示。

## 3. 覆盖优先级（显式覆盖，可复现）

从高到低：

1. `--as-of <ISO8601>` / `ERA_AS_OF`
2. `--max-ts <ISO8601>` / `ERA_MAX_TS`（老参数，等价于「as-of = 这个时刻」）
3. 配置里的 `window`

滚动模式下 `as-of` 与 `max-ts` 是同一个量（上界即 as-of）；fixed 模式下 `max-ts` 覆盖上界，窗口日期仍取配置。冲突时以 1 为准。

报告里同时写两个字段，避免「用了 `--max-ts` 却报成 `--as-of`」这种误导：

| 字段 | 取值 | 含义 |
|---|---|---|
| `asOfSource` | `cli` / `env` / `arg` / `clock` | 这个基准是从命令行、环境变量、调用参数还是系统时钟来的 |
| `asOfFrom` | `as-of` / `max-ts` / `clock` | 具体用了哪个参数：`--as-of`/`ERA_AS_OF` 记 `as-of`，`--max-ts`/`ERA_MAX_TS` 记 `max-ts` |

例：`--max-ts 2026-11-12T00:00:00+08:00` → `asOfSource=cli, asOfFrom=max-ts`。

## 4. 各阶段要求

| 阶段 | 必须 |
|---|---|
| `collect.py` | 读到窗口后，抓回的事件按 §2 判定；越界丢弃并计入 report，**不写进候选** |
| `promote_candidates.py` | 提升时按同一窗口判定；越界候选跳过并计数（`skippedOutOfWindow`），不因越界让整批失败 |
| `validate.py` / `validate.mjs` | 下界越界=warning，上界越界=error；不得因为库里有旧记录就整库判负 |
| `export_astro.py` / `from-export.mjs` | 展示数组按窗口过滤；`meta.window = { from, to, mode, asOf, days }`；rolling 下**不写 frozenAt** |
| `export_site.py`（旧旁路，不在 CI 链路） | rolling 下**直接拒绝运行**（exit 2，提示改用 `export_astro.py`），防止陈旧窗口被误当真实 meta；确需跑旧旁路加 `--legacy-allow` |
| `scripts/ci/lib/data.mjs` | 只用 `resolveWindow(config, asOf)`，不再读 `w.frozenAt` 当兜底 |
| 构建/派生 | 与导出同一窗口；`src/data` 与 `products` 一致 |

## 5. 失败与诊断

- 校验失败仍然阻断提交与发布（exit 1），不要为了让 pipeline 变绿跳过 validate 或强推。
- 每阶段报告必须带 `window{from,to,mode,asOf,asOfSource,days}`，便于核对「这轮到底按哪个窗口判的」。

## 6. 测试（必须真跑）

- 下界：`from` 当天 00:00:00 通过；`from - 1 秒` 不通过（旧记录 warning）。
- 上界：`asOf` 时刻通过；`asOf + 1 秒` error。
- 跨日：`asOf=2026-11-14T00:00:01+08:00` 与 `23:59:59+08:00` 得到同一个 `to`，且下界随 `to` 平移一天。
- 跨午夜一轮不拆窗：同一轮里 collect 与 validate 用同一个 `ERA_AS_OF`。
- fixed 模式：结果与 2026-11-13 冻结时完全一致。
- 显式覆盖：`--max-ts` 压过配置；`--as-of` 压过 `ERA_MAX_TS`。
