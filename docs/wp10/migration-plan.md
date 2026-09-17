# WP10 migration 与实施顺序

当前权威 head：`0019_manual_claims_binding`（文件 `0019_manual_claims_and_subject_binding.py`）。不得改写 0001–0019；WP10 只追加单一线性 head。

## 1. 0020 `wp10_publication_contract`（WP10.1）

顺序：

1. preflight 当前 grants/outbox/public 行数与权限，不改数据；
2. 新增 `public.document_category`、`public.fact_status` enum，并把 `public.documents` 两列安全转换；非空未知值阻断 `publication_manifest_invalid`；
3. 新增 document/claim/entity manifest、manifest evidence、public identity、quarantine 表及复合 FK/CHECK/UNIQUE；
4. `ops.outbox_events` 新增 `terminal_at`、`terminal_error_code`，CHECK published/terminal 互斥；
5. 更新 grant/manifest hash helper、`record_review_decision` 和 publication-outbox v2；先应用 claim structured changes，再捕获 manifest/grant/outbox；
6. legacy active grant/pending v1 event 写 quarantine，event terminal `publication_manifest_required`；不创建 manifest/public 行；
7. revoke `uap_api` 全 core/ops DML、review/grant/audit 私有 DML；revoke Publisher public DML和 generic publication ack；收紧 default privileges；
8. 添加静态与真实角色 runtime probe。

Upgrade 数据规则：

- 既有 public 表若非空但不能证明与 active typed manifest 对应，upgrade 必须阻断，不得清空或采信；
- legacy grants 可保留并 quarantine；
- v1 event 不标 published；
- enum 转换不做同义词映射。

Downgrade：仅当无 v2 manifest/identity/quarantine、无 terminal marker、无 WP10 public 状态时允许。否则 `22023/publication_contract_state_blocks_downgrade`。拒绝发生在 DROP/ALTER 前且事务全回滚。

## 2. 0021 `wp10_publisher_projection`（WP10.2）

顺序：

1. 新增 delivery-attempt 历史、专用 claim/apply/fail SQL 函数及必要索引；
2. REVOKE PUBLIC 和非 Publisher EXECUTE；
3. SQL contract/permission tests；
4. Python publishing service（不 commit）与 Publisher loop（事务 owner）；
5. unit test → failure injection →真实 lease/runtime probe；
6. validator 更新，但 CI 总接线延期 10.6。

Downgrade：public document/entity 行、identity、delivery-attempt、terminal/retry event 任一存在即拒绝。空状态才 drop 函数/索引。

## 3. 0022 `wp10_claim_search_projection`（WP10.3）

顺序：

1. 新增 rebuild-run command 记录，扩展 apply/rebuild 支持 claim/evidence/document_entities/search；
2. 增加 public claim/evidence keyset/reconcile 所需索引和 constraint repair；
3. 数据库原子性/withdraw/republish/rebuild tests；
4. Python handler dispatch 扩展；
5. runtime probe。

不得新增 relation manifest/function/handler。Downgrade 在任何 claim/evidence/document_entities/search内容、对应 identity 或 rebuild-run 记录存在时拒绝。

## 4. 0023 `wp10_api_read_indexes`（WP10.4）

只增：

- `public.documents(published_at DESC,id DESC)`；
- category/fact_status + keyset 组合索引；
- `public.entities(published_at DESC,id DESC)` 与 type 组合索引；
- claim document/ordinal、document_entities 双向索引；
- search facets/query 所需、经 EXPLAIN 证明的最小索引。

不改变 DTO 语义、不新增搜索服务。索引 downgrade 可执行；若实现需要 column/type drop，必须另发设计勘误，不能夹带。

## 5. Python/HTTP/runtime/CI 全局顺序

同一阶段固定：

```text
migration source
  -> migration static/chain tests
  -> real upgrade + role/SQL probes
  -> Python contracts/service
  -> handler/process entrypoint
  -> unit/integration/runtime probe
  -> validator
  -> stage commit + STOP
```

- FastAPI/ASGI server、OIDC/JWT、连接池等依赖只能在 WP10.4/10.5 对应授权阶段加入 lock；10.1–10.3 不夹带 HTTP dependency。
- Makefile、Docker/compose、workflow 只在 WP10.6 授权后接线；此前用明确命令人工运行探针。
- 每个 migration 必须 upgrade→downgrade→upgrade（空状态）及“有状态拒绝后仍完整”两类证据。

## 6. 禁止的数据迁移

- 从 legacy SQLite 自动导入 public（WP11）；
- 从 latest analysis 自动挑 title/summary/category/fact status；
- 把 internal UUID 直接复制为 public UUID；
- 删除/更新历史 decision、grant、audit event；
- 把 v1 outbox 标 published 以消除积压；
- 为通过 downgrade 自动 purge public/manifest/identity；
- 暂时解除 constraint 或授予登录角色 DML 后忘记收回。

## 7. Alembic/validator 断言

- 单一 head，down_revision 严格为上一 WP10 revision；
- 文件名/revision id/head 断言同步；
- 0001–0019 blob hash 不变；
- G8-16C 和 WP9 function signatures/EXECUTE 关键断言保持；
- 每个新 object 在 migration、permissions、acceptance、validator 中至少各有一个映射；
- downgrade 拒绝码进入 error registry 和 runtime probe。
