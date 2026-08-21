# WP8 主张、证据、实体与知识交接

状态：`G8-FROZEN-20260821-02` 已冻结；等待项目负责人授权 WP8.1。
父基线：G7-R2 `a34acd3282001421de1376e6e62ca3d7cf0f4233`。

WP8 把 WP7 的 valid `claim_extraction` / `entity_extraction` 分析结果，经 WP4 durable jobs 物化为内部 Claim、Entity Candidate 与 Evidence。AI 结果仍是候选，不进入 `public`；审核、发布与关系抽取不在本阶段交付。

## R2 核心冻结

1. 每条 valid claim/entity 分析均同事务入队，与 `analysis_selections` 无关。
2. WP7 允许合法空数组；空数组任务必须 `succeeded` 且物化计数为 0。
3. 部分候选成功可成功；非空结果全部无法定位才 `terminal_failure`。
4. 新增 `ops.finish_knowledge_job`，唯一受控地写 attempt metrics 并结束知识任务。
5. 预期失败使用 SAVEPOINT 回滚领域写入后再结束 attempt；租约失效不得伪造收口。
6. extraction 锚定必须唯一；零匹配和多匹配分别为 missing / ambiguous，不得取最新。
7. span 身份包含 `extraction_id` 与 `input_sha256`，相同坐标在不同提取上不得复用。
8. PDF、音视频 locator 必须对字符轴与页码/时间轴做双向对应校验。
9. 新增 `core.entity_candidate_evidence`，完整表达一个候选的 1–20 条 evidence。
10. materialize bundle 必须与 job、analysis_result、model_run、schema、result JSON 和 locator ordinal 精确绑定。
11. merge/reverse 使用图级事务 advisory lock；WP8 不向任何登录运行时角色授予执行权。
12. WP8.x 串行独立验收；WP8.1 从本 docs-only 设计提交开始。

## 文档

| 文件 | 作用 |
|---|---|
| [BASELINE.md](BASELINE.md) | 固定父基线、设计提交与放行规则 |
| [R2-REMEDIATION.md](R2-REMEDIATION.md) | R1 十项阻断的闭环矩阵 |
| [TOPIC-INDEX.md](TOPIC-INDEX.md) | 冻结主题到 ADR / 用例的索引 |
| [implementation-ticket.md](implementation-ticket.md) | WP8.1–8.6 实施边界和阶段链 |
| [acceptance-ticket.md](acceptance-ticket.md) | 独立验收责任、证据和门禁 |
| [acceptance-cases.md](acceptance-cases.md) | G8-01–G8-20 正反向验收用例 |
| [adr/0008-knowledge-job-handover.md](adr/0008-knowledge-job-handover.md) | analysis_result → resolve_* 事务性交接 |
| [adr/0009-claim-document-evidence-constraints.md](adr/0009-claim-document-evidence-constraints.md) | Claim / Entity Candidate / Evidence 数据库一致性 |
| [adr/0010-evidence-locator-mapping.md](adr/0010-evidence-locator-mapping.md) | extraction 锚定、坐标与 span 身份 |
| [adr/0011-knowledge-write-authority.md](adr/0011-knowledge-write-authority.md) | 写权限、bundle 绑定、事务与 metrics |
| [adr/0012-entity-merge-state-machine.md](adr/0012-entity-merge-state-machine.md) | 合并、撤销、并发和授权边界 |
| [adr/0013-relations-out-of-scope.md](adr/0013-relations-out-of-scope.md) | 关系非范围与禁止空成功 |
| [SHA256SUMS](SHA256SUMS) | 除自身外冻结文档的 SHA-256 |

## 明确不授权

设计冻结不等于自动开工。项目负责人未提供完整启动口令前，不得写业务代码、迁移、CI、commit 或 PR；实施中也不得修改本冻结标准来适配代码。
