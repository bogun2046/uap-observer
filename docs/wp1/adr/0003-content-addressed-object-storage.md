# ADR-0003：按内容哈希寻址的对象存储

- 状态：Accepted for G1
- 日期：2026-08-11

## 背景

HTML、PDF、字幕、提取正文和模型原始响应可能较大，需要版本化、去重和可恢复，但不适合全部放入事务行或公开数据库。

## 决策

使用 S3 兼容对象存储保存不可变对象；`core.stored_objects` 是统一物理对象登记，唯一约束为 `(storage_domain, content_sha256)`，对象 key 全局唯一且只存在于该表。原始 `artifact_versions`、派生 `extractions`、模型 `model_runs` 分别以外键引用同一登记。上传前后校验哈希，同域同哈希通过原子 upsert 复用一个 `stored_object_id`，不重复存物理副本。

## 后果

- 数据库备份和对象存储清单必须联合恢复验证。
- 业务记录不能在对象尚未持久化、校验并完成统一登记时标记成功。
- artifact version、extraction 和 model run 不拥有 `object_key`，因此多个业务记录可以安全共享同一物理对象。
- 原始与模型对象默认私有；公开资产经过发布器复制到独立前缀。

## 未采用方案

- 全部存 PostgreSQL `bytea/text`：增加主库、WAL 和备份压力。
- 以来源 URL 作为对象 key：URL 可变且会泄漏 query/标题信息，不能可靠去重。
