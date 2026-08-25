# ADR-0015：Review Case 与 Decision 生命周期

- 状态：Proposed for `G9-FROZEN-20260825-05`
- 日期：2026-08-25
- 前置：0002 `audit.review_cases` / `review_decisions`、ADR-0014、ADR-0013

## 1. 背景

0002 已有 case / decision 表、单一 subject 检查、以及“同一 subject 至多一个未关闭 case”。WP8 未写入这些表。WP1 OpenAPI 草案使用 `candidate` / `in_review` / `document_version` 等与数据库不一致的词。

## 2. 决策：数据库枚举权威

权威状态为：

```text
audit.review_status = open | assigned | approved | rejected | disputed | withdrawn | closed
audit.review_decision = approve | reject | dispute | withdraw | revise
audit.review_case_type = document | claim | entity | relation
```

WP1 OpenAPI 是合同草案。WP9 DTO、探针和错误码使用上列数据库值。禁止平行状态机。

subject 列与 `case_type` 已由 0002 CHECK 对齐。API 查询参数 `subject_type` 使用 `document|claim|entity`。

## 3. 打开、指派、关闭

`audit.open_review_case(p_case_type, p_subject_id, p_priority, p_reason)`：

1. `require_active_role('reviewer')`；
2. `p_case_type='relation'` → `22023` / `knowledge_relation_review_not_in_wp9`，不插行；
3. 按类型校验 subject 存在：
   - document → `core.document_versions.id`；
   - claim → `core.claims.id`；
   - entity → `core.entities.id`（必须已是实体，不能用 candidate id）；
4. 插入 case：`status='open'`，`opened_by`=GUC principal，`opened_at=clock_timestamp()`；
5. 同一未关闭 subject 由 `uq_open_*_case` 保证；冲突 `23505` / `review_case_already_open`；
6. 追加 `audit_events.action='review.case.open'`。

`audit.assign_review_case(p_case_id, p_assignee_id)`：

- 要求 reviewer；
- case 必须 `open` 或 `assigned` 且 `closed_at IS NULL`；
- assignee 必须是 active person，且自身满足 reviewer 绑定；
- `status='assigned'`，`assigned_to=p_assignee_id`。

`audit.close_review_case(p_case_id, p_reason)`：

- 要求 reviewer；
- 仅当最新决定已把状态带到 `approved|rejected|withdrawn|disputed` 或明确无 grant 的终态后允许 `closed`；
- 不得在仍 `open/assigned` 且无决定时直接 close（`22023` / `review_case_not_decidable`）。

不提供 DELETE case。

## 4. 追加决定

`audit.record_review_decision(p_case_id, p_decision, p_reason, p_structured_changes jsonb)`：

1. `require_active_role`：`withdraw` 要求 `senior_reviewer`，其余 `reviewer`；
2. case `closed_at IS NULL`；
3. `sequence_no = coalesce(max(sequence_no),0)+1`，在 `SELECT ... FOR UPDATE` 的 case 行上计算；
4. 插入 `review_decisions`，`decided_by`=GUC principal，不 UPDATE 旧决定；
5. `supersedes_decision_id` 仅 `revise` 可指向同 case 前序 `approve` 或 `revise`；
6. 更新 case.status：

| decision | 新 status |
|---|---|
| approve | approved |
| revise | approved |
| reject | rejected |
| dispute | disputed |
| withdraw | withdrawn |

7. 自审隔离：若 case subject 是该 principal 经 `audit.create_manual_claim` 创建的 claim（`claims.created_by`），则 `42501` / `review_self_review_denied`。AI 物化 claim 的 `created_by` 为空，不适用本条。
8. 追加 audit event，`event_key` 见 ADR-0014 §2.4。
9. WP9.3 起，本函数在同一 PL/pgSQL 块内按决定类型调用私有副作用（ADR-0016 grant/outbox）。WP9.6 起，若 `p_structured_changes` 含冻结键，再调用私有 `_apply_claim_subject_bind` / `_replace_claim_evidence` / `_retire_manual_claim_supports`（ADR-0019）。这些私有函数无登录角色 EXECUTE，调用方不能事后单独执行。

`p_reason` 最短 10 字符。`p_structured_changes` 必须是 object，禁止数组或标量。未知键 → `22023` / `review_structured_changes_unsupported`。

允许键按阶段冻结：

| 阶段 | 允许键 | 适用 decision |
|---|---|---|
| WP9.2 | 无，只 `{}` | 全部 |
| WP9.3–9.5 | 只 `{}`；grant 由 decision 类型推导，不经 structured_changes | 全部 |
| WP9.6 claim case | `bind_subject_entity_id` (uuid) | `approve` 或 `revise` |
| WP9.6 claim case | `replace_supporting_span_ids` (uuid 数组，长度 ≥ 1) | 仅 `revise` |
| WP9.6 claim case | `retire_supporting_evidence` (JSON `true`) | 仅 `reject` 或 `withdraw` |

document/entity case 在 WP9 只允许 `{}`。relation case 不会进入本函数成功路径。

## 5. 不做

- 把 entity_candidate 加成第五种 `review_case_type`；
- 实现 relation case 成功路径；
- 覆盖或删除历史 decision；
- 让 Worker 自动打开 case（WP9 打开只经 `uap_api` 函数）；
- 向登录角色授予私有 `_apply_*` / `_replace_*` / `_retire_*` EXECUTE。
