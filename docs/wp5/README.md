# WP5 统一采集器插件

状态：实现中；RSS 与 source-run 持久化已完成，限速/冷却和健康检查仍待实现，G5 待验收

- 工作包编号：`WP5-IMPL-20260816-01`
- 冻结标准：`G5-FROZEN-20260816-01`
- 前置门禁：G4 已通过（`WP4-ACCEPT-20260814-01`）
- 当前门禁：WP6 关闭

## 范围

- 定义 Collector SDK：`fetch`、`parse`、`normalize`、`persist` 四个边界。
- 以一个 RSS 来源作为端到端试点，使用固定离线快照完成确定性验证。
- 记录 `ingest.sources`、`ingest.source_config_versions` 和 `ingest.source_runs`。
- 通过 WP4 持久化任务队列执行采集，使用幂等键避免重复任务和重复条目。
- 实现 URL 规范化、来源条目 ID 规范化、限速、冷却和来源健康检查。
- 为 304、空结果、403、429、超时和 5xx 建立统一结果分类。

## 不做事项

- 不实现网页、X、YouTube 或 API 来源；它们留到后续扩展。
- 不实现正文提取、文档版本生成或 PDF/字幕处理；这些属于 WP6。
- 不调用模型、不生成 Claim/Entity；这些属于 WP7/WP8。
- 不改变公开读模型、审核流程或站点 API。
- 不绕过 `ops.jobs` 直接使用进程内队列或外部消息代理。

## 交付导航

- 实现单：`implementation-ticket.md`
- 独立验收单：`acceptance-ticket.md`
- 冻结用例：`acceptance-cases.md`
- 开发自检：`development-self-review.md`
