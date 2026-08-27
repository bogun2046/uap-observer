# ADR-0016：Publication Grant 与 Publisher Outbox

- 状态：Proposed for `G9-FROZEN-20260825-05`
- 日期：2026-08-25
- 前置：0002/0003 grant 表与 `validate_publication_grant`、ADR-0004 Outbox、ADR-0015、ADR-0006

## 1. 背景

公开投影只允许 `uap_publisher` 写入 `public`。0003 已要求 grant 的 decision 为 approve/revise，撤回 decision 为 withdraw，且 decision 与 subject 属于同一 case。WP4 禁止普通 Worker/API 入队 `publish_document` / `withdraw_document` / `invalidate_public_cache`。

WP9 必须在不破坏这些边界的前提下，把“审核通过”变成 Publisher 可消费的事实。

## 2. 决策：Grant 由决定函数内创建

`audit.record_review_decision` 在 WP9.3 起，于同一事务：

- `approve` / `revise`：插入对应类型的 publication grant；
- `withdraw`：将当前 active grant 标为 withdrawn（写 `withdrawn_by_decision_id`、`withdrawn_at`、`grant_status='withdrawn'`），不得 DELETE grant；
- `reject` / `dispute`：不创建 grant，不改已有 grant。

grant 字段：

- `review_case_id` / subject id / `decision_id` 取自本事务新决定；
- `revision_no`：取得 case 行锁后，对该 subject 全部 grant 计算 `max(revision_no)+1`（无历史则为 1）。禁止使用锁前缓存的 revision。
- `grant_status='active'`（非 withdraw）；
- `publication_payload_sha256`：对冻结的最小投影信封做 SHA-256，信封只含 ID、类型、revision、subject 哈希，不含正文、Prompt、审核人显示名。

`revise` 必须先将同 subject 当前 `grant_status='active'` 的行改为 `superseded`（`withdrawn_at` 与 `withdrawn_by_decision_id` 保持 NULL），再插入新的 `active` grant。0002 部分唯一索引

```text
UNIQUE (subject_id) WHERE withdrawn_at IS NULL
```

会把 `superseded` 旧行仍当作占用者，新 active 行必然 `23505`。因此 **WP9.3 迁移必须替换 document/claim/entity 三张 grant 表的唯一索引**，不得依赖现有索引。

WP9.3 `0016_review_decisions_and_grants` upgrade：

1. 不修改 0002/0003 源文件。
2. 对 `audit.document_publication_grants`、`claim_publication_grants`、`entity_publication_grants`：
   - `DROP INDEX` `uq_document_grant_active` / `uq_claim_grant_active` / `uq_entity_grant_active`；
   - `CREATE UNIQUE INDEX ... (subject_id) WHERE grant_status = 'active'`，名称分别为 `uq_document_grant_live` / `uq_claim_grant_live` / `uq_entity_grant_live`；
   - 增加 CHECK：`active` 与 `superseded` 均要求 `withdrawn_at IS NULL AND withdrawn_by_decision_id IS NULL`；`withdrawn` 要求两者均非 NULL。
3. `audit.relation_publication_grants` 的 `uq_relation_grant_active` **不改**（WP9 无 relation grant 成功路径）。
4. 0003 `validate_publication_grant` 保持：`superseded` + 空撤回字段可通过；不得放宽 approve/revise/withdraw 绑定。

downgrade：

1. 若三张表任一存在 `grant_status='superseded'`，`RAISE` 拒绝降级（稳定码 `review_grant_superseded_blocks_downgrade`）。
2. 否则 DROP 新 CHECK 与 `uq_*_grant_live`，重建原 `WHERE withdrawn_at IS NULL` 索引。

同一 subject 在提交后至多一行 `grant_status='active'`。

并发契约（PostgreSQL 默认 `READ COMMITTED`）冻结为 **串行双成功**，不使用 `expected_active_grant_id` 乐观失败：

1. `record_review_decision` 必须先 `SELECT ... FOR UPDATE` **case 行**，再读取当前 `grant_status='active'` 的 grant（若 `revise`/`withdraw`）。
2. 后到事务等待锁；前一事务提交后，后到事务看到的是新的 active grant 与已增加的 `max(revision_no)`。
3. 两个不同 `request_id` 的并发 `revise` **都可以成功**：各追加一条 decision；revision_no 严格递增（例如 1→2 与 2→3）；最终恰好一行 active；较早 grant 均为 `superseded`。
4. 不要求、也不允许把后到事务定义为必须 `40001` / `23505`。live 唯一索引只防止锁协议被绕过时的双 active，不是并发双方的预期终态。
5. 无丢失更新：后到事务必须 supersede **锁后读到的** active 行，不得 supersede 锁前快照中的旧 grant id。
6. 相同 `request_id` 的重放仍按 ADR-0014 §2.4，不得因串行化再插第二份 decision。

document / claim / entity 三张 grant 表均走此路径。relation grant 函数入口直接失败：`22023` / `knowledge_relation_review_not_in_wp9`。

## 3. 决策：只写 Outbox，不入队 publish job

私有 `ops.enqueue_publication_outbox(...)`：

- owner `SECURITY DEFINER`，`REVOKE ALL FROM PUBLIC`；
- 无登录角色 EXECUTE；仅审核函数内部调用；
- `INSERT ops.outbox_events`，幂等键：

| event_type | event_key |
|---|---|
| `publication.granted` | `publication-granted:{grant_table}:{grant_id}` |
| `publication.withdrawn` | `publication-withdrawn:{grant_table}:{grant_id}:{decision_id}` |
| `publication.superseded` | `publication-superseded:{grant_table}:{old_grant_id}:{new_grant_id}` |

payload schema `publication-outbox.v1`，只含 grant id、subject type/id、decision id、revision_no、payload sha256。

不调用 `ops.enqueue_job`。Publisher 在 WP10 领取 Outbox 后才写 `public` 并可选入队 `invalidate_public_cache`。

WP9 探针断言：

- `uap_api` 对 `public.*` 无 INSERT/UPDATE/DELETE；
- 决定成功后 `ops.jobs` 中无新的 `publish_*` 行；
- `ops.outbox_events` 有且仅有对应 event_key。

## 4. 权限

`uap_api` 仍无 `public` 写权限、无 Outbox DML、无 `ops.enqueue_job` 对 publisher 类型的执行权。`uap_publisher` 在 WP9 不获得新的审核函数 EXECUTE。

## 5. 不做

- 写 `public.documents` / `claims` / `entities` / `relations` / `search_*`；
- 公开 API、CDN、slug 生成规则（WP10）；
- 让 Worker 扫描 grant 后直接投影。
