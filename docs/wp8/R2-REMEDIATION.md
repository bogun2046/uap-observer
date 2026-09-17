# WP8 R2 设计阻断闭环

本文件记录 `G8-FROZEN-20260821-01` 设计审核的十项阻断，以及 R2 的权威闭环。实现者不得重新选择其它方案。

| ID | R1 阻断 | R2 冻结结论 | 权威位置 | 验收 |
|---|---|---|---|---|
| B1 | 把 WP7 合法空数组当终态失败 | valid 空数组 `succeeded`、零物化；只有非空结果全部不可定位才终态失败 | ADR-0010 §6 | G8-10、12、15 |
| B2 | rejected metrics 无受控写路径；SQL RAISE 后事务不可直接 finish | 新增 `ops.finish_knowledge_job`；冻结 SAVEPOINT / 回滚算法；租约失败不伪收口 | ADR-0011 §5–6 | G8-05、12、15 |
| B3 | extraction 同哈希可多行；span 哈希未含 extraction 身份 | 0/1/>1 分别 missing/matched/ambiguous；span 哈希 envelope 含 extraction id 与 input hash | ADR-0010 §2–3 | G8-07、08 |
| B4 | PDF/媒体只检查轴存在，未证明字符轴对应页码/时间轴 | 由锚定 `location_map` 计算字符命中集合与页/时间命中集合，要求集合相等 | ADR-0010 §5 | G8-09 |
| B5 | EntityCandidate 允许 1–20 evidence，表只能放一条 | 新增 `core.entity_candidate_evidence`；旧单值列仅兼容，不是 WP8 写入权威 | ADR-0009 §5 | G8-06、14、15 |
| B6 | payload / bundle 未精确绑定 provenance；幂等键冲突可静默接受不同 payload | `knowledge.v2` 携带 result hash/schema/anchor 状态；bundle 全 ordinal 覆盖；冲突 payload 必须失败 | ADR-0008 §3、ADR-0011 §3 | G8-03、11、13、14 |
| B7 | 仅端点行锁无法证明长环并发安全 | 所有 merge/reverse 先取固定图级事务 advisory lock，再按 UUID 升序锁实体 | ADR-0012 §4 | G8-18 |
| B8 | merge EXECUTE 给 Worker/API，违反 senior_reviewer 边界 | WP8 建状态机函数但不给任何登录运行时角色 EXECUTE；WP9 才绑定 senior reviewer | ADR-0012 §3 | G8-19 |
| B9 | WP8.1/8.2 门禁要求尚未实现的 handler/materialize 成功项 | 用例按基础设施、纯映射、Claims、Entities、Merge、CI 六阶段重新切分 | 实施/验收任务书 | 各 `G8-GATE-8.x` |
| B10 | 设计未进入 SHA 链，WP8.1 仍从 G7 直接分叉 | 先形成 docs-only R2 设计提交；WP8.1 从该提交 SHA 开始 | BASELINE、实施任务书 §阶段链 | G8-20 |

## 闭环规则

- 上表十项均为架构决议，不交由实施者自由裁量。
- 若实现发现冻结方案与 G7 事实不兼容，当前阶段立即停止，由 Codex 发新设计编号。
- 不允许把 R1 文档、旧 zip 或旧提示词与 R2 混用。
