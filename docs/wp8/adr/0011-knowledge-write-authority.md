# ADR-0011：知识写入权威、Bundle 与任务收口

- 状态：Accepted for `G8-FROZEN-20260821-02`
- 日期：2026-08-21
- 前置：WP1 permissions、WP4 jobs、WP5 lease guard、WP7 model governance

## 1. 方案选择

G7 基线中 `uap_worker` 仍可直接写多数 core 表。只靠 Python 先查 lease，无法在数据库层证明“没有 resolve attempt 就不能写知识”。新增 `uap_knowledge` 登录角色又会扩大密钥和进程面。

冻结方案 C：

- 领取身份仍是 `uap_worker`；
- 从 Worker/API 收回知识表直接 DML；
- 写入只经 owner 的 `SECURITY DEFINER` 函数；
- 每次 materialize 必须绑定活动 resolve lease；
- 任务完成与领域写入同一事务。

不采用“Worker 保留 DML、靠自觉”或本阶段新增登录角色。

## 2. 函数与角色

所有函数固定可信 `search_path`、`REVOKE ALL FROM PUBLIC`，并检查 `session_user` 而非 `current_user`。

| 函数 | 用途 | 登录角色 EXECUTE |
|---|---|---|
| `ops.enqueue_followup_job` | 私有同事务入队 | 无 |
| `ops.reconcile_knowledge_jobs` | 受控补偿入队 | `uap_scheduler` |
| `ops.require_active_resolution_job_lease` | 锁定并验证 resolve job/attempt | `uap_worker` |
| `ops.finish_knowledge_job` | 写安全 metrics 并调用 WP4 finish | `uap_worker` |
| `core.compute_evidence_locator_sha256` | 数据库权威 span hash | 无登录角色，供 materialize 内部调用 |
| `core.compute_claim_fingerprint` | 数据库权威 Claim 指纹 | 无登录角色，供 claim materialize 内部调用 |
| `core.materialize_claim_bundle` | Claims/Spans/Evidence | `uap_worker` |
| `core.materialize_entity_bundle` | Candidates/Spans/Join evidence | `uap_worker` |
| `core.canonical_entity_id` | 只读 canonical 解析 | 明确只读角色，非 PUBLIC |
| `core.merge_entities` / `core.reverse_entity_merge` | 默认关闭状态机 | **无登录角色，WP9 再授权** |

model governance、publisher、public reader 均无知识写入函数权限；scheduler 只能补偿入队；API 在 WP8 无知识 DML 或 merge 执行权。

## 3. Lease 与 Payload 精确绑定

`require_active_resolution_job_lease(job_id, attempt_id, token, expected_type)`：

1. `session_user='uap_worker'`；
2. `expected_type` 只能是 `resolve_claims` 或 `resolve_entities`；
3. `SELECT jobs ... FOR UPDATE`，使用 `clock_timestamp()`；
4. job 必须 running、类型匹配、token 匹配、lease 尚未过期；
5. attempt 必须属于 job、token 相同、outcome running、未 finished，并 `FOR UPDATE`；
6. 失败为 `42501`（角色/类型）或 `40001`（lease/attempt）；
7. 返回不可变 job payload，供同事务后续函数使用。

materialize 函数必须重新调用守卫，不相信 Python 已检查。

随后逐字段验证：

- jobs 列和 payload 的 schema 均为 `knowledge.v2`；
- payload 只含 ADR-0008 冻结键；
- analysis_result id/document/result type/model run/schema/result hash 与 payload 相同；
- `analysis_schema_version='ai.v1'`，validation valid；
- model run status succeeded，document/task/input hash 与 payload 相同；
- 先验证 result 数组长度；合法空数组允许 missing/ambiguous anchor 并直接形成零写入 receipt；非空结果才要求 extraction anchor status/id 满足 ADR-0010；
- result type 与 job type 映射一致；
- 禁止 JOIN `analysis_selections`。

任一不一致 fail closed：`knowledge_payload_mismatch`、`knowledge_schema_unsupported` 或明确 anchor error。

### 3.1 `knowledge-bundle.v2`

Python 不传 Claim 文本、Entity 名称或类型作为权威值；函数直接从 `analysis_results.result` 按 ordinal 读取。bundle 只描述映射决策：

