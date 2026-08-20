# G3 正式验收记录

- 验收编号：`G3-ACCEPT-20260814-01`
- 冻结标准：`G3-FROZEN-20260812-01`
- 对应整改：`G3-R2-REMEDIATION-20260813-01`
- 验收基准提交：`04c234cb955d0594f05a27eaeb8ca7550e957566`
- required CI：[#31686094348](https://github.com/bogun2046/uap-observer/actions/runs/31686094348)
- 完整证据清单：`artifacts/wp3-engineering-20260813-r2/MANIFEST.sha256`
- 清单 SHA-256：`5ccd167750488028a1e828f0ad9bf74311be5443e3bd7ffc8bc529665aeb66d5`
- 验收日期：2026-08-14（Asia/Shanghai）
- 验收责任：数据库负责人 + 安全负责人
- 验收结论：通过，未发现阻断项
- 门禁结果：G3 正式通过；WP4 门禁开启

## 独立复验范围与结果

| 用例 | 结论 |
|---|---|
| G3-01 空数据库迁移与字典一致性 | 通过；唯一 head 为 `0004_g3_semantic_repairs`，5 个 Schema、49 张业务表、108 个外键，孤儿数 0 |
| G3-02 顺序升级、幂等与 downgrade smoke | 通过；迁移故障后 `uap_migrator` 为 `NOLOGIN/NOINHERIT` |
| G3-03 关系完整性与合法夹具 | 通过；五项历史缺陷均完成运行态正反验证 |
| G3-04 原始对象哈希 | 通过；对象写入、读回、登记和恢复哈希一致 |
| G3-05 同域去重与跨域隔离 | 通过；raw、derived、model-io 联合恢复成功 |
| G3-06 最小权限 | 通过；public reader、worker、backup 的实际连接权限符合冻结矩阵 |
| G3-07 独立联合恢复 | 通过；源、目标 49/49 表行数一致，对象哈希一致；损坏备份和缺失对象均非零失败 |
| G3-08 交付一致性与门禁 | 通过；39/39 清单校验通过，required CI 四个 job 全部成功，worktree 干净 |

## 质量与安全证据

- Ruff、mypy、28/28 测试、91.86% 覆盖率通过。
- 平台策略 23/23、WP3 静态检查 9/9 通过。
- public reader 仅可读 `public`；worker 不能写 `public/audit`；backup 五域可读但不可写。
- 本轮临时容器、volume、镜像和备份夹具已清理。

## 历史记录

首轮 G3 不通过记录及 G3-R2 整改记录永久保留；本记录仅登记独立复验后的正式通过结论，不覆盖历史失败事实。

后续 WP4 实施必须建立独立实现单和验收单，并继续遵循“实现与独立验收分离”的门禁规则。
