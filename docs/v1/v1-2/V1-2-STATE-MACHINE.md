# V1-2 Contract Freeze — State Machine

任务、编辑、公开和回收站是四个正交维度；不得用一个 `internal_state` 字段替代它们。

## 任务维度

`discovered → fetch queued/running → raw saved → extraction queued/running → extraction succeeded|failed`。

对每个 `classification|summary|claim_extraction|entity_extraction` 独立：`not_requested → queued → running → succeeded|invalid|failed|retry_wait`。失败/invalid 必须可见并沿用现有 retry、lease、budget 规则；成功结果 append-only。`analysis_ready` 只是读模型派生状态，不是可编辑状态。

## EDITORIAL / lifecycle 维度

`internal (no revision)` → `internal (revision N)` → `internal (revision N+1)`。

每次保存、采用 AI 建议、字段覆盖都产生新 revision；旧 revision 不更新。并发保存的 `base_revision` 不是当前 revision 时返回 409 `editorial_revision_conflict`，不产生部分写入。

`internal` → `trash` 由管理员显式 trash；trash 只改变 lifecycle，不删除 RAW、AI_RESULT、evidence 或 audit。trash 中不出现在正常 Internal Library，但出现在回收站和文档审计中。

`trash` → `internal` 由管理员显式 restore，恢复最后一个 EDITORIAL revision；不自动 reanalysis，不自动 grant/public。

trash 文档默认禁止 reanalysis，避免隐藏资料继续产生任务；负责人若选择允许，必须明确规定为恢复后才能触发，而非静默例外。

## 公开维度

V1-2 所有状态均保持 `public_authorized=false`、无 publication grant、无 manifest、无 public projection。编辑、采用 AI、trash、restore 都不得改变公开维度。

## 关键转换

| 动作 | 前置 | 结果 | 必须记录 |
|---|---|---|---|
| 保存编辑 | internal、`base_revision` 当前 | 新 EDITORIAL revision | principal/request_id、before/after digest、字段 diff |
| 采用 AI 建议 | internal、指定 valid analysis result | 新 EDITORIAL revision；AI_RESULT 不变 | task/result ID、采用字段、原因 |
| 单任务重分析 | internal、extraction succeeded、预算未超 | 新 model run/AI_RESULT；EDITORIAL 不变 | task/prompt/model/job/budget |
| trash | internal | lifecycle=trash；列表隐藏 | delete actor/reason/before/after |
| restore | trash | lifecycle=internal；revision 保留 | restore actor/before/after |

所有写动作使用现有 OIDC principal、`uap.request_id`/Idempotency-Key、单事务；失败整体 rollback。相同 request-id+payload 重放返回首次资源；不同 payload 返回幂等冲突。
