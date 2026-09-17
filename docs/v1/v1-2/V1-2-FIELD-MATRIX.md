# V1-2 Contract Freeze — Field Matrix

原则：RAW、AI_RESULT、EDITORIAL 分层；任何人工保存都不能改写 RAW 或 AI_RESULT。下表的 Existing/Adapt/New 指当前 snapshot 能力，不代表本轮已实施。

| 产品字段/动作 | 权威层 | 当前载体 | V1-2 访问 | 计划处理 |
|---|---|---|---|---|
| canonical URL、source、source item key、首次/最近发现 | RAW | `core.documents`、`ingest.sources` | 只读 | 直接复用 |
| 原始 Atom/Web/PDF bytes、媒体类型、hash、抓取元数据 | RAW | artifact/stored-object、`core.document_versions.metadata` | 只读 | 直接复用 |
| 原文、extraction outcome、derived text、output SHA、locator map | RAW/derived extraction | `core.extractions`、object store | 只读 | 直接复用 |
| 原始标题/作者/发布时间 | RAW | `core.document_versions`、`core.extractions` | 只读；编辑标题另存 EDITORIAL | 适配详情 DTO |
| classification relevance、labels、suggested category | AI_RESULT | `core.analysis_results` (`result_type=classification`) | 只读建议；可显式采用字段 | 直接复用 + 新 editorial adopt 动作 |
| summary、bullets、language | AI_RESULT | `core.analysis_results` (`summary`) | 只读建议；可显式采用字段 | 直接复用 + 新 editorial adopt 动作 |
| claims、source_statement、speaker、type/status、evidence locator | AI_RESULT | `analysis_results`、`core.claims`/`claim_evidence`、`core.evidence_spans` | AI 结果只读；EDITORIAL 可覆盖允许值 | 适配 overlay |
| entities、type、aliases、evidence | AI_RESULT | `analysis_results`、`core.entity_candidates`、`core.entities`、`core.evidence_spans` | AI 候选只读；EDITORIAL 可覆盖允许值 | 适配 overlay |
| model/provider/prompt/hash/input hash/token/cost/status | AI_RESULT provenance | `ops.model_runs`、`ops.prompt_versions` | 只读 | 直接复用 |
| 当前标题、中文摘要、bullets、category、labels | EDITORIAL | 当前不存在 | 可编辑 | 新增 revision snapshot |
| claim 文本/type/status/speaker/source statement 的人工覆盖 | EDITORIAL | 当前不存在；manual claim 只支持新增 | 可编辑；证据只能引用已有 span | 新增 revision overlay |
| entity name/type/aliases/绑定 ID 的人工覆盖 | EDITORIAL | 当前仅 candidate accept/bind/merge | 可编辑；不自动 merge | 新增 revision overlay |
| 字段来源（人工或 adopted AI result） | EDITORIAL provenance | 当前不存在 | 只读显示 | revision `source_map` |
| 文档 internal/trash 生命周期 | lifecycle | `core.documents` 无字段 | 由 trash/restore 动作改变 | 最小 migration |
| publication authorization/grant/manifest | PUBLIC boundary | publication 表 | V1-2 始终无授权 | 不修改 |

## EDITORIAL 形态

建议增加单一 `core.editorial_revisions` append-only 表，而不是为六种对象建六套平行体系。每行至少包含：`document_version_id`、单调 `revision_no`、`content jsonb`、`source_map jsonb`、`adopted_from`（任务到 analysis_result ID）、`created_by`、`created_at`、`superseded_at`。`content` 只承载可编辑字段与 claim/entity overlay；overlay 中引用现有 `claim_id`、`entity_id`、`evidence_span_id`，未绑定实体保留为 editorial candidate，不写入 canonical entity registry。

`core.documents` 增加最小 lifecycle 字段（`deleted_at`、`deleted_by`、`delete_reason`；恢复清空 deleted 字段并留下审计）或等价受控 lifecycle 表。两者均需由 migration 实施；不能用查询约定伪造回收站。

## 只读 provenance 规则

RAW bytes、source URL/抓取元数据、原始 model response、prompt version/hash、tokens/cost、validation status、evidence 原始 locator、audit history 均不可由普通编辑覆盖。Evidence 只能被 EDITORIAL claim/entity 引用或取消引用；不得改 locator/text。AI_RESULT 保留多个版本，EDITORIAL 仅在“采用 AI 建议”动作后写入新的 revision。

现有 `core.analysis_selections`/`audit.select_analysis_result` 只冻结支持 `claim_extraction` 与 `entity_extraction`；不能把它扩展成 summary/classification 的编辑覆盖。V1-2 的所有任务采用都写入 EDITORIAL revision，并保留 `adopted_from` provenance。
