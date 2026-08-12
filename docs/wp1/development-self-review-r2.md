# WP1 第二轮补充整改设计自检记录

- 自检编号：`WP1-REMEDIATION-R2-DEV-20260812-01`
- 冻结标准：`G1-FROZEN-20260811-01`
- 对应不通过记录：`G1-REJECT-R2-20260812-01`
- 自检工具：`tools/validate_wp1_design.py`
- 自检结论：第二轮补充整改开发自检通过
- G1 门禁：已提交第三次独立复验，复验通过前 WP2 继续关闭

## 用例结果

| 用例 | 补充整改后实际结果 | 自检 | 证据 |
|---|---|---|---|
| G1-01 | 模型运行写 `ops.model_runs`，结构化分析结果写 core；独立 Publisher 写 public | 通过 | `architecture.md`、ADR-0001、ADR-0006 |
| G1-02 | 业务状态与 `ops.jobs/job_attempts` 继续解耦 | 通过 | `data-model.md`、ADR-0004 |
| G1-03 | 49 张唯一逻辑表的所有者、主键、唯一约束继续完整；别名来源、三类标签来源外键已补齐；关系授权唯一 | 通过 | `data-model.md`、语义检查 |
| G1-04 | AI 结果追加、同文档同类型选择和实体候选来源约束保持完整 | 通过 | `data-model.md`、ADR-0005 |
| G1-05 | 普通 Worker 无 public 权限且不加载发布 handler；独立 Publisher 以唯一运行时写入凭据发布或撤回投影 | 通过 | `architecture.md`、`module-boundaries.md`、`permissions.md` |
| G1-06 | PostgreSQL 继续作为唯一目标事实数据库 | 通过 | ADR-0002 |
| G1-07 | 根目录 `alembic/versions/` 继续作为唯一可部署迁移链 | 通过 | ADR-0002、`module-boundaries.md` |
| G1-08 | OpenAPI、跨文档权限/分层/外键/授权语义和交付文件哈希均纳入同一证据包 | 通过 | `design-validation.json`、`MANIFEST.sha256` |

## 自动检查摘要

- 检查项：28/28 通过。
- 逻辑目标表：49，重复名称 0；44/44 个数据字典章节具备所有者、主键和唯一约束。
- OpenAPI：3.1.0 规范校验通过；三个详情 DTO 正例通过，额外字段反例均被拒绝。
- Publisher：容器、时序、部署、代码布局和权限边界一致；时序图不存在普通 Worker 写 public。
- 数据分层：`model_run` 写 `ops.model_runs`，`analysis_result` 写 core。
- 关系约束：别名来源及三个标签来源外键完整；关系授权唯一；撤回决定与授权 subject 属于同一 review case。
- 证据清单：`artifacts/wp1-design-remediation-r2-20260812/MANIFEST.sha256`。

## 待独立复验

- 架构负责人：未签署
- 数据负责人：未签署
- 安全负责人：未签署
- 最终 G1 结论：待第三次独立复验；本自检不宣布 G1 通过
