# ADR-0005：模型运行与分析结果只追加

- 状态：Accepted for G1
- 日期：2026-08-11

## 背景

当前 `news.analysis_json`、模型字段和处理状态会把一次结果当成当前事实，模型或 Prompt 升级容易覆盖历史，难以审计费用和错误。

## 决策

每次 Provider 调用新增一条 `ops.model_runs`，每个通过/失败的结构化结果新增 `core.analysis_results`。`analysis_selections` 使用 `(analysis_result_id, document_version_id, result_type)` 复合外键，只能选择同文档、同任务类型结果；禁止更新旧结果内容。Claim 和关系保留 `origin_analysis_result_id`，实体候选保存在 `entity_candidates` 并记录 analysis result、文档版本、证据和最终 canonical entity 解析关系。

## 后果

- 可比较模型、Prompt、输入哈希、Token、费用、响应和校验。
- 存储量增加，需要生命周期和索引策略。
- AI 结果只是候选，必须经过审核和发布授权才能进入 `public`。

## 未采用方案

- 原位更新“当前分析”：失去复现和审计能力。
- 仅保存最终 JSON：无法解释模型、成本、错误和选择过程。
