# ERA 趋势雷达 · 工程规则

> 给所有在本仓库干活的人与 agent。每条规则背后都是一次真实的返工，删改前先读理由。
> 证据：`coders/era-trend-radar/日志.md`。

## 1. 一条规则只能有一份实现

长度上限、时间窗口、分数阈值这类「规则」，只在 `scripts/_core.py` 定义一次（`TITLE_MAX`、`clip()`、`Window`…），别处一律引用，不抄数字。

反例：标题长度曾在导出、校验、页面各写一套，改到第三次才炸 —— `86f8414` → `18b9e56` → 11-16 的 10 条 `F_LEN`。

## 2. 入口先截断，写盘前自检

采集与 promote 造数据时按契约上限截断（`_core.clip`），写盘前用 `_core.validate_event` 跑一遍；不合格的丢弃并计入 `rejectedByContract`，不让坏数据流到 CI。

反例：美联储 195 字长标题未截断，`ERA collect` 因 10 条 `F_LEN` 整轮作废（run `43801438140`）。

## 3. 改配置要验证它真的被读到

`pipeline.config.json`、`config/**`、页面里的常量，改完必须确认**消费端**的值变了，不能只看定义处。

反例：`HOMEPAGE_MIN_TOTAL` 只在 `scripts/export_astro.py` 算成 `display.homepage`，`src/pages/index.astro` 从没读过 → 门槛是假的，48 分新闻照样上首屏。

## 4. 不以「绿了」当合格

Actions 绿 ≠ 真跑（可能是手工 dispatch）；构建成功 ≠ 生产版本；源返回 200 ≠ 有内容（HTML 伪装）。

判断前先看 `gh run list --event schedule`、看线上产物、看解析出的条数。

## 5. 提交前跑本地门禁

```bash
./scripts/local-gate.sh --fast   # 改代码/配置：内容校验 + 草稿校验（约 5 秒）
./scripts/local-gate.sh          # 改内容/页面：再加构建与泄漏检查（约 1 分钟）
```

CI 跑同一套，本地过基本等于线上过。

## 修完一个 bug 之后

用 `bugfix-retro` skill 走一遍：判断属于哪类根因、把新学到的一条写回 skill、能变成脚本或门禁的就做掉。
不写回去，下次还会付同样的成本 —— 11-13 到 11-16 的 63 次提交里 28 次是在修 bug。
