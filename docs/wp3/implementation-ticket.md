# WP3 实现单

- 实现编号：`WP3-IMPL-20260812-01`
- 前置验收：`G2-ACCEPT-20260812-01`
- 冻结标准：`G3-FROZEN-20260812-01`
- 状态：实现与开发自检完成，待独立验收
- 负责人：数据库实现人员

## 输入

- `docs/wp1/data-model.md` 的 49 张唯一逻辑表、五个 Schema 和约束策略。
- `docs/wp1/permissions.md` 的数据库角色矩阵与对象存储边界。
- WP2 已验收的 Python 3.12、Alembic、PostgreSQL 16.14、SeaweedFS 4.41 和 required CI gate。

## 实现任务

1. 建立不可分叉的 Alembic revision 链和五个 Schema。
2. 建立冻结枚举、49 张表、复合外键、部分唯一索引、检查约束和搜索索引。
3. 建立 `uap_owner`、`uap_migrator`、内部服务角色、`uap_public_reader`、`uap_audit_reader` 与 `uap_backup` 权限。
4. 实现对象存储接口、内容寻址 key、上传后 SHA-256 校验、统一对象登记及并发去重。
5. 实现数据库备份、对象清单、恢复和跨介质校验脚本。
6. 实现静态 Schema 契约测试和 PostgreSQL/SeaweedFS 运行态集成测试。
7. 生成开发自检、SQL 报告、对象哈希报告和完整 SHA-256 清单。

## 完成限制

实现人员只能提交“待独立验收”。任何 G3 用例失败均回到本实现单修复；WP4 在正式 G3 通过记录前保持关闭。
