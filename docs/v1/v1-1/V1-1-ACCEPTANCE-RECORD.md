# V1-1 最终验收记录

状态：**PASS / ACCEPTED / CLOSED**  
验收日期：2026-09-17（Asia/Shanghai）  
验收对象：`377fdd24-bf7c-48df-a451-48937d4f60fd`  
固定代码基线：`4a14b368c96808edf9dfbc1dfd5fc0ce9fbf6638`  
代码仓库：`recovery/r0-20260907-Sk1qtq/recovered-repo`

本记录是 V1-1 的最终只读验收归档。除回归缺陷外，不向 V1-1 增加功能、门禁或验收条件。`resolve_claims`、`resolve_entities` 等未纳入冻结 V1-1 DoD 的工作转入后续阶段。

## 1. 冻结产品链

已证明同一真实 Reddit 资料完成：

`Reddit Atom RAW → extraction succeeded → classification valid → summary valid → claim valid → entity valid → Internal Library analysis_ready`

## 2. 正例证据

### 2.1 Reddit Atom RAW / Extraction

- Source：Reddit r/UFOs new，`source_type=rss`。
- Feed：`https://www.reddit.com/r/UFOs/new/.rss`。
- Canonical URL：`https://www.reddit.com/r/UFOs/comments/1wg2ic8/a_strange_encounter_my_coworker_told_me_about/`。
- Document Version：`7ded8d9a-1713-47b0-9e2d-02ec2c113b57`。
- Atom RAW version：`1d3da748-0014-4463-9b71-e01aea3571e0`。
- RAW object：`01a0aa92-76ef-74b7-8850-a4a3d91052a5`。
- RAW SHA256：`d9f7e1c9fd1a9a722fa9c0d304ee26cc820227e342b1b70afa274cb090e3f64f`。
- RAW bytes/media：`6796` / `application/xml`。
- Atom title：`A strange encounter my coworker told me about — South America`。
- Atom author：`/u/QuoteZestyclose3269`。
- Atom published：`2026-09-14T12:49:35+00:00`。
- Atom entry content：`5687` 字符；link 指向同一 canonical URL，未命中 challenge 特征。
- Extraction ID：`10aeea43-b9ae-4e73-afb0-3a651cbd1604`。
- Outcome：`succeeded`；extractor：`reddit_source_text 1.0.0`。
- Derived text object：`01a0ab17-9e7d-7dbc-9d9b-14b35379491f`。
- Output SHA256：`02c0261c8a328b5566225cd5a9e6c1b7c600c2d3ce08cc46743c2f1b9ab3ed58`。
- Derived bytes：`5243`。
- Extracted author：`/u/QuoteZestyclose3269`。

采集 job 只持久化 RSS feed URL；extraction job 只引用已保存的 Atom `source_object_id`，没有帖子 HTML 或帖子级 `/.rss` URL，也没有额外帖子级 fetch job。

### 2.2 四个真实 DeepSeek 结果

| 任务 | Run ID | Prompt version / ID | Prompt hash | Schema | Tokens | 精确费用 |
|---|---|---|---|---|---:|---:|
| classification | `1a103c93-eea9-463e-bf7b-26d0bba16fd6` | `v1.2.0` / `00000000-0000-7000-8000-000000001501` | `df50726dae4691258b9b92331f266b0225bc0b716d7c69375457cb073f982177` | `ai.v1 valid` | `2054 / 152` | `0.005324 CNY` |
| summary | `853b76f5-4fca-4b51-a945-35b709d64d92` | `v1.2.0` / `00000000-0000-7000-8000-000000001502` | `296906d73a121325db6aebd5958cacbc73cca26ed841586346a2adfcb721751a` | `ai.v1 valid` | `1823 / 959` | `0.011318 CNY` |
| claim_extraction | `ed00ce3d-e515-4bbc-b6d4-bdb2e2b09ba7` | `v1.2.0` / `00000000-0000-7000-8000-000000001503` | `5f8bf4c4e3e9cdddf42988c2b302a6e2360d65690e8ef2b2d86ca5e0ff87b4cc` | `ai.v1 valid` | `2586 / 4099` | `0.035957 CNY` |
| entity_extraction | `b0d2af37-c20d-435c-a58b-c2ded967bb0d` | `v1.2.0` / `00000000-0000-7000-8000-000000001504` | `3b70f005aa19db13f5a77a6066b0b73a4a0b8cec4fa0d2246f13fdd5bce19d97` | `ai.v1 valid` | `2462 / 640` | `0.008288 CNY` |

