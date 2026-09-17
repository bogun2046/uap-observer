# WP10 权限与调用边界

## 1. 0019 事实基线

登录数据库角色仍是：`uap_migrator`、`uap_api`、`uap_worker`、`uap_scheduler`、`uap_publisher`、`uap_model_governance`、`uap_public_reader`、`uap_audit_reader`、`uap_backup`；`uap_owner` 为 NOLOGIN。

WP9 已把知识表 DML、analysis selection DML 和 review/grant DML 收到 owner 函数，但 0003 的历史宽授权使 `uap_api` 对部分 `core`/`ops` 表仍可能保留裸 DML，`uap_publisher` 对 `public` 仍有裸 DML。WP10.1 必须向前收紧，不能依赖应用约定。

## 2. WP10 最终角色矩阵

| 角色 | 允许 | 明确拒绝 |
|---|---|---|
| `uap_api` | 内部受控 SELECT；冻结 `audit.*` 公共包装函数 EXECUTE | 所有 `core`/`ops`/review/grant/public 裸 DML；私有函数；底层 merge/reverse；Publisher 函数 |
| `uap_publisher` | 必要内部 SELECT；专用 publication claim/apply/fail EXECUTE | 所有 public 裸 DML；review/knowledge/merge 函数；通用 publication ack |
| `uap_public_reader` | `public` 当前投影 SELECT | ingest/core/ops/audit USAGE/SELECT；所有写函数；对象存储 |
| `uap_worker` | 已冻结普通任务/知识函数 | public 全权限；publication/review/merge；relation success handler |
| `uap_scheduler` | 已冻结调度/补偿函数 | public DML；review；publication apply；relation materialize |
| `uap_model_governance` | 已冻结 model governance 表/函数 | review/public/publisher/knowledge写 |
| `uap_audit_reader` | 全 schema SELECT | 全部写 |
| `uap_backup` | 默认只读全 schema | 全部写 |
| `uap_migrator` | 部署窗口 `SET ROLE uap_owner` | 常驻应用使用 |

`ALTER DEFAULT PRIVILEGES` 必须保证未来 `core`/`ops`/`audit` 表不会把 DML 自动授予 `uap_api`，未来 `public` 表不会把 DML自动授予 `uap_publisher`。新表显式授权优先于 `ALL TABLES`。

## 3. 公共函数目录

### 既有 WP9，保留 `uap_api` EXECUTE

| 函数 | 应用角色 |
|---|---|
| `audit.require_active_role(audit.application_role)` | 只读会话检查 |
| `audit.open_review_case(audit.review_case_type,uuid,smallint,text)` | reviewer/senior |
| `audit.assign_review_case(uuid,uuid)` | reviewer/senior |
| `audit.close_review_case(uuid,text)` | reviewer/senior |
| `audit.record_review_decision(uuid,audit.review_decision,text,jsonb)` | reviewer；withdraw 为 senior |
| `audit.select_analysis_result(uuid,text)` | reviewer/senior；只支持 claim/entity extraction |
| `audit.accept_entity_candidate(uuid,text)` | reviewer/senior |
| `audit.bind_entity_candidate(uuid,uuid,text)` | reviewer/senior |
| `audit.apply_entity_merge(uuid,uuid,text)` | senior |
| `audit.apply_entity_merge_reverse(uuid,text)` | senior |
| `audit.create_manual_claim(uuid,text,core.claim_type,core.assertion_status,text,uuid[])` | reviewer/senior |

WP10 更新同一 `record_review_decision` 签名的 publication manifest 语义，不建立绕过 WP9 决定链的独立 grant 写函数。

### WP10 新公共函数

| 函数签名 | EXECUTE | 作用 |
|---|---|---|
| `ops.claim_publication_outbox(text,integer,integer)` | `uap_publisher` | 只领非 terminal 的 v2 publication event |
| `ops.apply_publication_event(uuid,uuid)` | `uap_publisher` | 有效 lease 下原子 projection+ack |
| `ops.fail_publication_event(uuid,uuid,text,text,integer,boolean)` | `uap_publisher` | retry/terminal 失败收口 |
| `audit.requeue_publication_event(uuid,text)` | `uap_api` | `data_operator` 受控重放，带 request_id 幂等 |
| `ops.rebuild_public_projection(uuid)` | 无常驻登录角色；仅 migrator/owner 的显式维护窗 | 从 active v2 manifests 重建并出报告 |

