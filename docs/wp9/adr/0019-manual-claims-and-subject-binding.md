# ADR-0019：手工 Claim、证据与 Subject 绑定

- 状态：Proposed for `G9-FROZEN-20260825-01`
- 日期：2026-08-25
- 前置：ADR-0009、0002 `core.claims`、ADR-0014、ADR-0015、ADR-0017

## 1. 背景

0002：`origin_analysis_result_id` 与 `ordinal` 同为 NULL 或同非空。AI Claim 由 WP8 物化，带 origin 与 fingerprint。ADR-0009 把手工 Claim 的 evidence 规则留给 WP9，并禁止绕过延迟约束删除最后一条 evidence。

`claims.subject_entity_id` 在 WP8.3/8.4 保持 NULL。

## 2. 决策：手工 Claim 创建

`audit.create_manual_claim(...)` 要求 `reviewer`，在单一事务：

1. 插入 `core.claims`：`origin_analysis_result_id` 与 `ordinal` 均为 NULL，`created_by`=GUC principal；
2. `claim_fingerprint = core.compute_claim_fingerprint(claim_text)`，Python 不传入指纹；
3. 至少插入一条 `claim_evidence`，`support_type='supports'`，span 必须属于调用方提供的 `document_version_id`；
4. 提交时满足 ADR-0009 延迟约束；
5. 不写 `analysis_results`，不入队 resolve job。

同一 principal 随后不能 `record_review_decision` 该 claim（ADR-0015 自审隔离）。其它 reviewer / senior_reviewer 可以审核。

## 3. 决策：最后一条 evidence

禁止对仍需 evidence 的 claim 直接 DELETE 最后一条 `claim_evidence`。新增：

`audit.replace_claim_evidence(p_claim_id, p_span_ids, p_reason)`  
仅当存在未关闭 claim case，且本事务将留下 ≥1 条 `supports` evidence。

`audit.retire_claim_evidence_via_decision(...)`  
只有在 `reject` 或 `withdraw` 决定的同一事务中，才允许使 claim 不再满足“至少一条 supports”。实现必须使用 SECURITY DEFINER 并在约束触发器允许的路径上操作；禁止 `uap_api` 裸 DELETE。

AI origin claim 同样适用：不能为了“清理探针作业”删 evidence。

## 4. 决策：subject_entity_id 绑定

`audit.bind_claim_subject_entity(p_claim_id, p_entity_id, p_reason)`：

1. `require_active_role('reviewer')`；
2. 实体必须 active 且为 canonical；
3. 允许从 NULL 绑定，或在未关闭 claim case 的 `revise` 决定中改绑；
4. 绑定 merged 实体 `22023` / `review_subject_not_canonical`；
5. 不把绑定当作 relation 行，不创建 `core.relations`。

## 5. 应用服务与 HTTP

WP9.6 交付 `uap_platform/review/` 应用服务，方法与上述函数 1:1。不引入 FastAPI 依赖。不实现生产 OIDC。Admin HTTP 适配器若出现，只能是对同一服务的薄封装，且不得直接 SQL 写审核表。公开 `/v1/*` 仍属 WP10。

## 6. 不做

- relation 手工创建成功路径；
- 修改 0002 Claim CHECK 或 ADR-0009 AI evidence 触发器语义；
- 让 Worker 创建手工 Claim。
