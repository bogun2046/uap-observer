# ADR-0019：手工 Claim、证据与 Subject 绑定

- 状态：Proposed for `G9-FROZEN-20260825-04`
- 日期：2026-08-25
- 前置：ADR-0009、0010 `core.require_ai_claim_supports`、0002 `core.claims`、ADR-0014、ADR-0015、ADR-0017

## 1. 背景

0002：`origin_analysis_result_id` 与 `ordinal` 同为 NULL 或同非空。0010 另要求 `(origin IS NULL) = (created_by IS NOT NULL)`。AI Claim 由 WP8 物化。`core.require_ai_claim_supports` **在 origin IS NULL 时直接返回**，因此手工 Claim 目前没有任何“至少一条 supports”延迟约束。ADR-0009 把手工规则留给 WP9，并禁止为删最后一条 evidence 而削弱 AI 不变量。

`claims.subject_entity_id` 在 WP8.3/8.4 保持 NULL。

## 2. 决策：AI 与手工证据规则分离

**不修改** ADR-0009 与 `core.require_ai_claim_supports`：origin 非空的 AI Claim 在任意决定之后仍必须至少一条 `supports`。reject/withdraw AI Claim **不得**删除其 evidence。

WP9.6 新增 `core.require_manual_claim_supports`（DEFERRABLE INITIALLY DEFERRED），仅当 `origin_analysis_result_id IS NULL`：

1. 创建事务结束时必须 ≥1 条 `supports`（G9-23 验证的是本约束，不是 ADR-0009）。
2. 之后若该 claim **没有** status 为 `rejected` 或 `withdrawn` 的 review case，仍必须 ≥1 条 `supports`。
3. 仅当存在同 claim、`closed_at IS NULL` 或刚在本事务更新为 `rejected`/`withdrawn` 的 case，且本事务已插入对应 `reject`/`withdraw` decision 时，才允许 supports 降为 0。
4. 稳定错误：`23514` / `manual_claim_requires_supports`。

## 3. 决策：手工 Claim 创建

`audit.create_manual_claim(p_document_version_id, p_claim_text, p_claim_type, p_assertion_status, p_attribution, p_span_ids uuid[])` 要求 `reviewer`，在单一事务：

1. 插入 `core.claims`：origin 与 ordinal 均为 NULL，`created_by`=GUC principal；
2. `claim_fingerprint = core.compute_claim_fingerprint(claim_text)`，Python 不传入指纹；
3. 为每个 `p_span_ids` 插入 `claim_evidence`；至少一条 `supports`；span 必须属于 `p_document_version_id`；
4. 提交时满足 **§2 新手工约束**，不依赖 AI 触发器；
5. 不写 `analysis_results`，不入队 resolve job。

同一 principal 随后不能 `record_review_decision` 该 claim（ADR-0015 自审隔离）。

## 4. 决策：证据与 subject 变更只经 `record_review_decision`

删除公开的 `bind_claim_subject_entity`、`replace_claim_evidence`、`retire_claim_evidence_via_decision`。登录角色对 `core.claims.subject_entity_id` 与 `core.claim_evidence` 仍无 DML。

`record_review_decision` 插入 decision 后，仅在同一函数内调用：

```text
audit._apply_claim_subject_bind(p_case_id, p_decision_id, p_entity_id)
audit._replace_claim_evidence(p_case_id, p_decision_id, p_span_ids)
audit._retire_manual_claim_supports(p_case_id, p_decision_id)
```

三者均为 owner `SECURITY DEFINER`，`REVOKE ALL FROM PUBLIC`，**无登录角色 EXECUTE**。每个函数必须证明：

1. `p_decision_id` 属于 `p_case_id`；
2. 该 decision 行由本事务插入：`xmin` 对应 `pg_current_xact_id()`（或 PostgreSQL 16 等价 xid8 转换）。已提交的 decision id 传入必须失败；契约以 G9-32 为准，而不是调用方自觉。
3. case 为 `claim`，subject 为该 claim；
4. decision 类型匹配 ADR-0015 允许键。

`_apply_claim_subject_bind`：实体 active 且 canonical；`approve` 仅当当前 `subject_entity_id IS NULL`；`revise` 允许改绑。merged 目标 → `22023` / `review_subject_not_canonical`。不写 `core.relations`。

`_replace_claim_evidence`：仅 `revise`；本事务结束后 ≥1 条 supports（AI 与手工均适用）。

`_retire_manual_claim_supports`：仅 `reject`/`withdraw` **且** origin IS NULL。AI Claim 调用该函数 → `22023` / `review_ai_evidence_immutable`。

跨事务绕过：提交后再以 `uap_api` 调用上述私有函数必须 `42501`；裸 `UPDATE core.claims SET subject_entity_id` 或 `DELETE claim_evidence` 必须 `42501`。

## 5. 应用服务与 HTTP

WP9.6 交付 `uap_platform/review/` 应用服务。公开方法只映射有登录 EXECUTE 的函数；私有 `_apply_*` 不得出现在 Python 直接 SQL 调用中。不引入 FastAPI。公开 `/v1/*` 仍属 WP10。

## 6. 不做

- relation 手工创建成功路径；
- 修改 0002 Claim CHECK 或 `core.require_ai_claim_supports` / ADR-0009 AI 语义；
- 向 `uap_api` 授予 claims/evidence 裸 DML；
- 让 Worker 创建手工 Claim。
