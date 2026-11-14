# qa/window-verify —— 窗口契约 v1 独立验证

查错（一组）独立写的验证，**不复用后端测试、不改业务代码**。口径唯一来源：`docs/window-rolling.md`。

## 怎么跑

```bash
python3 qa/window-verify/run_verify.py           # 全跑 A/B/C/D，写 report.md + report.json
python3 qa/window-verify/run_verify.py --only C  # 只跑某一组
python3 qa/window-verify/run_verify.py --strict  # BLOCKED 也算失败（上 CI 用）
```

结论写在 `qa/window-verify/report.md`（用例 × 期望 × 实际 × 是否符合契约）。

## 四组用例

| 组 | 对象 | 验什么 |
| --- | --- | --- |
| A | `scripts/_core.py` 的 `resolve_window` | 窗口取值、`asOf`/`asOfSource`、同日两时刻同窗、跨日移一天、fixed 回归 |
| B | `scripts/ci/lib/data.mjs` 的 `resolveWindow` | rolling 取值、是否带 asOf、fixed 回归 |
| C | `scripts/validate.py` CLI | 下界 00:00 通过 / -1s 判旧只 warning、上界 asOf 通过 / +1s 报 `TS_FUTURE`、date-only 当天合法次日非法、`--max-ts` 生效、`--as-of` 压过 `ERA_MAX_TS`、真实 `content/` 全量 fixed 回归 |
| D | `scripts/export_site.py` | `meta.window` 带 `{from,to,mode,asOf,days}`、rolling 下不写 `frozenAt`、展示数组过滤越界事件 |

测法的独立性：契约窗口由 `run_verify.py` 里 `contract_window()` 自己算（不 import 业务代码），用例期望值全部硬编码在用例里。

## 跨日 fixture

`fixtures/cross-day/`（三条，克隆真实事件结构、只改时间与标识，保证 schema 本身合法），在 `asOf=2026-11-14T12:00:00+08:00`、rolling 180 天（from=2026-05-19）下：

| 文件 | occurredAt | 契约判定 | 理由 |
| --- | --- | --- | --- |
| `ev_qa_edge_0030.json` | 2026-11-14T00:30:00+08:00 | 合法 | 落在 05-19 00:00 ~ 11-14 12:00 内，且是「当天 00:00 之后、asOf 之前」的跨日样本 |
| `ev_qa_old_0518.json` | 2026-05-18T12:00:00+08:00 | **warning**（`WINDOW_OUT`），不 error | date 05-18 < from 05-19；旧记录保留、不参与展示 |
| `ev_qa_future_1115.json` | 2026-11-15T09:00:00+08:00 | **error**（`TS_FUTURE`） | ts > asOf |

跑法：

```bash
python3 scripts/validate.py qa/window-verify/fixtures/cross-day --json --history off --as-of 2026-11-14T12:00:00+08:00
```

`_tmp/` 是运行时生成的临时夹具和导出产物，不用入库。

## 已发现的契约歧义（需要拍板）

1. **「同日两个时刻下界差一天」自相矛盾。** `docs/window-rolling.md` §6 与 Coder 的口述都是「`asOf=2026-11-14T00:00:01` 与 `23:59:59` …… 下界差一天」，但按 §2 的定义（`to = asOf 换算到 Asia/Shanghai 的日历日`，`from = to - 179 天`），这两个时刻属于同一个日历日，`to`/`from` **必须完全相同**，不可能差一天。
   - 本套用例按契约定义验：A4 断言两者同窗（11-14 / 05-19）。
   - 真正会差一天的是**跨日**：`2026-11-13T23:59:59`（to=11-13, from=05-18）vs `2026-11-14T00:00:01`（to=11-14, from=05-19），由 A5 覆盖。
   - 如果本意是「按 24 小时滑动窗口」而不是按自然日，那 §2 的定义要改，请明确后我改用例。
