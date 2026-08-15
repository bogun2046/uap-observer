# WP3 独立验收单

- 验收编号：`WP3-ACCEPT-20260812-01`
- 冻结标准：`G3-FROZEN-20260812-01`
- 状态：G3 独立技术复验通过
- 验收责任：数据库负责人 + 安全负责人
- 门禁：WP4 开启

正式结论记录于 `g3-acceptance-record.md`。本记录采用验收基准提交
`04c234cb955d0594f05a27eaeb8ca7550e957566`、required run
`31686094348` 和清单 SHA-256
`5ccd167750488028a1e828f0ad9bf74311be5443e3bd7ffc8bc529665aeb66d5`。

## 验收输入

- 待验 Git commit、PR 和 required gate 结果。
- 完整 Alembic revision 链及生成的 SQL/Schema 清单。
- PostgreSQL 角色权限报告。
- 对象存储上传、重复上传、下载和哈希报告。
- 数据库与对象存储备份、恢复及跨介质核对报告。
- `artifacts/wp3-*` 的验证报告和 SHA-256 清单。

## 独立性要求

- 验收必须使用全新 PostgreSQL 与对象存储 volume，不复用开发自测状态。
- 恢复目标必须是与源环境隔离的独立实例和独立 bucket/volume。
- 权限测试必须实际连接 `uap_public_reader`、`uap_backup` 等角色，不能只检查 GRANT 文本。
- 哈希和孤儿检查必须以 SQL/对象存储实际结果为准。
- 开发人员不得填写最终通过结论。

## 最终记录字段

验收编号、测试环境、代码版本、前置数据、逐项步骤、预期结果、实际结果、SQL/日志/清单证据、缺陷编号、验收人角色、时间和结论。
