# WP2 工程环境与持续集成

状态：G2 独立复验通过；WP3 门禁开启
启动日期：2026-08-12（Asia/Shanghai）  
前置门禁：G1 已通过（`G1-ACCEPT-20260812-01`）

## 输入与范围

- 以 `platform/` 作为目标平台的新工程根目录，旧 `src/uap_observer` 保持为迁移输入。
- 建立 Python 3.12、依赖锁、Docker Compose 开发环境、PostgreSQL、S3 兼容对象存储、CI、密钥管理和 staging 部署入口。
- 建立 lint、类型检查、单元测试、迁移冒烟和安全扫描门禁。
- 只创建基础设施和空迁移框架，不建立 WP3 业务 Schema，不迁移或修改 SQLite 数据。

## 不做事项

- 不实现 PostgreSQL 业务表、数据库角色或对象登记表；属于 WP3。
- 不迁移 `data/uap.db`、`supabase/schema.sql` 或现有 SQL 数据。
- 不实现任务队列、采集、AI、审核、公开 API 或站点。
- 不部署真实 staging，不接触云账号或真实密钥；只交付可重复执行的部署入口和说明。

## 交付导航

- 实现单：`implementation-ticket.md`
- 独立验收单：`acceptance-ticket.md`
- 冻结用例：`acceptance-cases.md`
- staging 部署：`staging-deployment.md`
- 开发自检：`development-self-review.md`
- G2 不通过记录：`g2-rejection-record.md`
- G2 第二轮不通过记录：`g2-second-rejection-record.md`
- G2 安全整改标准补充：`acceptance-amendment-01.md`
- G2 安全整改说明：`security-remediation.md`
- G2 第二轮补充整改报告：`remediation-round2-report.md`
- G2 正式验收记录：`g2-acceptance-record.md`
