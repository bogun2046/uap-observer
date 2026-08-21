# WP8 R2 冻结主题索引

## 1. valid 结果与 selections

- ADR-0008：§2 触发条件、§5 `analysis_selections`
- G8-01、G8-02
- 结论：每条 valid claim/entity 都入队；WP8 不读不写 selections。

## 2. 存量补偿与幂等冲突

- ADR-0008：§4 幂等、§7 补偿
- G8-03、G8-04
- 结论：迁移回填和受控 reconciliation 共用同一 payload builder；同 key 不同 payload 必须失败。

## 3. valid 空数组、部分成功和全拒绝

- ADR-0010：§6 物化结果语义
- ADR-0011：§5 metrics
- G8-10、G8-12、G8-15
- 结论：空数组是成功零结果；非空全拒绝才是终态失败。

## 4. 任务收口、metrics 与失败事务

- ADR-0011：§5 `finish_knowledge_job`、§5.1 metrics、§6 事务算法
- G8-05、G8-12、G8-15
- 结论：Worker 无 attempt UPDATE；受控函数写 metrics 并结束任务；预期 RAISE 先回滚到 SAVEPOINT。

## 5. extraction 唯一锚定与 span 身份

- ADR-0008：§3 `knowledge.v2` payload
- ADR-0010：§2 extraction 锚定、§3 span identity
- G8-07、G8-08
- 结论：禁止“最新提取”和任意挑选同哈希行；hash envelope 含 extraction id/input hash。

## 6. locator 跨轴校验

- ADR-0010：§4 分类型映射、§5 location_map 双向对应
- G8-09
- 结论：PDF/媒体的字符命中集合必须与页码/时间命中集合一致。

## 7. Entity Candidate 多 evidence

- ADR-0009：§5 `entity_candidate_evidence`
- ADR-0011：§3 bundle 覆盖
- G8-06、G8-14、G8-15
- 结论：新增真实关联表完整保存 1–20 条 evidence；旧单值列不再作为 WP8 权威。

## 8. bundle provenance 与 Schema

- ADR-0008：§3 payload
- ADR-0011：§3 精确绑定
- G8-03、G8-11、G8-13、G8-14
- 结论：接受的分析结果版本是数据库实际持久化的 `ai.v1`；每个 candidate/locator ordinal 必须与 result JSON 对齐。

## 9. 消费者上线顺序

- ADR-0008：§6 消费者激活
- G8-16
- 结论：触发器可先排队；领取集合始终是已部署 handler 的子集。

## 10. merge 图并发与撤销

- ADR-0012：§4–7
- G8-17、G8-18、G8-19
- 结论：图级 advisory lock + 升序行锁；reverse 不是反向 merge，不改写关系端点。

## 11. merge 授权

- ADR-0012：§3
- G8-19
- 结论：active principal 不是 senior_reviewer 授权；WP8 不给 Worker/API/Publisher 等登录角色 EXECUTE。

## 12. 关系非范围

- ADR-0013
- G8-16C
- 结论：不入队、不实现 `resolve_relations` 成功路径；误领取必须 terminal failure。

## 13. 阶段独立门禁

- implementation-ticket：§阶段链、§阶段交付
- acceptance-ticket：§WP8.x 独立验收门禁
- G8-01–G8-20
- 结论：基础设施、映射、Claims、Entities、Merge、CI 各自只验已实现能力。

## 14. 设计提交进入 SHA 链

- BASELINE：§冻结提交与起点
- implementation-ticket：§阶段链
- G8-20
- 结论：WP8.1 起点是本 docs-only 设计提交，不再直接从 G7 分叉。
