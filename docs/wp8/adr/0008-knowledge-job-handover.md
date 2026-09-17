# ADR-0008：analysis_result 到知识任务的事务性交接

- 状态：Accepted for `G8-FROZEN-20260821-02`
- 日期：2026-08-21
- 前置：ADR-0004、ADR-0005、G4、G7-R2

## 1. 背景

WP7 把结构化结果追加到 `core.analysis_results`，但不写 Claim、Entity Candidate 或 Evidence。WP4 已提供 durable jobs、attempt、lease、retry 和 dead letter。Worker 扫描结果后无任务写表会绕开这些保证；Publisher Outbox 又只用于发布凭据，不能用于入队普通 Worker 任务。

`uap_model_governance` 无 `ops.enqueue_job` 执行权，也不应扩权。因此交接必须发生在 PostgreSQL 内，与 analysis_result 的插入处于同一事务，同时不改变 WP7 Provider、Prompt、model-io 或追加语义。

## 2. 决策：AFTER INSERT 同事务扇出

在 `core.analysis_results` 上建立 `AFTER INSERT` 触发器。仅当：

```text
NEW.validation_status = 'valid'
AND NEW.result_type IN ('claim_extraction', 'entity_extraction')
```

触发器调用私有 `ops.enqueue_followup_job(NEW.id)`：

| result_type | job_type | idempotency_key |
|---|---|---|
| `claim_extraction` | `resolve_claims` | `resolve-claims:{analysis_result_id}` |
| `entity_extraction` | `resolve_entities` | `resolve-entities:{analysis_result_id}` |

函数与触发器：

- `SECURITY DEFINER`，固定可信 `search_path`；
- owner 持有，`REVOKE ALL FROM PUBLIC`；
- 不向任何登录角色授予 `enqueue_followup_job` EXECUTE；
- 函数参数只有 `analysis_result_id`，调用者不能提供 job type、key 或 payload；
- 失败会使 analysis_result、model_run 和 `finish_model_job` 所在事务整体回滚。

translation、summary、classification、invalid 结果均不入队；不产生 `resolve_relations`。

## 3. `knowledge.v2` payload

`ops.jobs.payload_schema_version` 和 payload 内版本均为 `knowledge.v2`。payload 只能包含下列字段：

```json
{
  "payload_schema_version": "knowledge.v2",
  "analysis_result_id": "<uuid>",
  "analysis_result_sha256": "<64 hex>",
  "analysis_schema_version": "ai.v1",
  "document_version_id": "<uuid>",
  "result_type": "claim_extraction | entity_extraction",
  "model_run_id": "<uuid>",
  "input_sha256": "<64 hex>",
  "extraction_anchor_status": "matched | missing | ambiguous",
  "extraction_id": "<uuid or null>"
}
```

字段全部由函数在同一数据库快照中从 `analysis_results`、`model_runs`、`extractions` 解析：

1. `analysis_result_sha256 = analysis_results.result_sha256`；
2. `analysis_schema_version = analysis_results.schema_version`；G7 实际持久化值为 `ai.v1`；
3. model run 必须与 analysis_result 的复合 FK 字段一致，且状态为 `succeeded`；
4. `input_sha256 = model_runs.input_sha256`；
5. extraction 锚定按 ADR-0010 的 0/1/>1 规则形成 status/id。

不得放入正文、Prompt、原始响应、Token、费用、selection 或自由文本。

missing/ambiguous 不阻断 WP7 已成功事务：仍入队，`extraction_id=null`。后续若结果非空，resolver 终态失败；若结果是合法空数组，不需要 locator，仍成功零物化。

## 4. 严格幂等

`ops.enqueue_followup_job` 利用 `ops.jobs.idempotency_key` 全局唯一，但冲突处理必须比较已有行：

```text
job_type
payload_schema_version
payload（jsonb 等值）
```

- 三者完全相同：返回已有 job id，不 UPDATE 任何字段；
- 任一不同：RAISE SQLSTATE `23505`，error `knowledge_idempotency_payload_conflict`；
- 不允许 `ON CONFLICT DO UPDATE payload`，也不允许静默返回冲突旧 job。

