# WP3 PostgreSQL 与对象存储

状态：实现与开发自检完成，待独立 G3 验收；WP4 门禁关闭

- 工作包编号：`WP3-IMPL-20260812-01`
- 冻结标准：`G3-FROZEN-20260812-01`
- 前置门禁：G2 已通过（`G2-ACCEPT-20260812-01`）
- 权威设计：`docs/wp1/data-model.md`、`docs/wp1/permissions.md`

## 范围

- 实现 `ingest/core/ops/audit/public` 五个 PostgreSQL Schema 的权威 Alembic 迁移链。
- 实现主键、外键、唯一约束、检查约束、必要索引和数据库角色权限。
- 实现内容寻址对象存储适配器，统一登记 `core.stored_objects` 并校验上传后哈希。
- 实现 PostgreSQL 与对象存储清单的自动备份、独立恢复和一致性校验脚本。
- 实现从空数据库及已有 Alembic 版本顺序升级的自动测试。

## 不做事项

- 不迁移现有 SQLite 业务数据；数据迁移在后续独立工作包中执行。
- 不实现 WP4 的 Worker 租约、重试、死信和 Outbox 运行逻辑；WP3 只建立冻结 Schema。
- 不实现 WP5-WP11 的采集、提取、AI、审核、发布 API 或前端业务流程。
- 不把对象内容写入 PostgreSQL，也不向 `public` 角色暴露 raw、derived 或 model-io 对象。
- 不在开发人员自检后直接宣布 G3 通过。

## 交付导航

- 实现单：`implementation-ticket.md`
- 独立验收单：`acceptance-ticket.md`
- 冻结用例：`acceptance-cases.md`
- 开发自检：`development-self-review.md`
