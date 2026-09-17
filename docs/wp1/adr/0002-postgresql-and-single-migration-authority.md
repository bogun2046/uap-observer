# ADR-0002：PostgreSQL 与唯一 Alembic 迁移权威

- 状态：Accepted for G1
- 日期：2026-08-11

## 背景

当前 SQLite 迁移存在仓库与包内双副本，另有独立 `supabase/schema.sql`；数据库与代码已出现 013/014 版本差异。

## 决策

目标生产数据库统一为 PostgreSQL。项目根目录 `alembic/versions/` 是唯一可部署 DDL 历史；升级只能运行 Alembic。SQLAlchemy 映射或其他运行时模型不是独立 DDL 源，并由 Schema 一致性测试验证。SQLite 和 Supabase SQL 在 WP11 迁移完成后转为只读历史输入。

## 后果

- 空库和升级路径使用同一迁移链，消除复制漂移。
- 发布物必须明确包含 Alembic 版本和迁移镜像/入口。
- 紧急生产 DDL 仍必须补成正式迁移并通过审批，不接受长期手工漂移。

## 未采用方案

- ORM `create_all`：缺少可审计的顺序升级和回滚边界。
- 同时维护纯 SQL 与 Alembic：重复权威来源会重现当前问题。
- 继续以 SQLite 为生产权威：不满足并发 Worker、权限 Schema 和恢复目标。
