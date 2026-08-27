# WP6 文档提取与版本化

- 工作包编号：`WP6-IMPL-20260819-01`
- 冻结标准：`G6-FROZEN-20260819-01`
- 前置门禁：G5 已通过，WP6 已开启
- 当前状态：实现自检完成，待 required CI 与独立验收
- 后置门禁：WP7 关闭

## 目标

从 G5 已登记的原始对象生成可追溯、可重复和可追加的正文提取结果。提取器只读取对象登记与文档版本，不直接访问外部 URL，不写入 `public`、Claim、Entity 或审核表。

## 范围

- 复用 `extract_document` durable job 和 `ops.job_attempts` 租约，不建立第二套队列。
- 从 `ingest.artifact_versions.stored_object_id` 读取 raw 对象，经 `object_registry` 将派生正文写入 `derived` 域。
- 以 `core.document_versions` 为输入，以 `core.extractions` 为追加式输出。
- 首个端到端切片覆盖 HTML、PDF 和字幕（WebVTT/SRT）三类对象；每类均使用固定离线夹具。
- 输出标题、作者、语言、来源日期、规范化正文、输出哈希和 `location_map`。

## 不做事项

- 不调用外部 URL，不实现来源采集和重定向跟随。
- 不修改既有 `document_versions`，不覆盖成功提取，不删除历史 extraction。
- 不调用模型、不生成 Claim/Entity、不执行审核或公开发布。
- 不把正文、原始对象或错误摘要写入日志。

## 交付导航

- 实现单：`implementation-ticket.md`
- 冻结用例：`acceptance-cases.md`
- 独立验收单：`acceptance-ticket.md`
