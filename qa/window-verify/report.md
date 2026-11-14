# 窗口契约 v1 独立验证报告

- 生成时间：2026-11-14T13:21:40+08:00
- 口径：Asia/Shanghai，asOf=2026-11-14T12:00:00+08:00，days=180
- 契约正向窗口（独立推导）：from=2026-05-19 to=2026-11-14
- 结果：PASS 48 / FAIL 1 / BLOCKED 0

| 编号 | 组 | 用例 | 契约期望 | 实际结果 | 结论 |
| --- | --- | --- | --- | --- | --- |
| I4 | I | summary 长度口径：validate 按字(char_len=119)放行 / export_astro 仍按裸 len()=126 拒绝 | 两侧口径一致（要么都放行、要么都拒） | validate: exit=0 errors=0；export_astro: ['event[ev_qa_summary_spaced]：summary 126 字，需在 10–120'] | FAIL |
| A1 | A | resolve_window 滚动窗口取值 | from=2026-05-19 to=2026-11-14 days=180 mode=rolling | from=2026-05-19 to=2026-11-14 days=180 mode=rolling | PASS |
| A2 | A | 返回体带 asOf | asOf=2026-11-14T12:00:00+08:00 | 2026-11-14T12:00:00+08:00 | PASS |
| A3 | A | 返回体带 asOfSource | cli | cli | PASS |
| A4 | A | 同日 00:00:01 与 23:59:59 得到同一窗口 | to=2026-11-14 from=2026-05-19（两者相同） | 00:00:01 → to=2026-11-14 from=2026-05-19；23:59:59 → to=2026-11-14 from=2026-05-19 | PASS |
| A5 | A | 跨日：13T23:59:59 vs 14T00:00:01 下界差一天 | to=2026-11-13 from=2026-05-18 | to=2026-11-13 from=2026-05-18 | PASS |
| A6 | A | fixed 模式回归 | from=2026-05-13 to=2026-11-13 mode=fixed 且保留 frozenAt | from=2026-05-13 to=2026-11-13 mode=fixed frozenAt=2026-11-13 | PASS |
| B1 | B | data.mjs resolveWindow rolling 取值 | start=2026-05-19 end=2026-11-14 | start=2026-05-19 end=2026-11-14 | PASS |
| B2 | B | data.mjs resolveWindow 返回 asOf/asOfSource | 带 asOf 与 asOfSource | keys=['asOf', 'asOfFrom', 'asOfSource', 'days', 'end', 'mode', 'start', 'upper'] | PASS |
| B3 | B | 同日 00:00:01 与 23:59:59 同一窗口 | 两者 start/end 相同 | early=2026-05-19~2026-11-14 late=2026-05-19~2026-11-14 | PASS |
| B4 | B | data.mjs fixed 回归 | start=2026-05-13 end=2026-11-13 | start=2026-05-13 end=2026-11-13 | PASS |
| B5 | B | data.mjs 复用 window.mjs：同一 asOf 下 from/end/asOf/upper/days 全一致 | start=2026-05-19 end=2026-11-14 asOf=2026-11-14T12:00:00+08:00 days=180 | shared(from=2026-05-19,to=2026-11-14,asOf=2026-11-14T04:00:00.000Z) data(start=2026-05-19,end=2026-11-14,asOf=2026-11-14T04:00:00.000Z,upper=2026-11-14T04:00:00.000Z) | PASS |
| C0 | C | 夹具自检：11-14T00:30 事件在 --max-ts=11-13 下应报 TS_FUTURE | 存在 TS_FUTURE（说明夹具时间字段被真正校验） | [error/TS_FUTURE] qa/window-verify/_tmp/c0_clean/ev_qa_edge_0030.json#occurredAt | PASS |
| C1 | C | 跨日 fixture 三条：edge 合法 / old warning 不 error / future error | exit1；old 仅 warning；future 报 TS_FUTURE；edge 干净 | exit=1 [error/TS_FUTURE] qa/window-verify/_tmp/cross_day/ev_qa_future_1115.json#occurredAt；[warn/WINDOW_OUT] qa/window-verify/_tmp/cross_day/ev_qa_old_0518.json#occurredAt | PASS |
| C2 | C | 下界：from 00:00 通过 / from-1s 判旧 | 05-19T00:00 无 finding；05-18T23:59:59 仅 WINDOW_OUT(warn)，exit0 | exit=0 [warn/WINDOW_OUT] qa/window-verify/_tmp/lower/ev_qa_lower_minus1s.json#occurredAt | PASS |
| C3 | C | 上界：asOf 通过 / +1s error | exit1；12:00:00 干净，12:00:01 报 TS_FUTURE | exit=1 [error/TS_FUTURE] qa/window-verify/_tmp/upper/ev_qa_upper_plus1s.json#occurredAt | PASS |
| C4 | C | date-only：当天合法 / 次日非法 | 11-14 无 error；11-15 有 error（TS_FUTURE） | exit=1 [error/TS_FUTURE] qa/window-verify/_tmp/dateonly/ev_qa_dateonly_next.json#occurredAt | PASS |
| C5 | C | --max-ts 生效（11-12 上限压掉 11-14 事件） | exit1；TS_FUTURE 命中 | exit=1 [error/TS_FUTURE] qa/window-verify/_tmp/maxts/ev_qa_upper_exact.json#occurredAt | PASS |
| C6 | C | --as-of 压过 ERA_MAX_TS（11-14T00:30 事件不报 TS_FUTURE） | asOfSource=cli；edge 无 TS_FUTURE；window.to=2026-11-14 | asOfSource=cli to=2026-11-14 upper=2026-11-14T12:00:00+08:00 ts_future=0 无 findings | PASS |
| C7 | C | rolling 真实 content/ 全量：旧记录只 warning、不 error | exit0；errors=0；warning 全部是 WINDOW_OUT；window{from,to,mode,asOf,days,asOfSource} 齐全 | exit=0 errors=0 warnings=2 old=2 errfind=0 window={"from": "2026-05-19", "to": "2026-11-14", "mode": "rolling", "asOf": "2026-11-14T13:21:39+08:00", "asOfFrom": "clock", "days": 180, "asOfSource": "clock"} | PASS |
| C8 | C | fixed 回归：冻结快照 ee7fd88 上重跑 | errors=0 warnings=0 records=368；快照配置 window.mode=fixed、冻结区间 05-13~11-13 | errors=0 warnings=0 records=368 cfg.mode=fixed (旧版 validate 不输出 window 字段) | PASS |
| D1 | D | rolling 配置下 export_site 拒绝运行（exit 2 并指路 export_astro） | exit=2；提示「拒绝运行」且提到 --legacy-allow | exit=2 [export_site] 拒绝运行：pipeline.config.json 是 rolling 窗口，本脚本（旧旁路）不会按 asOf 过滤、也不会写 mode/asOf，会产出失真的 meta.window。 | PASS |
| D2 | D | --legacy-allow 放行旧旁路（不再被 rolling 护栏拦） | exit!=2；无「拒绝运行」；输出进入对照表/自检阶段 | exit=1 对照表=有 护栏=放行 | PASS |
| E1 | E | judge(): from 当天 00:00:00+08 → 通过 | ok | ok | PASS |
| E2 | E | judge(): from-1 秒（05-18T23:59:59）→ 旧记录（warning 口径） | old | old | PASS |
| E3 | E | judge(): asOf 时刻本身 → 通过 | ok | ok | PASS |
| E4 | E | judge(): asOf+1 秒 → error(TS_FUTURE) | future | future | PASS |
| E5 | E | judge(): date-only == to（2026-11-14）→ 合法 | ok | ok | PASS |
| E6 | E | judge(): date-only == to+1（2026-11-15）→ 非法 | future | future | PASS |
| E7 | E | judge(): date-only < from（2026-05-18）→ 旧记录 | old | old | PASS |
| E8 | E | judge(): asOf 当天 23:00 带时刻 → future（不得用整日上限） | future | future | PASS |
| F1 | F | window.mjs rolling：from/to/days/asOfSource | mode=rolling from=2026-05-19 to=2026-11-14 days=180 asOfSource=cli | {"mode": "rolling", "from": "2026-05-19", "to": "2026-11-14", "days": 180, "timezone": "Asia/Shanghai", "asOf": "2026-11-14T04:00:00.000Z", "asOfSource": "cli", "asOfFrom": "as-of", "upper": "2026-11-14T04:00:00.000Z", "fromTs": "2026-05-18T16:00:00.000Z"} | PASS |
| F2 | F | window.mjs rolling：upper = asOf 时刻本身 | upper=2026-11-14T12:00:00+08:00 | 2026-11-14T04:00:00.000Z | PASS |
| F3 | F | window.mjs fixed 回归（与 2026-11-13 冻结一致） | from=2026-05-13 to=2026-11-13 frozenAt=2026-11-13 | {"mode": "fixed", "from": "2026-05-13", "to": "2026-11-13", "days": null, "timezone": "Asia/Shanghai", "asOf": "2026-11-14T05:21:40.033Z", "asOfSource": "clock", "asOfFrom": "clock", "upper": "2026-11-13T15:59:59.999Z", "fromTs": "2026-05-12T16:00:00.000Z", "frozenAt": "2026-11-13"} | PASS |
| F4 | F | fixed + --max-ts：上界被压过、窗口日期不变 | from/to 仍 05-13/11-13；upper=2026-11-12T00:00+08 | {"mode": "fixed", "from": "2026-05-13", "to": "2026-11-13", "days": null, "timezone": "Asia/Shanghai", "asOf": "2026-11-11T16:00:00.000Z", "asOfSource": "cli", "asOfFrom": "max-ts", "upper": "2026-11-11T16:00:00.000Z", "fromTs": "2026-05-12T16:00:00.000Z", "frozenAt": "2026-11-13"} | PASS |
| F5 | F | --as-of 压过 ERA_MAX_TS | asOfSource=cli，upper=2026-11-14T12:00+08（不是 11-12） | {"mode": "rolling", "from": "2026-05-19", "to": "2026-11-14", "days": 180, "timezone": "Asia/Shanghai", "asOf": "2026-11-14T04:00:00.000Z", "asOfSource": "cli", "asOfFrom": "as-of", "upper": "2026-11-14T04:00:00.000Z", "fromTs": "2026-05-18T16:00:00.000Z"} | PASS |
| F6 | F | 跨日：13T23:59:59 → 05-18/11-13；14T00:00:01 → 05-19/11-14 | 下界随日历日平移一天 | 13日: 2026-05-18~2026-11-13；14日: 2026-05-19~2026-11-14 | PASS |
| F7 | F | 同日 00:00:01 与 23:59:59 同窗（差一天只发生在跨日） | 两者 from=2026-05-19 to=2026-11-14 | 2026-05-19~2026-11-14 / 2026-05-19~2026-11-14 | PASS |
| F8 | F | asOf 用 UTC 写法（13T17:00Z）→ 按 +08 落到 11-14 | to=2026-11-14 from=2026-05-19 | to=2026-11-14 from=2026-05-19 | PASS |
| F9 | F | asOf=13T15:59:59Z → 仍属 11-13（前一日） | to=2026-11-13 from=2026-05-18 | to=2026-11-13 from=2026-05-18 | PASS |
| G1 | G | 簿记时间（collectedAt/firstReportedAt/scoreHistory.at/revisions.at/checkedAt）晚于 asOf 3 分钟 → 必须通过 | exit0；无 findings | exit=0 无 findings | PASS |
| G2 | G | 同一条记录只把 occurredAt 改到 asOf 之后 → 必须报 TS_FUTURE | exit1；TS_FUTURE 只命中 occurredAt（簿记字段不再连坐） | exit=1 ts_future=1 fields=['occurredAt'] [error/TS_FUTURE] qa/window-verify/_tmp/bookkeeping_bad/ev_qa_bk_bad.json#occurredAt | PASS |
| H1 | H | rolling：墙钟(13:2x) 晚于 asOf(12:00) → collect.now / promote.now_iso 钳到 asOf | 两者都等于 2026-11-14T12:00:00+08:00 | collect=2026-11-14T12:00:00+08:00 promote=2026-11-14T12:00:00+08:00 wall=2026-11-14T13:21:40+08:00 | PASS |
| H2 | H | rolling：asOf(2099) 晚于墙钟 → 不得向上钳（返回真实墙钟） | collect.now ≈ 墙钟，不是 2099 | collect=2026-11-14T13:21:40.523943 wall=2026-11-14T13:21:40+08:00 as_of=2099-01-01 | PASS |
| H3 | H | fixed + --max-ts(11-12)：同样钳到窗口 asOf | 两者都等于 2026-11-12T00:00:00+08:00 | as_of=2026-11-12T00:00:00+08:00 collect=2026-11-12T00:00:00+08:00 promote=2026-11-12T00:00:00+08:00 | PASS |
| H4 | H | fixed 无 max-ts：asOf=时钟 → 不钳，仍返回墙钟 | collect.now ≈ 墙钟，asOfSource=clock | as_of=2026-11-14T13:21:40.523965 source=clock collect=2026-11-14T13:21:40.523965 | PASS |
| I1 | I | 英文带空格标题：裸 len()=126 但 char_len=119 → export 接受 | len=126 char_len=119，无 title 报错 | len=126 char_len=119 title_errors=[] | PASS |
| I2 | I | char_len=121（超 120）→ 仍必须拒绝 | title 报错且提到 121 字 | len=121 char_len=121 title_errors=['event[ev_qa_len_001]：title 121 字，需在 4–120'] | PASS |
| I3 | I | char_len=120（正好上限）→ 边界接受 | 无 title 报错 | len=120 char_len=120 title_errors=[] | PASS |