Classification：`relevant`、confidence `0.9`、category `sighting`、labels `10`。  
Summary：中文 summary 已生成，`bullets=14`。  
Claims：`28` 条，`28` 条 evidence；每条包含 claim/source statement/assertion status/locator。  
Entities：`18` 条，`18` 条 evidence；每条包含 name/entity type/aliases/locator。

## 3. Challenge 负例

真实 challenge RAW：

- Document：`17d7a596-edcc-4ee9-802f-c22cdc973990`。
- Failed version：`c90a82cc-0653-4f6e-99e5-a9f1ef888864`。
- RAW：`text/html`，`167021` bytes。
- RAW 命中：`Prove your humanity`、`not for bots`、`CAPTCHA`、`challenge`。
- Extraction：`791301ce-35a0-4a84-bbfc-6a54d67c9064`。
- Outcome：`failed`。
- Error：`source_bot_challenge`。
- `text_object_id=NULL`，`output_sha256=NULL`，无 derived 正文。
- Attempt：`terminal_failure`。
- 失败 extraction 时间之后没有 DeepSeek model run。

该版本更早存在一次成功的旧 HTML extraction 及其历史认证失败记录；历史认证失败为 `0 / 0 / 0`，发生在 fail-close 之前，不构成 fail-close 后续调用。

## 4. Internal Library 与公开边界

指定 Document 的内部详情在停止服务前只读确认：

- `internal_state=analysis_ready`。
- 原文、中文摘要、bullets、claims、claim evidence、entities 均可查看。
- `public_authorized=false`。
- publication grants：`0`。
- publication manifests：`0`。
- Public documents：`0`。
- Public search rows：`0`。

自动采集和 AI 分析只进入内部资料库，没有自动公开授权。

## 5. 预算与运行态

- 当月累计：`141049 micro-CNY = 0.141049 CNY`。
- 月预算上限：`20000000 micro-CNY = 20 CNY`。
- `warning=false`，`blocked=false`。
- Claim job：`08c93343-86fe-1c13-e085-ec373ee58f64`，`succeeded`，1 attempt。
- Entity job：`2589edf3-c165-ebe7-9f62-259c3223fc3c`，`succeeded`，1 attempt。
- 隔离窗口内没有其他 Document 的 V1 job 被处理。
- 当前可领取 V1 jobs：`0`。
- Worker、Library、Scheduler：均已停止。
- Scheduler 未用于本次 Claim/Entity 复验。

全局剩余 `resolve_claims` / `resolve_entities` 不属于 stock V1 Worker 的 `fetch_source` / `extract_document` / `analyze_document` claim 范围，本轮未消费，不属于 V1-1 关闭条件。

## 6. 冻结 DoD 判定

V1-1 冻结 DoD 的真实来源、Atom RAW、正文 extraction、四类 DeepSeek 结果、schema valid、内部详情、真实 token/cost、无 publication grant、Public 投影为空及 challenge fail-close 均已证明。此前已观察三个连续 30 分钟调度周期，并确认 canonical URL 去重无重复。

结论：**V1-1 PASS / ACCEPTED / CLOSED**。

V1-1 进入停止线；后续 `resolve_claims`、`resolve_entities` 以及 WAR.GOV、公开发布、复杂图谱等不纳入本次关闭条件，转入后续阶段。
