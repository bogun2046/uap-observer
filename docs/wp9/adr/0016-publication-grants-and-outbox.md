# ADR-0016：Publication Grant 与 Publisher Outbox

- 状态：Proposed for `G9-FROZEN-20260825-01`
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
- `revision_no`：该 subject 历史 grant 数 + 1；
- `grant_status='active'`（非 withdraw）；
- `publication_payload_sha256`：对冻结的最小投影信封做 SHA-256，信封只含 ID、类型、revision、subject 哈希，不含正文、Prompt、审核人显示名。

`revise` 必须先将同 subject 仍 active 的 grant 标为 `superseded`（`grant_status='superseded'`，不填 withdraw 字段），再插入新 active grant。禁止两行 `withdrawn_at IS NULL`（已有部分唯一索引）。

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
