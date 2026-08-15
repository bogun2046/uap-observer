# WP4 独立验收单

- 验收编号：`WP4-ACCEPT-20260814-01`
- 冻结标准：`G4-FROZEN-20260814-01`
- 状态：G4 通过，已完成归档
- 验收责任：运行时负责人 + 安全负责人
- 门禁：WP5 开启

## 独立性要求

- 使用全新 PostgreSQL volume，并从 G3 已验收基线升级到 WP4 head。
- 至少两个真实数据库连接并发领取同一批任务，确认无重复 attempt/租约。
- 使用真实 `uap_worker`、`uap_scheduler`、`uap_publisher` 连接验证白名单和拒绝路径。
- 通过人为超时、408、429、403、5xx 和超过最大尝试次数验证状态及死信。
- 独立核对同事务领域写入与 Outbox 事件、事件幂等和 Publisher lease token。

## 最终记录字段

验收编号、测试环境、基线提交、迁移版本、逐项操作、预期/实际结果、SQLSTATE、日志、清单哈希、缺陷编号、验收人角色、时间和结论。

## 最终验收记录

- 验收状态：通过
- 待验提交：`add1b9505012a0c7d40831bd31e1dea4a6dc6fef`
- PR：[#27](https://github.com/bogun2046/uap-observer/pull/27)，Draft
- Required CI：[run 31896777355](https://github.com/bogun2046/uap-observer/actions/runs/31896777355)
- Required job：`quality`、`security`、`integration`、`gate` 全部成功
- WP4 运行态、迁移链、失败关闭和安全扫描：通过
- 证据清单：29/29 校验通过
- 清单：29/29 校验通过；最终 SHA-256 以 `artifacts/wp4-engineering-20260814/MANIFEST.sha256` 及其外层锚点为准
- 结论：G4 通过，WP5 门禁开启