```json
{
  "bundle_schema_version": "knowledge-bundle.v2",
  "analysis_result_id": "<uuid>",
  "analysis_result_sha256": "<64 hex>",
  "accepted_candidates": [
    {
      "ordinal": 0,
      "accepted_locators": [
        {
          "locator_ordinal": 0,
          "evidence_text": "<slice>",
          "char_start": 10,
          "char_end": 20,
          "page_start": null,
          "page_end": null,
          "time_start_ms": null,
          "time_end_ms": null
        }
      ],
      "rejected_locators": [
        {"locator_ordinal": 1, "reason_code": "locator_out_of_range"}
      ]
    }
  ],
  "rejected_candidates": [
    {
      "ordinal": 1,
      "reason_code": "knowledge_locator_unmappable",
      "rejected_locators": [
        {"locator_ordinal": 0, "reason_code": "locator_axis_conflict"}
      ]
    }
  ]
}
```

校验不变量：

1. source candidate 数组的每个零基 ordinal 恰好出现在 accepted 或 rejected 一侧；
2. accepted candidate 至少一个 accepted locator；其全部 source locator ordinal 恰好分区到 accepted/rejected；
3. rejected candidate 的全部 source locator 必须出现在 rejected_locators；
4. ordinal 不得重复、越界、遗漏或指向另一个 candidate；
5. source locator 从 analysis JSON 读取，函数构造 ADR-0010 envelope；Worker 不能替换 source locator；
6. page/time/char 强类型值必须与 source locator 和 locator type 相符；
7. 对 PDF/媒体，函数使用锚定 `extractions.location_map` 重新执行 ADR-0010 的 `C=A` 检查；不能只相信 Python 的页/时间映射；
8. evidence_text 最大 8192 UTF-8 bytes，不接受其它自由字段；正文边界和切片正确性由读取固定 hash 对象的映射库及运行态夹具验证，因为正文内容不在 PostgreSQL；
9. Claims 的 text/缺省值、Entities 的 name/type/candidate_payload 均由数据库 source JSON 派生；
10. Claim accepted locator 全部写 `support_type='supports'`；Entity 全部写 join 表并保存 source evidence ordinal；
11. valid 空数组仅允许所有列表为空；非空结果不能伪装成 empty。

任何覆盖不完整或内容不符为 `knowledge_bundle_mismatch`，不得静默丢候选或 evidence。

## 4. Span Hash 与数据库复核

span envelope 由 materialize 函数使用 payload 与 source locator 构造。hash 的数据库权威函数为：

```text
core.compute_evidence_locator_sha256(envelope jsonb)
```

它的冻结表达式为：

```sql
encode(sha256(convert_to(envelope::text, 'UTF8')), 'hex')
```

即使用 PostgreSQL 16 的 `jsonb` 文本表示、UTF-8 与内建 SHA-256，不依赖 `pgcrypto`；Python 不提供最终 hash。插入冲突时，函数还必须比较现有 span 的 envelope、extraction id、轴字段与 evidence_text；同 hash 不同内容以 `knowledge_locator_hash_conflict` 失败。未来若更换 PostgreSQL 主版本或序列化算法，必须提升 locator schema version，不得静默改变 v2 hash。