这保证“同 key”同时代表“同一不可变交接事实”。

## 5. 与 `analysis_selections` 解耦

`analysis_selections` 表示后续审核/发布使用的当前选择，不是知识物化闸门。

冻结：

1. 每条 valid claim/entity 分析都入队，无论有无 selection、是否当前选择；
2. WP8 不读、不写、不 supersede selections；
3. 未被选择的结果只成为内部候选，仍不能进入 public；
4. invalid 结果不能通过 selection 绕过 valid 要求；
5. materialize 函数禁止 JOIN selections 作为前置。

## 6. 消费者激活顺序

触发器从 WP8.1 起 ENABLE，允许任务先排队；`ops.claim_job.p_job_types` 必须始终是本进程已部署 handler 的子集。

| 阶段 | 领取集合变化 |
|---|---|
| WP8.1–8.2 | 不含任何 `resolve_*` |
| WP8.3 | claim handler 部署并通过启动自检后，加入 `resolve_claims` |
| WP8.4 | entity handler 部署并通过启动自检后，加入 `resolve_entities` |
| 全 WP8 | 不加入 `resolve_relations` |

启动自检必须证明 handler registry 有对应实现；配置声明而实现缺席时进程拒绝启动。若通用分发器仍误领未知/关系任务，必须用现有 `finish_job` 终态失败，禁止 succeeded。

## 7. 存量与漏入队补偿

所有补偿只写 jobs，不写知识表，并复用 `enqueue_followup_job(analysis_result_id)` 的唯一 payload builder。

### 7.1 迁移窗口回填

WP8.1 迁移在触发器 ENABLE 后，按 `(created_at,id)` 遍历 **全部**既有 valid claim/entity 结果并调用私有 enqueue 函数。已有完全相同 job 返回原 id；已有同 key 冲突 payload 会使迁移 fail closed，而不是被 `NOT EXISTS` 过滤掩盖。并发插入由相同 key 收敛。invalid 和其它 result type 不处理。

### 7.2 上线后显式 reconciliation

新增：

```text
ops.reconcile_knowledge_jobs(
  p_after_created_at timestamptz,
  p_after_id uuid,
  p_created_before timestamptz,
  p_limit integer default 500
)
```

- `SECURITY DEFINER`，仅授予 `uap_scheduler`；
- `p_limit` 范围 1–1000；
- 以 `(created_at,id)` 严格大于调用者 cursor、且不晚于截止时间的顺序分页；首批 cursor 为 NULL；
- 选择该页全部 valid claim/entity，不先过滤已有 key；
- 对每行调用同一个私有 enqueue 函数；
- 返回处理的 analysis/job id、anchor status 和下一页 cursor；完全相同 job 幂等返回，冲突 payload 立即失败；
- 由操作员显式、分批调用，不是常驻无界 scanner；
- 不接受外部 payload，因此不能形成第二套锚定算法。

### 7.3 dead letter

已有 job 进入 dead 后使用 `ops.requeue_dead_letter`；不得以同 analysis_result 创建第二个 key。已 succeeded 但数据不完整视为缺陷修复，不由 scanner 补写。

## 8. Worker 消费与至少一次语义

Worker 用现有 `claim_job` 领取。每个 handler：

- 只消费冻结 job type；
- 读取 job 内不可变 provenance；
- 物化以 `(analysis_result_id, ordinal)` 幂等；
- 按 ADR-0011 在领域事务中调用 materialize 与受控 finish；
- 完全复用 WP4 retry/dead-letter 状态机。

## 9. 后果

- 每条目标 valid 分析都有可重试、可死信、可审计的后续任务；
- 不扩张模型治理角色，不引入第二队列或 Publisher 越权；
- payload 可独立证明分析、模型输入与 extraction 锚定；
- 合法空结果不会因无 extraction 而被误判为失败。

## 10. 不采用

- Worker 扫描结果后直接写知识表；
- Publisher Outbox 入队普通 Worker 任务；
- 给 model governance 授 enqueue 权；
- 只物化当前 selection；
- reconciliation 接收调用者自制 payload；
- 同 key 不同 payload 静默返回旧 job。
