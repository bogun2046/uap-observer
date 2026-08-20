# G3 首轮独立验收不通过记录

- 记录编号：`G3-REJECT-20260813-01`
- 冻结标准：`G3-FROZEN-20260812-01`
- 待验版本：`67106b5f77b8f69140b03b3dda82696311a81e73`
- Draft PR：`https://github.com/bogun2046/uap-observer/pull/26`
- required run：`31621218347`（四个 job 成功，但不足以覆盖本轮语义缺陷）
- 验收日期：2026-08-13（Asia/Shanghai）
- 结论：独立复验不通过，待整改
- 门禁：PR 保持 Draft；WP4 继续关闭

## 用例结论

| 用例 | 结论 |
|---|---|
| G3-01 空库迁移与字典一致性 | 不通过 |
| G3-02 顺序升级、幂等、downgrade smoke | 通过 |
| G3-03 关系完整性与合法夹具 | 不通过 |
| G3-04 实际对象哈希 | 通过 |
| G3-05 同域去重、跨域隔离 | 通过 |
| G3-06 public/worker/backup 权限边界 | 通过 |
| G3-07 独立联合恢复 | 通过 |
| 证据清单完整性 | 不通过 |

## 阻断缺陷

1. `G3-R1-D01`（P0）：公开 Claim 的延迟证据触发函数访问错误行型字段，合法 Claim + evidence 事务无法提交。
2. `G3-R1-D02`（P1）：部署窗口结束后 `uap_migrator` 仍可登录并切换到 `uap_owner`。
3. `G3-R1-D03`（P1）：`core.evidence_spans` 接受 locator 类型与区间字段混用。
4. `G3-R1-D04`（P1）：`public.document_entities` 未校验 Claim 与 Relation 的发布 revision 一致。
5. `G3-R1-D05`（P2）：交付证据清单遗漏功能性变更文件 `platform/alembic/env.py`。

## 已确认通过且整改不得回退

- 26/26 单元测试、91.86% 覆盖率、Ruff、mypy、23/23 WP2 检查及 7/7 WP3 静态检查。
- 五个 Schema、49 张业务表、108 个外键、孤儿数 0。
- 对象实际哈希、同域去重、跨域隔离、角色权限边界及独立联合恢复。
- 损坏数据库 dump 和缺失对象均失败关闭。

整改必须形成新 commit、新 required run 和新完整清单，再提交 `G3-R2`；本记录永久保留，不由后续通过记录覆盖。
