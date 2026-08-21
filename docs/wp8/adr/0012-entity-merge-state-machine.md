# ADR-0012：Entity Merge / Reverse 状态机、并发与授权

- 状态：Accepted for `G8-FROZEN-20260821-02`
- 日期：2026-08-21
- 前置：WP1 permissions、ADR-0005、`core.entity_merge_events`

## 1. 背景

同名实体不能自动合并；合并必须有向、可撤销、可审计且不丢历史端点。仅锁本次 source/target 无法独立证明两个端点不相交的并发事务不会借既有图形成长环。另一个边界是授权：WP1 明确合并/撤销由 `senior_reviewer` 批准，active principal 只证明身份存在，不证明角色授权。

WP8 冻结并实现数据库状态机，但不实现 WP9 的审核 API 和 senior reviewer 绑定。

## 2. 图与状态

合并边：

```text
source (loser) -> target (survivor)
```

有效边仅为：

```text
event_kind='merge' AND reversed_at IS NULL
```

`merge_entities(A,B,...)` 必须：

- A、B 存在且不同；
- A、B 在锁内均为 `active`；
- A、B 均为当前 canonical；
- A 成为 `merged`，B 保持 `active`；
- 追加 merge event，不删除 A；
- 不因 canonical_name 相同自动调用。

拒绝 merged、retired、disputed source 或 target。

## 3. 授权边界：WP8 默认关闭

WP8 创建 `core.merge_entities` 和 `core.reverse_entity_merge`，但：

- `REVOKE ALL FROM PUBLIC`；
- 不向 `uap_worker`、`uap_api`、`uap_scheduler`、`uap_publisher`、`uap_model_governance`、`uap_public_reader` 或其它登录运行时角色授予 EXECUTE；
- `merged_by` / `reversed_by` 必须引用 active `audit.principals`，仅用于归属和审计，不是授权检查；
- 隔离验收通过 owner/migrator 部署身份验证内部状态机；生产运行时不可调用；
- WP9 必须另行冻结 senior_reviewer 会话绑定、审核决定与 API wrapper，之后才能授予最小执行权。

因此 WP8.5 的完成定义是“状态机正确且默认不可用”，不是“合并后台已上线”。

## 4. 图级串行化与行锁

所有 merge 和 reverse 的第一条锁操作固定为：

```sql
PERFORM pg_advisory_xact_lock(824, 1);
```

该事务级 advisory lock 表示 `uap.core.entity_merge_graph`。所有写图函数必须使用同一个 key；禁止 try-lock 后绕过。

锁原语依据：[PostgreSQL 16 Advisory Lock Functions](https://www.postgresql.org/docs/16/functions-admin.html#FUNCTIONS-ADVISORY-LOCKS)。

取得图锁后：

1. 对涉及实体按 UUID 升序 `SELECT ... FOR UPDATE`；
2. 对相关 open merge rows 按稳定顺序 `FOR UPDATE`；
3. 在锁内重新读取 status 和整个有效路径；
4. 验证 source/target canonical；
5. 从 target 沿 open merge 边 DFS/BFS，若到达 source 则 `knowledge_merge_cycle`；
6. 最多跟随 64 条边，超过则 `knowledge_merge_chain_too_long`；
7. 插入事件并更新状态。

图锁串行化所有写图事务，升序行锁保持与其它实体事务的死锁顺序一致。`uq_open_merge_source` 仍作为最终数据库防线：

```text
UNIQUE (source_entity_id)
WHERE event_kind='merge' AND reversed_at IS NULL
```

即使存在由历史导入或 owner 误操作造成的“事件图与 status 不一致”，图锁后的重读和防环也必须拒绝形成四节点等长环。

## 5. Canonical 解析

`core.canonical_entity_id(entity_id)` 只跟随有效 merge 边：

```text
seen = {}
while open merge exists for current source:
  if current in seen: knowledge_merge_cycle
  if hops >= 64: knowledge_merge_chain_too_long
  current = target
return current
```

读函数不修改行，仍防御已有坏图。只授予需要 core 读取的明确角色，不授 PUBLIC。

合并后，下列 FK 保留写入时 id，展示/聚合时才调用 canonical：

- `entity_candidates.resolved_entity_id`
- `entity_aliases.entity_id`
- `claims.subject_entity_id`
- `relations.subject_entity_id/object_entity_id`
- evidence 与 relation evidence

禁止批量改写端点。逻辑上变成自反的 relation 也保留物理历史行。

## 6. 表结构

线性迁移给 `core.entity_merge_events` 追加：

```text
event_kind text NOT NULL DEFAULT 'merge'
  CHECK (event_kind IN ('merge','reverse'))
```

保留：

- `source_entity_id <> target_entity_id`；
- `(reversed_by_id IS NULL) = (reversed_at IS NULL)`；
- `reversed_by_id UNIQUE REFERENCES entity_merge_events(id)`。

创建 `uq_open_merge_source`。既有行回填为 merge。

## 7. Reverse

`reverse_entity_merge(merge_event_id,reversed_by,reason)` 在同一图锁和事务中：

1. 原行必须是未撤销 `event_kind='merge'`；
2. 锁 source/target 和原事件；
3. 新增一行 `event_kind='reverse'`，source/target 仍复制原 A/B，不写 B→A；
4. 原行 `reversed_by_id` 指向新 reverse id，`reversed_at=clock_timestamp()`；
5. reverse row 的 `reversed_by_id/reversed_at` 均 NULL；
6. A 在不存在其它 open source edge 时从 merged 恢复 active；
7. 不改写任何关系、候选、别名、Claim 或 evidence FK。

reverse row 永远不成边。禁止：重复 reverse、reverse 一条 reverse row、用 `merge(B,A)` 冒充撤销。

允许 A→B、B→C 后撤销 A→B：A 恢复 active，B 仍沿 B→C 为 merged。

## 8. 审计

函数与领域变化同事务写 `audit.audit_events`：

- merge：`action='entity.merge'`；
- reverse：`action='entity.merge.reverse'`；
- `event_key` 分别为 `entity.merge:{merge_event_id}` / `entity.merge.reverse:{reverse_event_id}`；
- target 为对应事件 id；
- actor 为 merged_by/reversed_by；
- metadata 仅含 source/target、被撤销事件 id、稳定 reason 摘要或其 hash。

失败事务不留下部分 event/status/audit。

## 9. 并发验收

至少覆盖：

- 同 source 不同 target；
- A→B 与 B→A；
- schema 允许但 status 不一致的历史 A→B、C→D 上，并发 B→C 与 D→A；
- reverse 与同 source 新 merge 并发。

最终图必须无环、同 source 至多一条 open merge，且无死锁。四节点场景用于证明图级锁，而不是依赖正常 status 偶然阻止环。

## 10. 不采用

- 只锁本次两个端点；
- 仅靠应用 sleep/retry 防环；
- 合并时改写所有关系 FK；
- 删除 source；
- reverse 作为 B→A merge；
- 给 Worker/API merge EXECUTE；
- 把 active principal 当作 senior_reviewer 授权。
