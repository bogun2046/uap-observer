# WP5 开发自检

状态：首轮 RSS 核心自检通过；WP5 尚未完成

实现完成后必须记录：

- Collector SDK 与 RSS adapter 的契约测试结果。
- 固定快照 SHA-256 及两次运行差异报告。
- source run、job、artifact 和 document 的追溯 SQL 报告。
- 304、空结果、403、429、超时、5xx 和畸形输入的正反结果。
- Ruff、mypy、全量测试、迁移检查和权限检查结果。
- 待验提交、证据清单逐项哈希及外层清单 SHA-256。

## 首轮自检结果

- Python 3.12.13 容器内 Ruff：通过。
- Python 3.12.13 容器内 mypy 严格检查：通过。
- 全量平台测试：34/34 通过。
- 当前全量平台测试：39/39 通过。
- 覆盖率：83.50%，高于项目 80% 门槛。
- RSS 契约与 source-run workflow 测试：11/11 通过。
- 已完成 source-run 生命周期与 PostgreSQL 持久化适配器的代码级接入；尚未完成真实 PostgreSQL 运行态验证。
- 限速、冷却、来源健康检查、版本化 payload 和固定快照哈希仍未完成，不能提交 G5 验收。
