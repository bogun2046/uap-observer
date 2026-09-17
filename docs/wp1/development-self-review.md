# WP1 整改后设计自检记录

- 自检编号：`WP1-REMEDIATION-DEV-20260812-01`
- 冻结标准：`G1-FROZEN-20260811-01`
- 对应不通过记录：`G1-REJECT-20260811-01`
- 设计基线：`6ca1af5adb9d1d2572b75d3c557896f02efb7e70` + 当前 WP1 设计文件
- 自检工具：`tools/validate_wp1_design.py`
- 自检结论：整改后设计自检通过
- G1 门禁：仍待架构负责人、数据负责人和安全负责人独立复验

## 用例结果

| 用例 | 整改后实际结果 | 自检 | 证据 |
|---|---|---|---|
| G1-01 | 五层边界保留；统一 `core.stored_objects` 由 object registry 拥有，原始/派生/模型 I/O 分别通过域约束外键引用 | 通过 | `architecture.md`、`data-model.md`、ADR-0003 |
| G1-02 | 目标模型不使用 `news` 承担任务状态；任务统一进入 `ops.jobs/job_attempts` | 通过 | `data-model.md`、ADR-0004 |
| G1-03 | 49 张唯一逻辑表分布在 44 个字典章节，每章均有所有者、主键和唯一约束；对象、授权、关系使用真实或复合外键 | 通过 | `data-model.md`、语义检查 |
| G1-04 | AI 只追加；selection 使用同文档/同类型复合外键；`entity_candidates` 保留 analysis result、文档、证据和解析实体 | 通过 | `data-model.md`、ADR-0005 |
| G1-05 | `uap_worker` 对 public 为无权限，运行时仅 `uap_publisher` 可写；公开 DTO 实例和禁止字段检查通过 | 通过 | `permissions.md`、`openapi.yaml`、语义检查 |
| G1-06 | PostgreSQL 为唯一目标事实数据库；SQLite/Supabase 只作迁移输入 | 通过 | ADR-0002 |
| G1-07 | 唯一可部署迁移链为根目录 `alembic/versions/`，禁止包内迁移副本 | 通过 | ADR-0002、`module-boundaries.md` |
| G1-08 | OpenAPI 规范、实例、对象去重、发布授权、AI 复合约束和权限矩阵交叉检查全部通过 | 通过 | `design-validation.json` |

## 自动检查摘要

- 检查项：24/24 通过。
- 逻辑目标表：49，重复名称 0。
- 数据字典覆盖：44/44 章节包含所有者、主键和唯一约束。
- OpenAPI：3.1.0，`openapi-spec-validator 0.7.1` 完整规范校验通过。
- DTO 实例：`DocumentDetail`、`EntityDetail`、`ReviewCaseDetail` 均通过 JSON Schema Draft 2020-12；加入未知字段后均被拒绝。
- API：7 个公开路径、5 个管理路径、23 个 Schema。
- 公开/内部 DTO 禁止字段：0。
- 跨文档语义：统一对象登记、四类发布授权、文档—实体关联、AI 复合外键、实体候选来源、Worker/Publisher 权限隔离全部通过。
- 证据清单：`artifacts/wp1-design-remediation-20260812/MANIFEST.sha256`。

## 待独立复验

- 架构负责人：未签署
- 数据负责人：未签署
- 安全负责人：未签署
- 最终 G1 结论：待复验；本自检不宣布 G1 通过
