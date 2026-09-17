# ADR-0024：WP10 延续 G8-16C relation fail-closed

- 状态：Proposed for `G10-FROZEN-20260828-01`
- 前置：ADR-0013、G8-16C、ADR-0015/0016

## 仓库事实

`ops.model_task_type` 只有 translation、summary、classification、entity_extraction、claim_extraction；WP7 strict Schema无relation；WP8触发器不入队、worker claim集合不含relation、误领terminal `knowledge_relation_task_not_in_wp8`；WP9 open/decision/grant明确拒绝relation。

## 决策

G8-16C在WP10.1–WP10.6全程保留：

- 不新增relation_extraction enum/schema/prompt；
- 不入队/claim/materialize `resolve_relations`成功路径；
- relation review/grant继续拒绝；
- 不创建relation manifest/outbox v2；
- Publisher遇relation event terminal，不写public、不ack成功；
- `/v1/relations`不注册；EntityDetail不返回误导性空relations。

## 未来解除的不可分割前置

只有未来WP11+独立冻结并串行签署以下全部门禁后，才能替代G8-16C：

1. 版本化relation输入：model task/strict schema或另一个已冻结确定算法；
2. analysis→job payload provenance、canonical hash、存量reconciliation；
3. relation bundle/evidence/端点canonical约束与owner materializer；
4. handler部署后才激活claimable set，成功/空/部分/全拒绝/lease/SAVEPOINT用例；
5. relation case/decision、自审/权限、active grant索引及withdraw/revise并发；
6. relation typed manifest、两端entity和evidence/publication依赖；
7. Publisher projection/withdraw/replay/rebuild；
8. `/v1/relations`与EntityDetail扩展、分页/可见性/性能；
9. 全链门禁签署与项目负责人明确解除口令。

在第9项签署之前，任何单点实现都不得把fail-closed改成succeeded或“暂时空结果”。

## 后果

WP10可安全发布已审核claims/entities而不伪造关系处理。relation API延期是显式产品边界，不是空数据状态。
