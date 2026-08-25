# ADR-0018：授权的 Entity Merge / Reverse

- 状态：Proposed for `G9-FROZEN-20260825-01`
- 日期：2026-08-25
- 前置：ADR-0012、ADR-0011、ADR-0014、G8-19

## 1. 背景

WP8.5 已实现 `core.merge_entities` / `core.reverse_entity_merge`：图级 `pg_advisory_xact_lock(824, 1)`、升序行锁、防环、防长链。所有登录运行时角色 `REVOKE EXECUTE`。`merged_by` / `reversed_by` 只引用 active principal，不是授权。

WP1 规定合并/撤销由 `senior_reviewer` 批准。

## 2. 决策：包装函数，不扩大核心 EXECUTE

新增：

```text
audit.apply_entity_merge(p_source_entity_id, p_target_entity_id, p_reason)
audit.apply_entity_merge_reverse(p_merge_event_id, p_reason)
```

行为：

1. `require_active_role('senior_reviewer')`；
2. `session_user='uap_api'`；
3. 以 GUC principal 作为 `p_merged_by` / `p_reversed_by` 调用现有核心函数；
4. 追加 `audit_events`（`knowledge.entity.merge` / `knowledge.entity.merge_reverse`）；
5. 不改变 ADR-0012 的锁顺序、错误码或状态机。

核心函数权限保持：

| 函数 | 登录角色 EXECUTE |
|---|---|
| `core.merge_entities` | 无 |
| `core.reverse_entity_merge` | 无 |
| `audit.apply_entity_merge` | `uap_api` |
| `audit.apply_entity_merge_reverse` | `uap_api` |

owner/migrator 仍可在探针中直接测核心状态机，但生产路径只走包装函数。G8-19 继续断言 Worker/API/Publisher **不能** `EXECUTE core.merge_entities`。

`reviewer` 调用包装函数 → `42501` / `review_role_denied`。

## 3. 与 review case 的关系

Merge 不要求预先存在 entity case，也不自动 approve/grant。合并是图操作，发布授权仍针对 survivor 实体的独立 case。source 被 merge 后不得再打开新的 entity case（open 时校验 `status='active'` 且为 canonical）。

## 4. 不做

- `GRANT EXECUTE ON core.merge_entities TO uap_api`；
- 同名自动 merge；
- Worker 补偿扫描后 merge；
- 把 merge 伪装成 publication grant。
