# 窗口契约 v1 独立验证报告

- 生成时间：2026-11-14T13:19:43+08:00
- 口径：Asia/Shanghai，asOf=2026-11-14T12:00:00+08:00，days=180
- 契约正向窗口（独立推导）：from=2026-05-19 to=2026-11-14
- 结果：PASS 3 / FAIL 0 / BLOCKED 0

| 编号 | 组 | 用例 | 契约期望 | 实际结果 | 结论 |
| --- | --- | --- | --- | --- | --- |
| I1 | I | 英文带空格标题：裸 len()=126 但 char_len=119 → export 接受 | len=126 char_len=119，无 title 报错 | len=126 char_len=119 title_errors=[] | PASS |
| I2 | I | char_len=121（超 120）→ 仍必须拒绝 | title 报错且提到 121 字 | len=121 char_len=121 title_errors=['event[ev_qa_len_001]：title 121 字，需在 4–120'] | PASS |
| I3 | I | char_len=120（正好上限）→ 边界接受 | 无 title 报错 | len=120 char_len=120 title_errors=[] | PASS |
