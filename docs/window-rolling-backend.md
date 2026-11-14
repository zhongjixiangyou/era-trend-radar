# rolling 窗口 · Python 侧实现与验证记录

口径唯一来源：`docs/window-rolling.md`；Node 侧对照实现：`scripts/ci/lib/window.mjs`。
本文只记 Python 侧改了什么、怎么验的、还有什么风险；未 commit / 未 push。

## 改了什么

| 文件 | 改动 |
| --- | --- |
| `scripts/_core.py` | `Window` 增 `as_of_from`（as-of/max-ts/arg/clock，报告里作 `asOfFrom`，与 `asOfSource` 并列）| 新增 `Window`、`resolve_window()`、`apply_window()`/`set_window()`/`active_window()`、`load_pipeline_config()`、`tz_for()`、`JUDGE_OK/OLD/FUTURE/INVALID`；`max_dt()` 改为「有窗口就用窗口上界」；`set_max_ts()` 保持兼容并清空窗口态；`validate_event()` 对早于下界的 `occurredAt` 记 `WINDOW_OUT` warning（不再判错）；`stats_dict()` 带 `window`（含 `asOfSource`） |
| `pipeline.config.json` | `window` 改为 `{mode:"rolling", days:180, timezone:"Asia/Shanghai", frozen:{start,end,frozenAt}, note}` |
| `scripts/validate.py` | 新增 `--as-of`；走 `apply_window()`；输出窗口行 |
| `scripts/cli.py` | 四个子命令加 `--as-of`，`validate/score/report` 统一先 `bootstrap_window()`（CI 入口 `cli.py validate content/` 靠它吃 `ERA_AS_OF`） |
| `scripts/collect.py` | 接窗口；`publishedAt` 归一（ISO + RSS 的 RFC 822）；越界条目丢弃并计入 `outOfWindow{old,future}`，不中断整批；报告带 `window` |
| `scripts/promote_candidates.py` | 接窗口；越界候选跳过并计 `skippedOutOfWindow`；输出带 `window` |
| `scripts/export_astro.py` | `meta.window`（rolling 无 `frozenAt`）；事件按窗口过滤（`content/` 原件保留）；`counts` 加 `contentTotal/publishedTotal/droppedOutOfWindow`；报告带 `window`；新增 `--as-of/--max-ts/--config` |
| `tests/test_window.py` | 新增 16 条标准库单测，覆盖契约 §6 ①②③④⑤⑥ |

未动：`.github/workflows/*`、`scripts/ci/*.mjs`、`scripts/from-export.mjs`（Coder 一组 负责）。

## 怎么验证的

```bash
python3 -m unittest discover -s tests            # 16 passed
python3 scripts/export_astro.py --selftest       # 12 项通过
python3 scripts/cli.py validate content/ --as-of 2026-11-14T12:00:00+08:00
python3 scripts/cli.py score content/ --as-of 2026-11-14T12:00:00+08:00
python3 scripts/export_astro.py --check --as-of 2026-11-14T12:00:00+08:00
python3 scripts/export_astro.py --check --config /tmp/era-fixed.json   # mode=fixed
```

实测结果（asOf = 2026-11-14T12:00:00+08:00）：

- 窗口 `2026-05-19 → 2026-11-14`，上界 `12:00:00+08:00`，下界 `00:00:00+08:00`。
- validate：368 记录，2 条 `WINDOW_OUT`（`ev_doe-smr-supply-20260514`、`ev_iea-oil-20260513`），0 error，PASS。
- export：content 349 → 窗口外 2 条（历史 2 / 超上界 0）→ published 68、trends 16；`meta.window = {from:"2026-05-19", to:"2026-11-14", mode:"rolling", asOf, days:180}`（无 `frozenAt`）；导出事件日期 2026-05-20 → 2026-11-12。
- fixed 模式（`window.mode=fixed` + `frozen`）：窗口 2026-05-13 → 2026-11-13、窗口外 0、published 70，与冻结前一致。
- collect 夹具（4 条：窗口内 ISO / 窗口内 RFC822 / 下界外 / 上界外）：保留 2、`outOfWindow{old:1,future:1}`，退出 0。
- promote 夹具（3 条候选）：`candidates:1, skippedOutOfWindow:2`，退出 0。

## 剩余风险

1. `scripts/export_site.py`（旧旁路）**未接窗口，改为护栏**：它自带老 schema（`events[].date`、`credibility` high/medium/low、`trends[].effect`），窗口逻辑是 *只增不减*（把事件日期 min/max 并进窗口），rolling 下会一直保留历史窗口。现在 `main()` 会读 `pipeline.config.json`，`mode=rolling` 时打印原因并 **exit 2**，提示改用 `export_astro.py`；确需旧旁路加 `--legacy-allow`。它不在 CI 链路，主链路是 `export_astro.py → from-export.mjs`。
2. 非 `Asia/Shanghai` 的 `timezone` 依赖 `zoneinfo`（macOS / GitHub runner 都有）；默认时区走固定 `+08:00`，不依赖 tzdata。
3. `fmt_ts()` 输出到秒；`fixed` 上界内部保留毫秒（`23:59:59.999+08:00`），比较用完整精度，只有打印时截断。
4. `scripts/cli.py` 超出原分工清单（CI 的 validate 入口就是它），若 Coder 一组 也动了同一文件需合并。
5. 窗口外事件仍留在 `content/`：`validate` 只给 warning，`score`/`report` 仍会读到它们（属预期，契约要求历史保留）。
