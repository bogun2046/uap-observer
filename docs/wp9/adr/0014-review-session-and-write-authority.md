# ADR-0014：审核会话绑定与写入权威

- 状态：Proposed for `G9-FROZEN-20260825-02`
- 日期：2026-08-25
- 前置：WP1 permissions、ADR-0011、ADR-0012、G8 已签署

## 1. 背景

WP8 证明知识写入只能经 `SECURITY DEFINER` 函数，且 `core.merge_entities` 对登录角色默认关闭。WP1 规定审核由人员 OIDC 主体执行，Worker/API 数据库角色只是进程凭据。active `audit.principals` 行只证明身份存在，不证明 `reviewer` / `senior_reviewer` 授权。

若把 `p_actor_id` 当函数参数传入，调用方可以伪造审核人。若把 EXECUTE 直接授给 `uap_api` 去裸写 `audit.review_*`，则 RBAC 只存在于应用层。

## 2. 决策

### 2.1 事务内会话 GUC

每个审核事务开始时，API 连接必须：

```sql
SELECT set_config('uap.principal_id', '<uuid>', true);
SELECT set_config('uap.request_id', '<uuid>', true);
```

`true` 表示 `SET LOCAL`：只在当前事务有效。函数读取：

```sql
nullif(current_setting('uap.principal_id', true), '')::uuid
```

缺失、空串、非 UUID → `42501` / `review_principal_missing`。

写函数（ADR-0015–0019 列出的所有 INSERT/UPDATE 入口）还要求 `uap.request_id` 为 UUID；缺失、空串、非 UUID → `42501` / `review_request_id_missing`。`require_active_role` 只读，不要求 request_id。request_id 不得从函数参数补写。

禁止：

- 用函数参数覆盖 GUC 中的 acting principal；
- 把 GUC 设为服务主体后执行审核；
- 在连接级 `SET` 而非 `SET LOCAL` 作为唯一绑定（探针和应用均必须 `SET LOCAL`）。

### 2.2 `audit.require_active_role(p_role audit.application_role)`

`SECURITY DEFINER`，固定 `search_path = audit, pg_catalog`，`REVOKE ALL FROM PUBLIC`。

检查顺序：

1. `session_user = 'uap_api'`。其它登录角色 `42501` / `review_session_role_denied`。
2. GUC principal 存在、`active`、`principal_type='person'`。服务主体 `42501` / `review_service_principal_denied`。
3. 存在 `role_bindings`：`principal_id` 匹配、`revoked_at IS NULL`、`role` 为 `p_role` 或在蕴含规则下满足。
4. `scope_type='global'` 且 `scope_id IS NULL` 视为全局授权。本阶段不实现文档级 scope；非 global 绑定 `42501` / `review_scope_unsupported`。
5. 返回 principal id，供后续写入 `opened_by` / `decided_by` / `created_by`。

蕴含：

| 要求角色 | 满足角色 |
|---|---|
| `reviewer` | `reviewer` 或 `senior_reviewer` |
| `senior_reviewer` | 仅 `senior_reviewer` |

`data_operator`、`model_manager`、`platform_admin` 不蕴含审核权。`platform_admin` 不能绕过本函数。

### 2.3 写入权威

新增审核函数均：

- owner 持有，`SECURITY DEFINER`；
- `REVOKE ALL FROM PUBLIC`；
- 检查 `session_user` 而非 `current_user`；
- 内部先调用 `require_active_role`；
- 不向 `uap_worker`、`uap_scheduler`、`uap_publisher`、`uap_model_governance`、`uap_public_reader` 授予 EXECUTE。

`uap_api` 只获得本 ADR 及后续 ADR 列出的最小函数 EXECUTE，无 `audit.review_cases` / `review_decisions` / publication grant 表的 INSERT/UPDATE/DELETE。

`audit.audit_events` 继续只允许 INSERT。审核函数内部追加事件。`event_key` 生成规则与冲突行为见 §2.4。

### 2.4 写函数幂等

每个写函数在成功路径插入恰好一条 `audit.audit_events`，`event_key` 冻结如下。`audit.audit_events.event_key` 已 UNIQUE。

| 函数 | event_key |
|---|---|
| `open_review_case` | `review.case.open:{case_type}:{subject_id}:{request_id}` |
| `assign_review_case` | `review.case.assign:{case_id}:{assignee_id}:{request_id}` |
| `close_review_case` | `review.case.close:{case_id}:{request_id}` |
| `record_review_decision` | `review.decision:{case_id}:{request_id}` |
| `select_analysis_result` | `review.selection:{document_version_id}:{result_type}:{request_id}` |
| `accept_entity_candidate` | `review.candidate.accept:{candidate_id}:{request_id}` |
| `bind_entity_candidate` | `review.candidate.bind:{candidate_id}:{entity_id}:{request_id}` |
| `apply_entity_merge` | `review.entity.merge:{source_id}:{target_id}:{request_id}` |
| `apply_entity_merge_reverse` | `review.entity.merge_reverse:{merge_event_id}:{request_id}` |
| `create_manual_claim` | `review.claim.manual:{fingerprint}:{document_version_id}:{request_id}` |

`metadata` 必须含 `payload_sha256`：对函数权威输入（不含时钟）的 canonical JSON SHA-256。

重复调用：

1. 已存在相同 `event_key` 且 `payload_sha256` 相同 → 不插新业务行，返回首次成功的 id（case/decision/claim/entity/event）；grant 与 outbox 也不再插。
2. 已存在相同 `event_key` 且 `payload_sha256` 不同 → `23505` / `review_idempotency_payload_conflict`；不覆盖旧决定或旧 grant。
3. 不同 `request_id` 视为新请求，走自然唯一约束（例如仍 open 的 subject → `review_case_already_open`）。

私有 `_apply_*` 函数不暴露独立幂等键；它们只由 `record_review_decision` 在同一次成功路径调用，复用该 decision 的 `event_key`。

Outbox `event_key` 仍按 ADR-0016，以 grant/decision id 为自然键；decision 幂等重放不得产生第二条 outbox。

## 3. 代码布局

跟随 WP5–WP8，新增 `platform/src/uap_platform/review/`。本阶段不重构完整 `domains/` 树，不引入 FastAPI。Python 只做：设置 GUC、调用函数、把稳定错误码映射为异常。权威状态在数据库。

## 4. 不做

- 新登录角色 `uap_reviewer`；
- 把 `core.merge_entities` EXECUTE 授给 `uap_api`；
- OIDC 验票实现（WP9.6 应用服务假定调用方已完成鉴权并设置 GUC）；
- 修改 ADR-0011 已冻结的知识函数权限矩阵，除非后续 ADR-0018 增加包装函数。
