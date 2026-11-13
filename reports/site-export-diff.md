# 站点导出对照表（v1 → 契约 v1）

- 生成时间：2026-11-13T14:13:44+08:00
- 输入（v1）：`/private/tmp/v1`
- 站点基线：`/Users/zhongjixiangyou/Documents/deepseek-harness/default-workspace/era-trend-radar`（只读）
- 输出：`/private/tmp/site3`（暂存，未写 src/data）

## 一、数量概览

| 类型 | 站点现有 | 本次新增 | 合并后 | 同 id 冲突 |
|---|---|---|---|---|
| events | 11 | 8 | 8 | 0 |
| trends | 7 | 5 | 7 | 0 |
| forecasts | 2 | 2 | 2 | 0 |
| snapshots | 0 | 0（原样沿用） | 0 | 0 |

## 二、本批新增条目

### 事件（8）

- `ev-20261111-cn-nev-oct` 10月新能源车销量占比64.3%，出口同比增1.4倍
- `ev-20261109-el-nino-11t` IFAD警告厄尔尼诺未来六年或致11万亿美元损失
- `ev-20261105-cn-marriage-9m` 前三季度结婚登记464.8万对，同比下降9.8%
- `ev-20260929-nrc-smr-first` 美国NRC批准首座小型模块化反应堆建设
- `ev-20260928-cn-profit-1-8` 1-8月规上工业企业利润同比增长15.7%
- `ev-20260917-ai-dc-power-161gw` 2026年全球数据中心电力需求达161GW，同比增31%
- `ev-20260916-fed-hike-sep` 美联储重启加息25个基点，12比0全票通过
- `ev-20260828-mideast-war-6m` 中东战事半年：霍尔木兹受阻，布伦特一度突破90美元

### 趋势（5）

- `tr-climate-food-inflation` 气候→食品通胀
- `tr-geopolitics-energy-premium` 地缘能源溢价
- `tr-cn-nev-demand-shift` 新能源车需求换挡
- `tr-cn-demographic-decline` 婚育与人口下行
- `tr-ai-power-constraint` AI 电力约束

### 判断（2）

- `fc-202611-fed-no-cut-2026` 2026年12月31日前，美联储不会下调联邦基金利率目标区间
- `fc-202611-ai-power-delay-2027` 2027年6月30日前，至少一家超大规模云厂商公开披露因电力或并网约束推迟数据中心上线

## 三、若不合并、直接写 src/data 会被覆盖的条目

当前合并策略为「只增不删」，以下站点现有 11 条均保留、不覆盖；`--no-merge` 仅用于对照。

- `ev_cn-nev-oct`
- `ev_el-nino-11t`
- `ev_cn-marriage-9m`
- `ev_cn-profit-1-9`
- `ev_cn-gdp-q3-2026`
- `ev_cn-pmi-sep-2026`
- `ev_nrc-smr-first`
- `ev_cn-profit-1-8`
- `ev_ai-dc-power-161gw`
- `ev_fed-hike-sep`
- `ev_mideast-war-6m`

## 四、字段级差异（站点现有 vs 本批新增）

### events

| 层级 | 站点有 / 本导出无 | 本导出有 / 站点无 |
|---|---|---|
| top | analysis、evidenceGrade、metrics、timeline、watch | — |
| sources | — | — |
| trends | — | — |
| scoreHistory | — | reason |

### trends

| 层级 | 站点有 / 本导出无 | 本导出有 / 站点无 |
|---|---|---|
| top | counterCount、evidenceCount、supportCount | — |
| revisions | — | — |

### forecasts

| 层级 | 站点有 / 本导出无 | 本导出有 / 站点无 |
|---|---|---|
| top | — | — |
| history | — | — |

说明：`metrics` / `timeline` / `watch` 是站点可选字段，本期 v1 无对应数据；`backfill` 仅在 v1 `retro.isRetro=true` 时输出。其余层级未列差异即完全一致。

## 五、派生字段与换算口径（需人工确认）

