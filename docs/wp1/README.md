# WP1 目标架构与数据模型

状态：G1 第四次独立复验通过；WP2 门禁已开启，尚未启动 WP2 实施  
启动日期：2026-08-11（Asia/Shanghai）  
前置门禁：G0 已通过  
现有代码基线：`6ca1af5adb9d1d2572b75d3c557896f02efb7e70`

## 输入与范围

- WP0 数据库、来源、迁移、测试、发布和已知问题基线。
- 目标形态：模块化单体、异步 Worker、独立 Publisher、PostgreSQL、S3 兼容对象存储、审核后台和独立公开读模型。
- 输出 C4 图、数据流、PostgreSQL ER、数据字典、模块边界、API 草案、权限矩阵、ADR 和服务目标草案。
- 明确原始、派生、审核、公开四层数据及其发布门禁。
- 明确每张目标表的主键、唯一约束、外键和数据所有者。

## 不做事项

- 不建立新工程、容器、CI 或 staging；这些属于 WP2。
- 不创建 PostgreSQL 实例、Alembic 迁移或对象存储适配器；这些属于 WP3。
- 不实现队列、采集器、AI、审核后台、API 或站点。
- 不迁移或修改当前 SQLite 数据库。
- 不把现有 `supabase/schema.sql` 宣布为目标权威 Schema；它只作为历史输入。

## 设计原则

- PostgreSQL 是目标系统唯一事实数据库；对象存储只保存不可变大对象，数据库保存元数据和哈希。
- Alembic 迁移链是唯一可部署 DDL 权威；运行时模型不得形成第二条迁移源。
- 业务记录、工作任务、模型运行、审核决策和公开投影分表保存。
- 原始和派生结果不可被公开数据库角色读取；公开 API 只访问 `public` Schema。
- AI 输出追加版本，不覆盖历史结果，也不能直接发布。
- 所有 Claim、Relation 和公开内容必须能回到具体文档版本与证据位置。

## 交付导航

- 冻结验收标准：`acceptance-cases.md`
- C4 与数据流：`architecture.md`
- ER 与数据字典：`data-model.md`
- 模块边界：`module-boundaries.md`
- API 契约：`openapi.yaml`
- 权限模型：`permissions.md`
- 服务目标：`service-targets.md`
- 架构决策：`adr/`
- 设计自检：`development-self-review.md`
- 第二轮补充整改自检：`development-self-review-r2.md`
- 第三轮补充整改自检：`development-self-review-r3.md`
- 独立验收不通过记录：`g1-rejection-record.md`
- 整改报告：`g1-remediation-report.md`
- 第二轮复验不通过记录：`g1-second-rejection-record.md`
- 第二轮补充整改报告：`g1-remediation-round2-report.md`
- 第三次复验不通过记录：`g1-third-rejection-record.md`
- 第三轮补充整改报告：`g1-remediation-round3-report.md`
- G1 最终验收记录：`g1-acceptance-record.md`
