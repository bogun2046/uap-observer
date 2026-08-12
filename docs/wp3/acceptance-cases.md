# G3 冻结验收用例

- 冻结编号：`G3-FROZEN-20260812-01`
- 冻结时间：2026-08-12（Asia/Shanghai）
- 前置门禁：`G2-ACCEPT-20260812-01`
- 变更规则：实现开始后不得降低预期结果；补充安全断言只能新增用例并记录原因。

## G3-01 空数据库迁移

前置：全新 PostgreSQL 16 实例，不存在五个业务 Schema 和 `alembic_version`。

步骤：以临时启用的 `uap_migrator` 执行 `alembic upgrade head`，再查询 revision、Schema、表、枚举、约束和索引。

预期：迁移一次成功；只有一个 Alembic head；五个 Schema 均存在；49 张冻结逻辑表名称唯一；所有主键、外键、唯一约束、检查约束和索引与权威数据字典一致；重复执行 upgrade 无变化。

## G3-02 已有版本顺序升级

前置：分别停在每个非 head revision 的独立数据库。

步骤：按 revision 顺序逐级升级至 head，记录每步版本和 Schema 差异；执行允许的 downgrade/upgrade 烟雾测试。

预期：不存在跳过、分叉或多 head；每一步可重复部署；已有数据在兼容升级中不丢失；失败事务不会留下半迁移状态。

## G3-03 关系完整性

前置：head Schema 和最小有效夹具。

步骤：查询全部外键孤儿；尝试写入跨文档 analysis selection、错误审核 case 授权、错误对象域引用、孤立关系和不匹配证据定位。

预期：全库外键孤儿数为 0；所有非法写入由数据库约束拒绝；合法最小夹具可提交。

## G3-04 原始对象哈希一致

前置：空 raw bucket 与空 `core.stored_objects`。

步骤：通过对象存储适配器上传固定二进制，指定预期 SHA-256；下载并重新计算哈希；核对 bucket、object key、byte length、media type 和登记行。

预期：上传、下载与登记 SHA-256 完全一致；key 仅由域和哈希构造且不含来源标题、URL query 或密钥；篡改预期哈希时操作失败且不登记成功对象。

## G3-05 重复对象去重

前置：G3-04 已有对象。

步骤：以同域、同内容重复上传两次，并由两个不同 artifact version 引用；再以不同域上传相同内容。

预期：同域同哈希只存在一个物理对象和一条 `stored_objects` 登记；不同业务版本可复用同一对象 ID；不同域不错误复用；重复调用返回稳定结果且不产生无意义副本。

## G3-06 public 最小权限

前置：head Schema、完整角色与最小内部/公开夹具。

步骤：以 `uap_public_reader` 连接并访问五个 Schema、对象登记、原始 artifact、提取正文、Prompt、模型 I/O 与公开投影；以 `uap_worker` 尝试写 public；以 `uap_backup` 尝试修改数据。

预期：public reader 只能读取 `public` 投影，访问 `ingest/core/ops/audit` 均 permission denied；无法读取 raw 或 model-io 对象；worker 无 public 权限；backup 可读五个 Schema 但不能写；普通业务角色不能更新/删除审计事件。

## G3-07 独立备份恢复

前置：源实例含五个 Schema 最小夹具和 raw/derived/model-io 对象；目标实例与目标对象存储为空且隔离。

步骤：执行自动备份；验证备份元数据和 SHA-256；恢复到独立 PostgreSQL 与独立对象存储；通过 `ingest.artifact_versions.stored_object_id -> core.stored_objects.id` 关联核对对象清单；运行孤儿、行数、Schema、对象内容哈希和权限检查。

预期：数据库和对象全部可恢复；五个 Schema 行数与约束一致；对象清单无缺失、无多余且哈希一致；外键孤儿数为 0；恢复过程不修改源实例；损坏备份或缺失对象必须失败关闭。

## 通过规则

G3-01 至 G3-07 必须全部通过，且 required CI gate 成功、证据清单完整。任何一项失败即登记 G3 不通过并保持 WP4 门禁关闭。