上述函数均由 `uap_owner` 持有、`SECURITY DEFINER`、固定可信 `search_path`、schema-qualified SQL、`REVOKE ALL FROM PUBLIC`，并检查 `session_user`。私有 manifest capture/hash/identity helper 不向任何登录角色授权。

WP10.2 实际只开放前三级专用函数（claim/apply/fail）给 `uap_publisher`；
`audit.requeue_publication_event` 与 `ops.rebuild_public_projection` 属于后续阶段，
本迁移不得创建或授权。Publisher loop 不调用 generic `ops.ack_outbox`。

W01–W11 是全部外部主体写操作，均以 `uap.request_id`、operation event key 和 canonical payload hash 幂等，完整六项负例见 acceptance-cases。Publisher claim/apply/fail 是内部 lease 协议，以 event/attempt/token 作为 operation key；rebuild 是维护命令，以 rebuild UUID 作为 command key。两类非 HTTP 函数的重放、冲突、越权、坏 token/参数和回滚规则见 projection-contract，不得被误当成不需要幂等证明的“内部实现”。

## 4. 永久拒绝项

以下函数对 `uap_api`、Worker、Publisher、Scheduler、model governance、public reader 永远不得授予 EXECUTE：

- `core.merge_entities(uuid,uuid,uuid,text)`；
- `core.reverse_entity_merge(uuid,uuid,text)`；
- `core.materialize_claim_bundle(...)`、`core.materialize_entity_bundle(...)`；
- 所有 `audit._*`、`ops.enqueue_publication_outbox(...)` 私有入口；
- future relation materializer（WP10 不得存在）。

`uap_publisher` 对 `ops.ack_outbox` 的 publication 用法必须撤销或在函数内拒绝 publication event；否则可绕过投影直接 ack。WP4 的 generic Outbox 数据和函数保留，但 WP10 Publisher 生产 loop 只走专用函数。

## 5. HTTP 双进程

| 进程 | 唯一数据库凭据 | 路由 |
|---|---|---|
| Public API | `uap_public_reader` | `/healthz`、`/v1/*` 只读 |
| Admin API | `uap_api` | `/healthz`、`/admin/v1/*` |
| Publisher | `uap_publisher` | 无业务 HTTP；只消费 Outbox |

不得把两个连接池放入同一请求依赖容器，也不得让 Public API 环境拥有 Admin/Publisher URL。Admin handler 只调用应用服务；Publisher 不接受用户 principal GUC。

## 6. OIDC 与应用角色

- 只接受 `Authorization: Bearer`；WP10 不接受认证 cookie，因而不存在 cookie CSRF 旁路。
- token 必须通过 alg allowlist、签名、issuer、audience、expiry、not-before、subject 校验。
- `(issuer,subject)` 必须命中 existing active `audit.principals` person；API 不自动创建 principal/role binding。
- 数据库 `audit.role_bindings` 是最终授权；token scope/claim 不能替代。
- `senior_reviewer` 只蕴含 reviewer；其它应用角色互不蕴含。

## 7. Admin API 授权矩阵

| 能力 | reviewer | senior_reviewer | data_operator |
|---|---:|---:|---:|
| review/candidate/analysis/evidence/entity 读 | R | R | — |
| open/assign/close case | W | W | — |
| approve/reject/dispute/revise | W | W | — |
| withdraw | — | W | — |
| selection/candidate accept/bind/manual claim | W | W | — |
| merge/reverse | — | W | — |
| publication dead-event 读/重放 | — | R | R/W |

`model_manager`、`security_admin`、`platform_admin`、`audit_reader` 的通用管理 HTTP 本阶段 closed；它们不因数据库角色或 token claim 获得上述写能力。

Admin read handler 同样必须开启只读事务、`SET LOCAL uap.principal_id`，并调用 `audit.require_active_role(...)`；不得因为底层 `uap_api` 可 SELECT 而绕过应用角色。publication dead-event 列表只允许 `senior_reviewer` 或 `data_operator`，普通 reviewer 不可见。Public read handler 不设置 principal GUC，只能使用 `uap_public_reader` 读取当前投影。