- **id**：`ev_<slug>` + occurredAt → `ev-YYYYMMDD-<slug>`；`tr_<slug>` → `tr-<slug>`；`fc_<slug>` + createdAt → `fc-YYYYMM-<slug>`。
- **scores**：v1 六维（25/20/20/15/10/10 分制）线性放大到 0–100——structural×4、scope×5、signal×5、delta×6.67、timeliness×10、crossDomain×10，取整；**`scores.total` 是站点当前分的唯一来源**（commit 31b7629 起硬校验），按六维加权取整写入，scoreHistory 末条与之相同。
- **scoreHistory**：站点要求非空且末条 total 等于六维加权公式；本期按换算后的六维重算，写入一条 dated 事件收录日。
- **credibility**：verified→high、partial→medium、unverified→low；**status**：published 且 high→confirmed、published 且 medium→partial、其余→pending（low 强制 pending）。
- **趋势 title**：v1 无 title 字段，取 thesis 前 42 字；**domain**：按 id 前缀（cn-/global-）与关键词推断。
- **趋势 confidence**：从 `currentJudgment` 的措辞取（高 75 / 中高 65 / 中 55 / 中低 45 / 低 35），取不到再退 stage（45/65/70/40/15）。合并进站点的那两条沿用站点已发布值，不覆盖。
- **趋势 revisions**：只写置信度真实变化的节点；首条 from=null、末条 to=confidence，`from==to` 的空转不写。
- **反证**：站点无 counterEvidence 字段，追加到 `judgment` 末尾「反证：…（n 条，见 content/trends/<id>.json）」，不静默丢。
- **趋势合并**：语义重复的 v1 趋势并入站点已发布 id（id 不改），只追加指标与反证段。
- **判断**：`statement→claim`、`createdAt→madeAt`、`resolveBy→dueBy`、`probability×100→confidence`；`resolution.outcome` hit→supported / partial→weakened / miss→falsified / unresolved→unresolved；`void`→unresolved。`counter` 站点要求数组，v1 无对应字段，暂为空数组。

### 趋势合并明细

- 无（本轮无合并）

### 趋势置信度来源

| 站点 id | confidence | 来源 |
|---|---|---|
| `tr-climate-food-inflation` | 35 | 判断原文「低」 |
| `tr-geopolitics-energy-premium` | 55 | 判断原文「中」 |
| `tr-cn-nev-demand-shift` | 45 | 判断原文「中低」 |
| `tr-cn-demographic-decline` | 65 | 判断原文「中高」 |
| `tr-ai-power-constraint` | 65 | 判断原文「中高」 |


## 六、内容与契约缺口（未自动改，等人工）

### 未导出字段清单（v1 有、站点契约无落点）

| 层 | v1 字段 | 站点契约 | 处理 |
|---|---|---|---|
| event | `analysis` | 无对应字段 | 不导出（长文判断留在 v1，站点只放 summary + facts） |
| event | `dedupeKey` | 无 | 不导出（内部去重键，不外泄） |
| event | `display` | 无 | 不导出（站点按 status / scores.total 自己算门禁） |
| event | `metrics` | `EraEvent.metrics` 存在 | **暂不导出**：v1 schema 未收编（validate 报未知字段），内容侧保留，等后端决定收编或移出 |
| trend | `evidenceRefs` | 无 | 不导出；站点通过 `events[].trends[]` 反查关联事件 |
| trend | `counterEvidence` | 无 | 已写进 `judgment` 末尾「反证：…」段，不丢 |
| forecast | `indicators` | 无 | 不导出；站点证据位只有 `support` / `counter` |
| forecast | `resolution.note` / `evidenceUrls` | 无 | 本批 2 条均为 open，未出现；resolve 后需人工定落点 |

### 逐条提示