2. **fixed 模式下 `--as-of` 是否也当上界用。** §3 说滚动模式下 as-of 与 max-ts 是同一个量；fixed 模式下「max-ts 覆盖上界，窗口日期仍取配置」。那么 fixed + `--as-of=2026-11-14T12:00` 时上界是 11-14T12:00 还是回到配置里的 11-13？本套先按「as-of = 上界覆盖」理解（C6 只验 rolling），fixed 回归（C7/A6/B4）只验「不传覆盖参时与 2026-11-13 冻结一致」。
3. **旧记录 warning 的判定基准。** 契约写 `date < from` 才 warning。那么 `2026-05-18T23:59:59+08`（date=05-18 < from）算旧——但它的时刻其实早于 from 00:00，两条判据一致；反例是 `2026-05-19T00:00:00` 必须通过（C2 已覆盖）。带时刻且 date < from 但 ts > from 00:00 的情况不存在，无歧义。

## 当前状态（2026-11-14 12:46，Python 侧已落地）

全跑：**PASS 35 / FAIL 1 / BLOCKED 2**。

- A1–A6 PASS：`_core.py` 的 `resolve_window()` 返回 dataclass `Window`，rolling 取值、asOf/asOfSource、同日同窗、跨日平移、fixed 冻结全部符合契约。
- C0–C8 PASS：`validate.py --as-of` 生效、`ERA_AS_OF` 生效、下界 from-1s 只出 `WINDOW_OUT`(warn)、上界 asOf+1s 出 `TS_FUTURE`(error)、date-only 当天合法/次日非法、`--max-ts` 生效、`--as-of` 压过 `ERA_MAX_TS`。
- C7（真实 content/，rolling 口径）：368 记录 **0 error / 2 warning**，两条 warning 都是 05-13、05-14 的旧事件 `WINDOW_OUT`，exit 0 —— 正是「旧记录只 warning」；`stats.window` 带 from/to/mode/asOf/days/asOfSource。
- C8（fixed 冻结口径，进程内 mock config，不动仓库配置）：368 记录 **0 error / 0 warning**，`window.mode=fixed from=2026-05-13 to=2026-11-13 frozenAt=2026-11-13` —— 与冻结时完全一致。
- E1–E8 PASS：`lib/window.mjs` 的 `judge()` 全部边界（含「asOf 当天 23:00 带时刻」必须 future，说明没用整日上限）。
- F1–F9 PASS：`window.mjs` CLI 的 rolling 取值、upper=asOf、fixed 回归、fixed+max-ts、as-of 压过 ERA_MAX_TS、跨日平移、同日同窗、UTC 写法换算。

### 唯一 FAIL：`scripts/ci/lib/data.mjs` 仍在用自己那份 `resolveWindow`（B2）

`data.mjs:34` 的 `resolveWindow(config, now = new Date())` 只返回 `{mode,start,end,days}`，全文件**没有一处** `ERA_AS_OF` / `ERA_WINDOW_*`（grep 为空），而 `validate.mjs:190` 调 `validateData(doc, cfg, { baseline })` 也不传 `now`。三个具体后果：

1. CI 钉好的 as-of 对站点数据校验无效——它永远用 `new Date()`，跨午夜那一轮仍会出现两个「今天」。
2. 没有时刻上界：`resolveWindow` 只有「日」，12:00 跑的时候 18:00 的数据会被判合法（契约要求 error）。
3. `data.mjs:218` 把 `day < win.start || day > win.end` 一律 push 成 `EVENT_DATE_OUT_OF_RANGE` **error**，没有「旧记录只 warning」。

建议：`data.mjs` 删掉自己的 `resolveWindow`，改 `import { currentWindow, judge } from "./window.mjs"`，`validateData` 用 `judge()` 的 ok/old/future 决定 error 还是 warning。

### 其余 BLOCKED：`export_site.py` 还没接窗口（D1/D2）

`export_site.py` 不认 `--as-of`，`meta.window` 仍由事件日期 min/max 反推（`export_site.py:1163-1169`），没有 mode/asOf/days，rolling 下会带 frozenAt，越界事件也进不了「不展示」的逻辑。
