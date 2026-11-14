# qa/window-verify —— 窗口契约 v1 独立验证

查错（一组）独立写的验证，**不复用后端测试、不改业务代码**。口径唯一来源：`docs/window-rolling.md`。

## 怎么跑

```bash
python3 qa/window-verify/run_verify.py           # 全跑 A–I（49 条），写 report.md + report.json
python3 qa/window-verify/run_verify.py --only GHI  # 只跑某一组
python3 qa/window-verify/run_verify.py --strict  # BLOCKED 也算失败（上 CI 用）
```

结论写在 `qa/window-verify/report.md`（用例 × 期望 × 实际 × 是否符合契约）。

## 用例分组

| 组 | 对象 | 验什么 |
| --- | --- | --- |
| A | `scripts/_core.py` 的 `resolve_window` | 窗口取值、`asOf`/`asOfSource`、同日两时刻同窗、跨日移一天、fixed 回归 |
| B | `scripts/ci/lib/data.mjs` 的 `resolveWindow` | rolling 取值、是否带 asOf、fixed 回归、**B5 是否真复用 `window.mjs`** |
| C | `scripts/validate.py` CLI | 下界 00:00 通过 / -1s 判旧只 warning、上界 asOf 通过 / +1s 报 `TS_FUTURE`、date-only 当天合法次日非法、`--max-ts` 生效、`--as-of` 压过 `ERA_MAX_TS`、冻结快照回归 |
| D | `scripts/export_site.py` | **旧旁路护栏**：rolling 下 exit 2 并指路 `export_astro`，`--legacy-allow` 才放行 |
| E | `scripts/ci/lib/window.mjs` 的 `judge()` | ok/old/future/invalid 全部边界 |
| F | `scripts/ci/window.mjs` CLI | rolling/fixed 取值、`--github-env` 导出、优先级、时区换算 |
| G | `validate.py` 的簿记时间 | 簿记字段晚于 asOf 必须通过；只有 `occurredAt` 超上界才 `TS_FUTURE` |
| H | `collect.now` / `promote_candidates.now_iso` | 墙钟晚于 `win.as_of` 时钳到 asOf；asOf 更晚时不得上钳；fixed 同口径 |
| I | `export_astro.py` 的长度口径 | title 按 `_core.char_len`；summary 是否同口径（I4 = 不一致，见下） |

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

## 当前状态（2026-11-14 13:21，HEAD 72d161e）

全跑：**PASS 48 / FAIL 1 / BLOCKED 0**（共 49 条）。

- A/B/C/D/E/F 全 PASS：窗口取值、`judge()` 边界、CLI 优先级、冻结快照回归（C8 改为 `git archive ee7fd88` 只读快照上跑，368 条 0 error / 0 warning）、`export_site` 护栏（D1 exit 2 + 指路，D2 `--legacy-allow` 放行）。
- B5 PASS：`data.mjs` 已 `import { resolveWindow } from "./window.mjs"`，同一 `ERA_AS_OF` 下 from/end/asOf/upper/days 与 shared 完全一致（旧 B2 已消）。
- G1/G2 PASS：`collectedAt`/`firstReportedAt`/`scoreHistory[0].at`/`revisions[0].at`/`verification.checkedAt` 比 asOf 晚 3 分钟 → 0 findings；只把 `occurredAt` 挪到 asOf 之后 → `TS_FUTURE` **只命中 occurredAt**。
- H1–H4 PASS：rolling 下墙钟 13:2x > asOf 12:00 → `collect.now` 与 `promote.now_iso` 都返回 `2026-11-14T12:00:00+08:00`；asOf=2099 时不向上钳（返回真实墙钟）；fixed + `--max-ts=11-12` 同样钳到 11-12T00:00；fixed 无覆盖参时 `asOfSource=clock` 且不钳。
- I1–I3 PASS：title 裸 `len()`=126 / `char_len`=119 → export 接受；`char_len`=121 → 拒绝且报「121 字」；`char_len`=120 → 边界接受。

### 唯一 FAIL：`export_astro.py:510` 的 summary 仍是裸 `len()`（I4）

`title` 已改用 `C.char_len`（`export_astro.py:504`，86f8414），**summary 那一行没跟着改**（`export_astro.py:510`）：

```python
elif not (SUMMARY_MIN_CHARS <= len(summary) <= SUMMARY_MAX_CHARS):
```

而 `_core.py` 的 `Field.text("summary", min_len=10, max_len=120)`（`_core.py:1121`）走的是 `char_len()`（`_core.py:614`，去掉空白计字）。所以一条 **`char_len`=119、裸 `len()`=126** 的英文 summary（带 7 个空格）在 validate 里 0 error、到 export 才被拒：

```
validate:      exit=0  errors=0
export_astro:  ['event[ev_qa_summary_spaced]：summary 126 字，需在 10–120']
```

这不是纯理论：仓库里 `ev_6f90d24b977c` 的 title `char_len=104 / len=126`，换成旧口径就会被拒 —— 同一错位在 title 上已经有真实记录，只是先被修掉了。

**同类未改行**（同一行模式，未逐条构造用例）：`export_astro.py:576` sources.title、`:581` publisher、`:716`/`:873` revisions.note、`:959` statement。建议统一改成 `C.char_len(...)`；对应 `_core` 侧上限见 `_core.py:1201`（sources title 2–200）、`1202`（publisher 2–80）、`1005`（revisions note ≤400）、`1440`（forecast statement 10–200）。

当前真实数据扫描：555 条 events 里 summary 触发风险 0 条、title 触发风险 1 条（即上面那条，已修）——所以 I4 现在是**潜在**风险，不是线上故障。
