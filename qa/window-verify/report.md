# 窗口契约 v1 独立验证报告

- 生成时间：2026-11-14T12:51:54+08:00
- 口径：Asia/Shanghai，asOf=2026-11-14T12:00:00+08:00，days=180
- 契约正向窗口（独立推导）：from=2026-05-19 to=2026-11-14
- 结果：PASS 9 / FAIL 0 / BLOCKED 0

| 编号 | 组 | 用例 | 契约期望 | 实际结果 | 结论 |
| --- | --- | --- | --- | --- | --- |
| F1 | F | window.mjs rolling：from/to/days/asOfSource | mode=rolling from=2026-05-19 to=2026-11-14 days=180 asOfSource=cli | {"mode": "rolling", "from": "2026-05-19", "to": "2026-11-14", "days": 180, "timezone": "Asia/Shanghai", "asOf": "2026-11-14T04:00:00.000Z", "asOfSource": "cli", "asOfFrom": "as-of", "upper": "2026-11-14T04:00:00.000Z", "fromTs": "2026-05-18T16:00:00.000Z"} | PASS |
| F2 | F | window.mjs rolling：upper = asOf 时刻本身 | upper=2026-11-14T12:00:00+08:00 | 2026-11-14T04:00:00.000Z | PASS |
| F3 | F | window.mjs fixed 回归（与 2026-11-13 冻结一致） | from=2026-05-13 to=2026-11-13 frozenAt=2026-11-13 | {"mode": "fixed", "from": "2026-05-13", "to": "2026-11-13", "days": null, "timezone": "Asia/Shanghai", "asOf": "2026-11-14T04:51:54.230Z", "asOfSource": "clock", "asOfFrom": "clock", "upper": "2026-11-13T15:59:59.999Z", "fromTs": "2026-05-12T16:00:00.000Z", "frozenAt": "2026-11-13"} | PASS |
| F4 | F | fixed + --max-ts：上界被压过、窗口日期不变 | from/to 仍 05-13/11-13；upper=2026-11-12T00:00+08 | {"mode": "fixed", "from": "2026-05-13", "to": "2026-11-13", "days": null, "timezone": "Asia/Shanghai", "asOf": "2026-11-11T16:00:00.000Z", "asOfSource": "cli", "asOfFrom": "max-ts", "upper": "2026-11-11T16:00:00.000Z", "fromTs": "2026-05-12T16:00:00.000Z", "frozenAt": "2026-11-13"} | PASS |
| F5 | F | --as-of 压过 ERA_MAX_TS | asOfSource=cli，upper=2026-11-14T12:00+08（不是 11-12） | {"mode": "rolling", "from": "2026-05-19", "to": "2026-11-14", "days": 180, "timezone": "Asia/Shanghai", "asOf": "2026-11-14T04:00:00.000Z", "asOfSource": "cli", "asOfFrom": "as-of", "upper": "2026-11-14T04:00:00.000Z", "fromTs": "2026-05-18T16:00:00.000Z"} | PASS |
| F6 | F | 跨日：13T23:59:59 → 05-18/11-13；14T00:00:01 → 05-19/11-14 | 下界随日历日平移一天 | 13日: 2026-05-18~2026-11-13；14日: 2026-05-19~2026-11-14 | PASS |
| F7 | F | 同日 00:00:01 与 23:59:59 同窗（差一天只发生在跨日） | 两者 from=2026-05-19 to=2026-11-14 | 2026-05-19~2026-11-14 / 2026-05-19~2026-11-14 | PASS |
| F8 | F | asOf 用 UTC 写法（13T17:00Z）→ 按 +08 落到 11-14 | to=2026-11-14 from=2026-05-19 | to=2026-11-14 from=2026-05-19 | PASS |
| F9 | F | asOf=13T15:59:59Z → 仍属 11-13（前一日） | to=2026-11-13 from=2026-05-18 | to=2026-11-13 from=2026-05-18 | PASS |
