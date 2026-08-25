# G9 冻结验收用例

- 冻结编号：`G9-FROZEN-20260825-04`
- 父基线：`8550b8fe2d3322428fc9487e91aeb830425b0ed1`
- 用例：G9-01–G9-38
- 维度：正向、反向、权限、幂等、状态机与可追溯性

所有角色拒绝项使用真实登录连接。所有“行数为 0”指事务结束后的可见状态。

## G9-01 reviewer 会话绑定成功（WP9.1）

`uap_api` 连接 `SET LOCAL uap.principal_id` 为 active person，且存在未撤销 global `reviewer` 绑定。调用 `audit.require_active_role('reviewer')`。

预期：返回该 principal id；无表写入。

## G9-02 缺失/无效 principal fail-closed（WP9.1）

分别：不设 GUC、空串、非 UUID、inactive principal、不存在的 UUID。

预期：`42501` / `review_principal_missing` 或等价稳定码；无审核表写入。

## G9-03 服务主体不能审核（WP9.1）

GUC 指向 `principal_type='service'`。

预期：`42501` / `review_service_principal_denied`。

## G9-04 Worker 与无绑定拒绝（WP9.1）

`uap_worker` 调用 `require_active_role`；`uap_api` 下 person 无 reviewer/senior_reviewer 绑定。

预期：均为 `42501`；分别为 `review_session_role_denied` 与 `review_role_denied`。

## G9-05 senior_reviewer 蕴含 reviewer（WP9.1）

仅有 `senior_reviewer` 绑定的 person 调用 `require_active_role('reviewer')` 成功；调用 `require_active_role('senior_reviewer')` 成功。仅 `reviewer` 绑定者调用 senior 失败。`platform_admin` 无审核绑定者失败。

## G9-06 打开 document/claim/entity case（WP9.2）

前置：存在 document_version、WP8 claim、以及已晋升的 entity。reviewer 设置 `uap.request_id` 后分别打开三类 case。

预期：各一行 `status='open'`，`opened_by` 为 GUC principal；audit event 存在。

## G9-07 同一 subject 不能两个未关闭 case（WP9.2）

对同一 claim 再 open。

预期：`23505` / `review_case_already_open`；仍一行未关闭 case。

## G9-08 relation case 拒绝（WP9.2）

`open_review_case('relation', ...)`，无论 subject 是否存在。

预期：`22023` / `knowledge_relation_review_not_in_wp9`；`audit.review_cases` 无 relation 行。

## G9-09 指派与非法关闭（WP9.2）

open 后 assign 给另一 reviewer。无决定时 close。

预期：assign 后 `status='assigned'`；close 为 `22023` / `review_case_not_decidable`。

## G9-10 自审隔离（WP9.3）

reviewer A 作为 `created_by` 的 claim（WP9.3 允许 owner 夹具插入 origin/ordinal 均为 NULL 的手工行；WP9.6 必须改用 `create_manual_claim`）打开 case 后自己 approve。

预期：`42501` / `review_self_review_denied`；无 decision、无 grant。reviewer B 可以 reject/approve。

## G9-11 approve 产生 grant 与 outbox（WP9.3）

对 claim case approve。

预期：decision 追加；`claim_publication_grants` 一行 active；`ops.outbox_events.event_type='publication.granted'`；`ops.jobs` 无新 `publish_*`；`public.claims` 行数为 0。

## G9-12 reject 不产生 grant（WP9.3）

reject。

预期：case `rejected`；grant 0；对应 granted outbox 0。

## G9-13 senior_reviewer 撤回（WP9.3）

已 approve 的 grant 上，senior_reviewer withdraw。

预期：grant `withdrawn`；outbox `publication.withdrawn`；case `withdrawn`。

## G9-14 reviewer 不能撤回（WP9.3）

仅 reviewer 绑定者 withdraw。

预期：`42501` / `review_role_denied`；grant 仍 active。

## G9-15 grant 必须绑定同 case 的 approve/revise（WP9.3）

尝试用 reject decision 或另一 case 的 approve 手工插 grant（owner 夹具）。

预期：触发器 `23514`；与 0003 语义一致。

## G9-16 API 不能写 public 或 Outbox 裸 DML（WP9.3）

`uap_api` 对 `public.documents` INSERT、对 `ops.outbox_events` INSERT。

预期：`42501`。决定函数成功路径仍只经 SECURITY DEFINER 写 outbox。

## G9-17 选择 valid 分析并取代当前行（WP9.4）

两条 valid claim_extraction。先选 A 再选 B。

