# G4 冻结验收用例

冻结编号：`G4-FROZEN-20260814-01`
前置门禁：`G3-ACCEPT-20260814-01`

## G4-01 幂等入队

同一 `idempotency_key` 重复入队必须返回同一 job，不得产生第二条有效任务；不同 key 必须产生不同 job。

## G4-02 原子领取与租约

两个真实连接同时领取同一 job，只允许一个 attempt 成功；返回 lease token 和过期时间。错误 token、错误角色和过期租约完成任务必须失败。

## G4-03 失败分类与退避

408、429、5xx、timeout 属可重试失败；403/401 等授权失败属终态失败。可重试失败进入 `retry_wait` 并设置退避，终态失败直接进入 `dead`。

## G4-04 最大尝试与死信

达到 `max_attempts` 后任务进入 `dead`，保存最后 attempt 和 payload snapshot；显式重入队后进入 `queued`，原始 attempt 不被覆盖。

## G4-05 事务 Outbox

领域写入与 `emit_outbox` 在同一事务提交；回滚时二者均不可见。相同 event key 幂等；Publisher 独占领取、确认和失败退避。

## G4-06 Worker/Publisher 最小权限

普通 Worker 不能领取、入队或完成发布类任务；Publisher 不能领取、入队或完成普通 Worker 任务；普通 Worker 不得写 `public`。

## G4-07 恢复与可观测性

租约过期后可被重新领取；旧 attempt 记录 `lease_expired`；每次状态变化保留 attempt、错误码、摘要和恢复原因，不把状态写入业务主表。

G4-01 至 G4-07 必须全部通过，且 WP4 静态检查、required CI 和证据清单完整。任一失败即登记不通过并保持 WP5 门禁关闭。
