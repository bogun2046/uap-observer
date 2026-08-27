# ADR-0013：WP8 不交付 AI Relation 物化

- 状态：Accepted for `G8-FROZEN-20260821-02`
- 日期：2026-08-21
- 前置：WP4 job 白名单、WP7 model task/schema、ADR-0005

## 1. 背景

WP4 白名单预留 `resolve_relations`，但 WP7 的任务和严格 Schema 只有 translation、summary、classification、entity_extraction、claim_extraction；不存在合法 relation_extraction 输出。

在没有版本化输入的情况下提供空 handler 并返回 succeeded，会把“没有能力”伪装成“关系已处理”。

## 2. 决策

WP8 明确排除 AI Relation 物化：

1. analysis_result 触发器不入队 `resolve_relations`；
2. Worker 领取集合不包含 `resolve_relations`；
3. 不实现 relation success handler；
4. 不修改 WP7 枚举/Schema，不从 Claim 文本猜关系；
5. 不删除 WP4 已验收白名单字符串；
6. `core.relations` / `relation_evidence` 保留，WP8.5 只可用 owner 夹具验证 merge 后端点不改写；
7. `claims.subject_entity_id` 不是 relation 行；WP8.3 创建时保持 NULL，WP8.4 不回填，实体解析留给 WP9。

如果配置错误或人工构造导致通用分发器领取该类型，它必须调用现有 `ops.finish_job`：

```text
outcome = terminal_failure
error_code = knowledge_relation_task_not_in_wp8
```

不得调用 claims/entities materialize，关系表零写入，禁止 succeeded。`finish_knowledge_job` 的白名单不扩展到 relation。

## 3. 后续开放条件

未来关系工作包必须同时具备：

- 版本化 relation 输入（模型任务或独立冻结算法）；
- provenance 与 evidence 约束；
- 专用 handler、权限和验收用例；
- 从当时已验收 HEAD 线性继续的设计与实施门禁。

不得在 WP8 整改或后续小修中夹带。

## 4. 后果

G8 只能声明 Claim、Entity Candidate 与 Evidence 物化能力；任何报告、PR 或验收清单都不得把 `resolve_relations` 列为已交付成功路径。