预期：仅 B 的 `superseded_at IS NULL`；WP8 resolve jobs 数量不变；无新 materialize。

## G9-18 不能选择 invalid（WP9.4）

选 invalid 或 summary。

预期：失败，无当前 selection 变化。

## G9-19 candidate 晋升不自动合并（WP9.4）

库中已有同名 active 实体。accept 一个 pending candidate。

预期：新 `core.entities` 行；candidate `resolved` 指向新行；不调用 merge；同名两行均 active。

## G9-20 senior_reviewer 包装 merge 成功（WP9.5）

两个 active canonical 实体。senior_reviewer 调 `audit.apply_entity_merge`。

预期：与 G8-17 状态机相同；audit event 存在。

## G9-21 reviewer 与核心 EXECUTE 仍关闭（WP9.5）

reviewer 调包装函数。`uap_api` 直接 `SELECT core.merge_entities(...)`。

预期：前者 `review_role_denied`；后者 `42501`（函数无 EXECUTE）。G8-19 保持。

## G9-22 reverse 同样要求 senior_reviewer（WP9.5）

reviewer 调 `apply_entity_merge_reverse` 失败；senior_reviewer 成功，source 恢复 active。

## G9-23 手工 Claim 必须带 supports evidence（WP9.6）

无 evidence 调用 `create_manual_claim`；带一条 supports 的 span（同 document_version）创建。

预期：前者提交失败，SQLSTATE `23514` / `manual_claim_requires_supports`（**新** `core.require_manual_claim_supports`，不是 `require_ai_claim_supports`）；后者成功，`origin`/`ordinal` NULL，fingerprint 由数据库计算。

## G9-24 subject 绑定只经 decide 事务（WP9.6）

claim case 上 `approve` 且 `structured_changes={"bind_subject_entity_id": "<canonical>"}`。再对 merged 实体 `revise` 改绑。

预期：第一次 `subject_entity_id` 写入成功。第二次 `22023` / `review_subject_not_canonical`。不插入 `core.relations`。不存在公开 `bind_claim_subject_entity`。

## G9-25 G8-16C 保持 fail-closed（WP9.6）

复跑 WP8.6 relation 探针。

预期：`dead` / `terminal_failure` / `knowledge_relation_task_not_in_wp8`。WP9 不得将其改为成功。

## G9-26 编排与迁移链（WP9.6）

全新 PG16/MinIO：

```text
python tools/wp8_runtime_probe.py --from wp3
python tools/wp9_runtime_probe.py
./scripts/verify-migration-chain.sh
```

预期：WP3–WP9 全绿；head 为 WP9.6 唯一 revision；0001→head 往返成功；表数与 WP9 文档声明一致；superseded grant 存在时 0016 downgrade 失败。

## G9-27 revise 替换 grant 且旧行可与新行共存（WP9.3）

对已 approve 的 claim grant 再 `revise`。

预期：旧 grant `grant_status='superseded'` 且 `withdrawn_at IS NULL`；新 grant `active`、`revision_no` + 1；两行 `withdrawn_at` 均为 NULL；`uq_*_grant_live` 不冲突；outbox 含 `publication.superseded` 与新 `publication.granted`；`public.*` 仍为 0。

## G9-28 并发 revise 串行双成功且无丢失更新（WP9.3）

两连接使用 **不同** `request_id`，在默认 `READ COMMITTED` 下同时对同一已 approve 的 claim case 调用 `revise`。

预期：

- 两个事务最终都可以 `COMMIT`（后到者等待 case 行锁后继续，不要求 `40001`/`23505`）；
- 两条 `revise` decision，`sequence_no` 为连续值；
- grant `revision_no` 严格递增（approve=1 则两次 revise 为 2 与 3）；
- 提交后恰好一行 `grant_status='active'`，其余为 `superseded` 且 `withdrawn_at IS NULL`；
- 后到事务 supersede 的是前一事务新插入的 active grant，而不是锁前快照中的 approve grant；
- `public.*` 仍为 0。

## G9-29 相同 request_id 重放不产生第二份决定（WP9.3）

对同一 case 用相同 `uap.request_id` 与相同 `p_decision`/`p_reason`/`p_structured_changes` 连续两次 `record_review_decision`。`event_key` 必须为 `review.decision:{request_id}`，不含 `case_id`。

预期：只一条 decision、一条 active grant、一条 granted outbox、一条 `audit_events` 对应该 event_key；第二次返回首次 id。

## G9-30 相同 request_id 不同 payload 冲突（WP9.3）