数据库原语依据：[PostgreSQL 16 Binary String Functions](https://www.postgresql.org/docs/16/functions-binarystring.html)。

Claim fingerprint 同样由数据库私有函数计算：先 `normalize(text, NFKC)`，再把连续 `[[:space:]]` 折叠为一个 U+0020、去除首尾 U+0020，最后对 UTF-8 使用内建 SHA-256；不 casefold。server encoding 必须为 UTF8。Python 可在单测中实现同算法，但不能把任意 fingerprint 传给 materialize。

Unicode 归一化依据：[PostgreSQL 16 String Functions](https://www.postgresql.org/docs/16/functions-string.html)。

## 5. `ops.finish_knowledge_job`

签名在现有 `finish_job` 参数上增加 `p_metrics jsonb`。函数：

1. 只允许 `uap_worker`；
2. 只允许 running `resolve_claims` / `resolve_entities`；
3. 复用 §3 的 job/attempt/token/`clock_timestamp()` 校验；
4. 验证 metrics；
5. succeeded 路径要求 payload 可完整验证，并按 payload.analysis_result_id 查询实际 Claim/Candidate 与 evidence link 数，核对 materialized 计数；terminal/retry 路径要求 materialized 计数为 0，payload 若仍可解析则额外确认本事务没有该 analysis 的新增行，若 payload 本身损坏则允许以 `knowledge_payload_mismatch` 收口；
6. `UPDATE ops.job_attempts SET metrics=p_metrics`；
7. 调用现有 `ops.finish_job` 完成状态转换；
8. 任一步失败则 metrics 和状态更新一起回滚；无内部 COMMIT。

Worker 仍无 `job_attempts` UPDATE 权；不得新增应用直写例外。

### 5.1 metrics Schema

只接受：

```json
{
  "schema_version": "knowledge-attempt-metrics.v1",
  "input_candidates": 3,
  "materialized_candidates": 1,
  "input_locators": 4,
  "materialized_locators": 1,
  "rejected_candidates": 2,
  "rejected_locators": 3,
  "empty_valid_result": false,
  "rejected_by_code": {"locator_out_of_range": 3},
  "samples": [
    {"candidate_ordinal": 1, "locator_ordinal": 0, "reason_code": "locator_out_of_range"}
  ]
}
```

约束：

- 未知键拒绝；所有计数为非负整数且内部算术一致；
- `samples` 最多 50 条，只能含 ordinal 和 ADR-0010 原因码；
- `rejected_by_code` 只含冻结原因码和计数；
- `pg_column_size(metrics) <= 65536`；
- 不得含正文、名称、claim、Prompt、响应、堆栈或任意自由文本；
- empty valid 必须 `input_candidates=materialized_candidates=0` 且 outcome succeeded；
- succeeded 必须“materialized_candidates>0 或 empty_valid_result=true”；
- 非空全拒绝必须 terminal failure、materialized 为 0。

## 6. 事务与 SAVEPOINT 算法

`claim_job` 在领取事务中提交。映射可在领域事务前读取固定 payload/对象，但真正写入按以下算法：

```text
BEGIN
  SAVEPOINT knowledge_materialize
  call materialize_*                 -- 内部重新校验 lease/payload/bundle
  call finish_knowledge_job          -- success 或全拒绝 terminal
COMMIT                               -- 领域行、metrics、attempt/job 同时可见
```

预期确定性 SQL RAISE（payload/bundle/约束）后，当前事务处于失败状态，必须：

```text
ROLLBACK TO SAVEPOINT knowledge_materialize
call finish_knowledge_job(terminal_failure, safe metrics/error)
COMMIT
```

禁止在未回滚 savepoint 的 aborted transaction 中调用 finish。

特殊路径：

- `40001` lease missing/expired：ROLLBACK 整个事务，不再 finish；由持有者或 WP4 lease-expiry 恢复闭环；
- 可重试基础设施错误：先确保领域事务整体回滚，再在新事务重新验证同一 lease，调用 `finish_knowledge_job(retryable_failure)`；若 lease 已失效则放弃，由 WP4 恢复；
- 未分类异常：绝不 succeeded；回滚后按冻结失败分类收口或等待 lease recovery。

不得在 materialize 已提交后另起事务 finish；也不得在另一事务把失败 attempt 标 succeeded。

## 7. 幂等与不可变性

- Claim：`(origin_analysis_result_id, ordinal)`；
- Entity Candidate：`(analysis_result_id, ordinal)`；
- Candidate evidence：`(entity_candidate_id, evidence_ordinal)`；
- Span：`(document_version_id, locator_sha256)`。

重放返回既有 ID，并逐字段比较不可变内容；不同内容使用稳定 conflict error，禁止 UPDATE 旧知识行。

## 8. 表权限

从 `uap_worker`、`uap_api` 收回 `INSERT/UPDATE/DELETE`：

```text
core.claims
core.claim_evidence
core.evidence_spans
core.entities
core.entity_candidates
core.entity_candidate_evidence
core.entity_aliases
core.entity_merge_events
core.relations
core.relation_evidence
core.tags
core.document_tags
core.entity_tags
core.claim_tags
```

保留任务所需最小 SELECT；Worker 获得 `core.analysis_results` SELECT，但继续无 `ops.model_runs` / `ops.prompt_versions` SELECT。input/model provenance 只能经受控函数从 job payload 和 definer 查询校验。

## 9. 后果与不采用

该设计让“无 lease 不写知识”“metrics 不由 Worker 裸改”“失败事务可闭环”都可由真实角色 SQL 探针证明。

不采用：

- Worker/API 保留知识 DML；
- materialize 函数信任客户端 claim/name/locator；
- SQL RAISE 后不回滚 savepoint就 finish；
- 在另一个事务提交领域行或标 succeeded；
- metrics 保存正文或无限拒绝明细；
- active principal 作为 merge 授权替代品。
