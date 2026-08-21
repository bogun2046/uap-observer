# WP4 持久化任务与事务 Outbox

状态：G4 已通过并完成归档；WP5 门禁开启

- 工作包编号：`WP4-IMPL-20260814-01`
- 冻结标准：`G4-FROZEN-20260814-01`
- 前置门禁：G3 已通过（`G3-ACCEPT-20260814-01`）

## 范围

- 冻结 `ops.jobs` 的幂等入队、优先级领取和有期限租约。
- 冻结普通 Worker 与 Publisher 的 job-type 白名单和数据库权限边界。
- 追加 `ops.job_attempts`，区分成功、可重试失败、终态失败和取消。
- 对 408、429、5xx、超时与授权失败执行不同的重试分类；超过上限进入 `ops.dead_letters`。
- 提供死信显式重入队，并保留原尝试和解决记录。
- 在同一 PostgreSQL 事务中写入 `ops.outbox_events`，以 Publisher 租约分发、确认和失败退避。

## 不做事项

- 不实现采集、提取、AI、审核或公开发布业务处理器。
- 不引入进程内队列或外部消息代理作为 WP4 的权威队列。
- 不改变 G3 已验收的 0001—0004 迁移；WP4 只追加线性 revision `0005_durable_jobs`。

## 交付导航

- 实现单：`implementation-ticket.md`
- 独立验收单：`acceptance-ticket.md`
- 冻结用例：`acceptance-cases.md`
- 开发自检：`development-self-review.md`