同一 `review.decision:{request_id}` 下分别：只改 `p_reason`；改 `p_case_id`；改 `p_decision` 或 `structured_changes`。

预期：均为 `23505` / `review_idempotency_payload_conflict`；原 decision/grant 不变；不得因改 case_id 而换出新 event_key。

## G9-31 写函数缺少 request_id（WP9.2）

已设 principal，不设 `uap.request_id`，调用 `open_review_case`。

预期：`42501` / `review_request_id_missing`；无 case 行。

## G9-32 跨事务不能改绑或撤回 evidence（WP9.6）

approve 已提交后，`uap_api` 分别：调用 `_apply_claim_subject_bind`、`_retire_manual_claim_supports`、`UPDATE core.claims SET subject_entity_id`、`DELETE FROM core.claim_evidence`。

预期：全部 `42501`。subject 与 evidence 保持提交时状态。

## G9-33 AI Claim reject 不得丢掉最后一条 supports（WP9.6）

对 WP8 AI Claim `reject`，并尝试 `structured_changes={"retire_supporting_evidence": true}`。另用 owner 夹具直接删其最后一条 supports。无该键的纯 `reject` 作为对照。

预期：带 retire 键的调用 `22023` / `review_ai_evidence_immutable`，无新 decision，evidence 仍 ≥1。owner 删除提交失败 `23514`（`require_ai_claim_supports`）。纯 reject 使 case `rejected` 且 evidence 保留。

## G9-34 open/assign 幂等重放与冲突（WP9.2）

`event_key` 为 `review.case.open:{request_id}` 与 `review.case.assign:{request_id}`。分别：

1. 相同 `request_id` + 相同权威输入重放；
2. 只改非身份字段：open 只改 `reason`（assign 无 reason，此步改为只改 `assignee_id` 的对照见第 3 步）；
3. 改旧 event_key 曾包含的业务参数：open 改 `subject_id` 或 `case_type`；assign 改 `case_id` 与 `assignee_id` 各一次。

预期：(1) 返回首次对象，不产生第二行 case、不改 `assigned_to`。(2)(3) 均为 `23505` / `review_idempotency_payload_conflict`，不得创建第二份业务状态，也不得因改 subject/assignee/case 生成不同 event_key。`require_active_role` 不在本用例内。不同 operation 复用同一 `request_id`（先 open 再 assign）必须被允许。

## G9-35 close 幂等重放与冲突（WP9.3）

`event_key` 为 `review.case.close:{request_id}`。case 已 `approved` 后：

1. 相同 `request_id` 两次 close；
2. 只改 `p_reason`；
3. 改 `p_case_id`（旧键曾含 case_id）。

预期：(1) 重放不更新 `closed_at`、不插第二 audit event。(2)(3) `review_idempotency_payload_conflict`，无第二份 close。不同 `request_id` 对已 close case 失败，稳定码 `review_case_already_closed`。

## G9-36 selection/candidate 写函数幂等（WP9.4）

对 `select_analysis_result`、`accept_entity_candidate`、`bind_entity_candidate` 分别做：重放；只改 `reason`；改旧键业务参数（`analysis_result_id` / `candidate_id` / `entity_id`）。

预期：重放不产生第二当前 selection、不插入第二实体、不改已 resolved candidate。只改 reason 与改业务 id 均为 payload conflict，不能创建第二份业务状态。`accept` 重放不得因同名再插 `core.entities`。

## G9-37 merge/reverse 幂等（WP9.5）

对 `apply_entity_merge` 与 `apply_entity_merge_reverse` 分别做：重放；只改 `reason`；改旧键业务参数（`source_entity_id`/`target_entity_id` 或 `merge_event_id`）。

预期：重放返回首次 merge event id，不产生第二开边、不二次 reverse。只改 reason 与改实体/event id 均为 payload conflict，图不变。核心 `core.merge_entities` 仍无登录 EXECUTE。

## G9-38 手工 Claim 幂等（WP9.6）

`event_key` 为 `review.claim.manual:{request_id}`。分别：

1. 相同权威输入重放；
2. 只改 `attribution`（本函数无 reason）；
3. 改旧键曾包含的业务参数：`document_version_id`、`claim_text`（从而 `fingerprint`）、`span_ids`。

预期：(1) 返回首次 claim id，不插第二 claim/evidence。(2)(3) 均为 `review_idempotency_payload_conflict`，不写新 claim。无 evidence 的失败路径仍走 G9-23，不产生 event_key。


