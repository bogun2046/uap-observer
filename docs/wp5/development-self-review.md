# WP5 开发自检

状态：WP5 实现自检通过；等待 Draft PR required CI 与独立验收

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
- 全量平台测试：59/59 通过。
- 覆盖率：83.98%，高于项目 80% 门槛。
- RSS、source-run workflow、持久化和传输测试：28/28 通过。
- 真实 PostgreSQL + 对象存储 R4 探针通过：失败事务与并发事务围绕同一内容地址交错执行后，数据库登记和物理对象均存在；一致性扫描成功删除手工制造的未登记 raw 对象。
- 真实 PostgreSQL R1 探针通过：source config 版本、`rss.v1`、快照 SHA-256、最近成功时间、连续失败计数和 cooldown 均正确落库；冷却期间未发出请求。
- 真实 PostgreSQL G5 运行态探针通过：跨来源配置引用返回 `23503`；成功采集后 `source_run=empty` 且任务为 `succeeded`；超时采集后任务进入 `retry_wait`。
- 租约跨越到期点的原子性探针通过：source run 最终更新与 `finish_job` 一起回滚，过期任务可重新领取并复用原 source run 完成重试。
- source run 重试追溯保护通过：同一 `job_id` 只能复用原 `source_id` 和 `source_config_version_id`，跨来源重标被拒绝且原记录保持不变。
- WP5 探针自包含验证通过；全新数据库可直接运行。按 required CI 的实际顺序 `WP3 → WP4 → WP5` 执行时，每次领取均严格命中本探针新建任务，未领取 WP4 遗留任务。
- source-run checkpoint 租约守卫通过：`0007_source_run_lease_guard` 以最小权限锁定并校验当前 fetch-source job/attempt；A 租约到期、B 重领完成后，A 迟到 checkpoint 返回 `40001`，B 的 source run 全字段保持不变。
- PostgreSQL 迁移链 `0001 → 0007`、重复升级、降级至 `0002` 后回升均通过，迁移窗口结束后 migrator 保持禁用。
- 限速、冷却、来源健康检查、版本化 payload、固定快照和扫描器幂等回归测试已完成；G5 证据清单已生成，Draft PR 和 required CI 尚未完成。
