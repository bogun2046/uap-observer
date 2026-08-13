# G3-R2 整改报告

- 整改编号：`G3-R2-REMEDIATION-20260813-01`
- 对应不通过记录：`G3-REJECT-20260813-01`
- 冻结标准：`G3-FROZEN-20260812-01`
- 状态：整改与开发自测完成，待独立复验；尚未形成独立验收结论
- 门禁：PR #26 保持 Draft；WP4 继续关闭

## 缺陷闭环设计

| 缺陷 | 整改 | 新增失败关闭证据 |
|---|---|---|
| G3-R1-D01 | 将公开 Claim 与 Claim evidence 的延迟约束拆为两个行型安全的触发函数 | 合法 Claim + evidence 同事务提交；孤立 Claim 及删除最后一条 evidence 均返回 `23514` |
| G3-R1-D02 | 部署脚本仅在迁移窗口启用 `uap_migrator LOGIN`，并从脚本入口注册 EXIT/HUP/INT/TERM 清理 | 部署完成后 `rolcanlogin=false`、`rolinherit=false`，实际角色登录失败 |
| G3-R1-D03 | 对每种 locator 类型明确要求全部无关区间字段为 NULL | text、pdf、video 合法记录提交；混合 PDF + char 区间返回 `23514` |
| G3-R1-D04 | 以可延迟触发器校验关联写入和后续 Claim/Relation revision 更新 | 同 revision 关联提交；跨 revision 关联及破坏性后续更新均返回 `23514` |
| G3-R1-D05 | 将 `platform/alembic/env.py` 纳入必需证据范围，并新增清单范围自检 | 证据生成器与静态验证器同时检查必需文件集合 |

## 迁移兼容策略

- 已发布的 `0001`、`0002`、`0003` revision 保持字节级不变。
- 所有数据库语义修复进入线性新 revision `0004_g3_semantic_repairs`。
- 迁移链覆盖空数据库、每个旧 revision 顺序升级、重复 head、`0004 -> 0002 -> 0004` downgrade smoke。
- 迁移器开关由管理员连接执行；业务迁移继续以 `uap_migrator` 登录并显式 `SET ROLE uap_owner`。

## 开发自测结果

- 最终代码在第二套全新 volume 一次升级至唯一 head `0004_g3_semantic_repairs`；五个 Schema、49 张业务表、108 个外键及 0 个孤儿成立。
- 合法公开 Claim + evidence 与合法同 revision document-entity 事务提交；五项整改反例均以 `23514` 失败关闭。
- migrator 正常部署、重复部署和人为迁移失败三条路径均恢复 `rolcanlogin=false`、`rolinherit=false`，实际登录失败。
- 最终迁移链完成 0001→0002→0003→0004、重复 head、0004→0002→0004；故障注入 revision 留在 0003。
- Ruff、mypy、28/28 pytest、91.86% 覆盖率、23/23 WP2 与 9/9 WP3 静态检查通过。
- 最终源环境联合备份恢复至第三套独立 volume；2 个对象精确核验；源/目标 50 张用户表统计逐项一致。
- 正式 staging 脚本连续执行两次，服务均 healthy，第二次 bucket 初始化 `created=[]`，最终 revision 为 0004、migrator 为 NOLOGIN。

## 待独立复验锚点

新 Git commit、GitHub required run 和完整清单 SHA-256 将在提交后写入最终证据。以上仅是实现人员开发证据，不构成 G3-R2 独立验收通过结论。
