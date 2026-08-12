# WP2 独立验收单

- 验收编号：`WP2-ACCEPT-20260812-01`
- 冻结标准：`G2-FROZEN-20260812-01`
- 状态：独立复验通过；G2 已关闭，WP3 门禁开启
- 验收责任：开发负责人 + DevOps
- 实现人员限制：只能提交开发自检和证据，不得填写最终“通过”结论

首轮及第二轮不通过结论永久保留；最终通过结论记录于
`g2-acceptance-record.md`。后续追加提交必须重新通过 required `gate`。

## 验收输入

- `platform/` 工程及锁文件
- Docker Compose 开发与 staging 配置
- `.github/workflows/platform-ci.yml`
- `docs/wp2/staging-deployment.md`
- WP2 开发自检报告和证据清单

## 最终记录字段

- 测试环境与代码版本
- 前置数据
- 操作步骤
- 预期结果
- 实际结果
- 日志、容器状态、扫描报告和 SQL/对象存储检查
- 缺陷编号
- 验收人、角色、时间和结论
