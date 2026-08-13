# WP3 开发自检报告

- 实现编号：`WP3-IMPL-20260812-01`
- 冻结标准：`G3-FROZEN-20260812-01`
- 日期：2026-08-13（Asia/Shanghai）
- 结论：G3-R2 整改开发自检通过，提交独立复验；WP4 门禁保持关闭。

## G3 用例自检

| 用例 | 开发自检结果 | 证据摘要 |
|---|---|---|
| G3-01 空数据库迁移 | 通过 | 第二套最终空 volume 升级至唯一 head `0004_g3_semantic_repairs`；五个 Schema、49 张业务表成立；重复 upgrade 无变化；迁移后 migrator 为 `NOLOGIN/NOINHERIT`。 |
| G3-02 顺序升级 | 通过 | 最终代码在独立临时库执行 0001→0002→0003→0004；重复 head 和 0004→0002→0004 烟雾通过且夹具保留；故障注入非零退出并关闭 migrator。 |
| G3-03 关系完整性 | 通过 | 108 个外键孤儿为 0；合法 Claim + evidence、合法 locator 及同 revision 关联可提交；混合 locator、孤立 Claim、删除最后 evidence、跨 revision 关联及破坏性 revision 更新均返回 `23514`。 |
| G3-04 对象哈希 | 通过 | 固定对象 SHA-256 为 `07c2de5fcc5a23d7e0161ad62c72df33a94b5e44373f5970a19738ba29159e9c`；写入、读回和登记一致。 |
| G3-05 去重 | 通过 | 同域两次登记复用同一 ID；两个 artifact version 共享一条登记；跨域内容不复用。 |
| G3-06 最小权限 | 通过 | public reader 访问 core、worker 写 public 均返回 `42501`；backup 写入返回只读事务 `25006`；app 实际以 `uap_api` 运行且不持有其余角色密码。 |
| G3-07 独立恢复 | 通过 | 最终源环境恢复至第三套独立 PostgreSQL/Object Store volume；2 个对象精确核验；源/目标 50 张用户表统计逐项一致；目标为 0004 且 migrator 为 NOLOGIN。首轮已确认的损坏 dump 和缺失对象失败关闭未回退。 |

## 质量结果

- Ruff：通过（`src tests tools alembic`）。
- mypy strict：20 个源文件通过。
- pytest：28/28 通过，覆盖率 91.86%。
- WP2 回归策略：23/23 通过。
- WP3 静态契约：9/9 通过。
- Shell 语法：备份、恢复和迁移链脚本通过 `sh -n`。

## 安全边界

- 迁移和角色密码配置只在一次性初始化容器中运行；长期 app 使用 `uap_api`。
- `uap_migrator` 为 `NOINHERIT`，只在部署窗口临时 `LOGIN`；正常、重复和迁移失败路径均实测恢复 `NOLOGIN`。
- public reader 仅有 public Schema SELECT，未分配对象存储凭据。
- 对象 key 仅包含存储域和 SHA-256，不包含来源标题、URL query 或凭据。
- 备份使用只读 `uap_backup`；恢复先在单事务内恢复数据库，再写对象，最终执行精确对象集合和哈希校验。

本报告仅是实现人员开发证据，不构成 G3 独立验收通过结论。
