# G9 冻结验收用例

- 冻结编号：`G9-FROZEN-20260825-01`
- 父基线：`8550b8fe2d3322428fc9487e91aeb830425b0ed1`
- 用例：G9-01–G9-26
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

前置：存在 document_version、WP8 claim、以及已晋升的 entity。reviewer 分别打开三类 case。

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

无 evidence 创建手工 Claim；带一条 supports 的 span（同 document_version）创建。

预期：前者提交失败（ADR-0009 延迟约束）；后者成功，`origin`/`ordinal` NULL，fingerprint 由数据库计算。

## G9-24 subject 绑定与非 canonical 拒绝（WP9.6）

绑定 active canonical 实体成功。绑定 merged 实体失败 `review_subject_not_canonical`。不插入 `core.relations`。

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

预期：WP3–WP9 全绿；head 为 WP9.6 唯一 revision；0001→head 往返成功；表数与 WP9 文档声明一致。