- 未找到人工置信度文件 /Users/zhongjixiangyou/Documents/deepseek-harness/default-workspace/era-trend-radar/reports/trend-confidence.json，回退 currentJudgment / stage 映射
- ev-20261111-cn-nev-oct：summary 33 字 < 契约建议 80–160 字（未改内容，留待人工补写）
- ev-20261111-cn-nev-oct：v1 字段 analysis 在站点契约中无落点（内容未导出）
- ev-20261111-cn-nev-oct：v1 字段 dedupeKey 在站点契约中无落点（内容未导出）
- ev-20261111-cn-nev-oct：v1 字段 display 在站点契约中无落点（内容未导出）
- ev-20261109-el-nino-11t：summary 31 字 < 契约建议 80–160 字（未改内容，留待人工补写）
- ev-20261109-el-nino-11t：v1 字段 analysis 在站点契约中无落点（内容未导出）
- ev-20261109-el-nino-11t：v1 字段 dedupeKey 在站点契约中无落点（内容未导出）
- ev-20261109-el-nino-11t：v1 字段 display 在站点契约中无落点（内容未导出）
- ev-20261105-cn-marriage-9m：summary 35 字 < 契约建议 80–160 字（未改内容，留待人工补写）
- ev-20261105-cn-marriage-9m：v1 字段 analysis 在站点契约中无落点（内容未导出）
- ev-20261105-cn-marriage-9m：v1 字段 dedupeKey 在站点契约中无落点（内容未导出）
- ev-20261105-cn-marriage-9m：v1 字段 display 在站点契约中无落点（内容未导出）
- ev-20260929-nrc-smr-first：v1 字段 analysis 在站点契约中无落点（内容未导出）
- ev-20260929-nrc-smr-first：v1 字段 dedupeKey 在站点契约中无落点（内容未导出）
- ev-20260929-nrc-smr-first：v1 字段 display 在站点契约中无落点（内容未导出）
- ev-20260928-cn-profit-1-8：v1 字段 analysis 在站点契约中无落点（内容未导出）
- ev-20260928-cn-profit-1-8：v1 字段 dedupeKey 在站点契约中无落点（内容未导出）
- ev-20260928-cn-profit-1-8：v1 字段 display 在站点契约中无落点（内容未导出）
- ev-20260917-ai-dc-power-161gw：v1 字段 analysis 在站点契约中无落点（内容未导出）
- ev-20260917-ai-dc-power-161gw：v1 字段 dedupeKey 在站点契约中无落点（内容未导出）
- ev-20260917-ai-dc-power-161gw：v1 字段 display 在站点契约中无落点（内容未导出）
- ev-20260916-fed-hike-sep：summary 43 字 < 契约建议 80–160 字（未改内容，留待人工补写）
- ev-20260916-fed-hike-sep：v1 字段 analysis 在站点契约中无落点（内容未导出）
- ev-20260916-fed-hike-sep：v1 字段 dedupeKey 在站点契约中无落点（内容未导出）
- ev-20260916-fed-hike-sep：v1 字段 display 在站点契约中无落点（内容未导出）
- ev-20260828-mideast-war-6m：summary 32 字 < 契约建议 80–160 字（未改内容，留待人工补写）
- ev-20260828-mideast-war-6m：v1 字段 analysis 在站点契约中无落点（内容未导出）
- ev-20260828-mideast-war-6m：v1 字段 dedupeKey 在站点契约中无落点（内容未导出）
- ev-20260828-mideast-war-6m：v1 字段 display 在站点契约中无落点（内容未导出）
- tr-fed-hike-cycle：v1 趋势 tr_global-rates-rehike 与站点已发布趋势语义重复，合并进 tr-fed-hike-cycle，不新增条目
- tr-cn-profit-recovery：v1 趋势 tr_cn-industrial-margin-repair 与站点已发布趋势语义重复，合并进 tr-cn-profit-recovery，不新增条目
- fc-202611-fed-no-cut-2026：v1 indicators 在站点契约中无字段（站点把证据写在 support/counter）
- fc-202611-ai-power-delay-2027：v1 indicators 在站点契约中无字段（站点把证据写在 support/counter）

## 七、站点校验

自检未通过，1 条：

- snapshots: 必须是非空数组

