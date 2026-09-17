# V1-2 Contract Freeze — Scope

状态：Contract Freeze 草案，基于 V1-1 snapshot `1656441ac73f15e51387e2811c63667a6bbddc78`。

V1-1 的 accepted code baseline 仍为 `4a14b368c96808edf9dfbc1dfd5fc0ce9fbf6638`，本文件不重新打开 V1-1，也不修改其归档分支、tag 或验收材料。

## 目标

让一名被明确绑定的资料管理员可以在 Internal Library 完成：查看 → 编辑 → 保存 → 单任务重分析（可选）→ 删除到回收站 → 恢复，并对每个动作保留完整审计。所有数据仍是内部资料，`public_authorized=false`，不产生 publication grant。

## V1-2 范围

### 内部可用能力

- 文档详情：RAW/extraction、原文、AI_RESULT、证据、模型运行、当前 EDITORIAL 和内部状态。
- 编辑标题、中文摘要、bullets、分类、labels，以及 claims/entities 的允许字段和引用关系。
- 保存为新的 EDITORIAL revision；刷新后仍可见，旧 revision 可审计。
- 对 classification、summary、claim extraction、entity extraction 单独重分析；沿用 active prompt/model、预算、lease/idempotency 和旧结果保留规则。
- 软删除到回收站、回收站列表、恢复；恢复回内部状态，不自动公开或重分析。
- 文档级审计历史和每次写入的 request-id 幂等。

### 明确不在 V1-2

公开发布、publication grant UI、Public website/search、Event 编目与自动合并、Person/Organization 独立页面、Relation/Graph、多人审核、永久删除、WAR.GOV 或新 source、Elasticsearch、高级 observability、历史迁移、WP11。

## 能力层边界

- RAW：复用现有 `core.documents`、`core.document_versions`、ingest/artifact/stored-object、`core.extractions`；只读。
- AI_RESULT：复用 `core.analysis_results`、`ops.model_runs`、`ops.prompt_versions`、evidence spans 和既有选择/物化表；追加新结果，不覆盖旧结果。
- EDITORIAL：新增最小的 append-only revision 层，引用现有 document version、analysis result、claim/entity/evidence ID；不重建实体、关系或模型结果体系。
- Public：V1-2 不创建 grant、manifest 或 projection 行。

## 顺序、依赖与完成条件

1. **V1-2.0 Contract Freeze**：本目录七份文档审查通过，角色、字段、状态、API 和 DoD 获负责人确认。
2. **V1-2.1 数据与权限底座**：完成 editorial revision/soft-delete migration、`editorial_admin` 最小角色、审计函数和并发约束；不改变 V1-1 数据。
3. **V1-2.2 管理 API**：详情、编辑/采用 AI、单任务重分析、trash/restore、trash list、audit history；所有写入有 OIDC、request-id、幂等和 409 并发冲突。
4. **V1-2.3 Internal Library UI**：复用现有 `v1/library.html`，增加编辑页和 Recycle Bin，不引入前端框架。
5. **V1-2.4 真实验收**：按 `V1-2-ACCEPTANCE.md` 的同一 analysis_ready Reddit 文档完成全链，且公开投影为空。

每一步依赖前一步的契约/迁移和回归测试；本轮不执行这些实施步骤。

## Track A / Track B 边界

- Track A 平台收口：既有 WP10 权限、OIDC、audit/session、jobs/lease/idempotency、model governance、publication boundary 保持不变；仅新增 V1-2 所需的窄角色/函数时按原安全模式扩展。
- Track B 产品开发：只实现内部编辑、单任务重分析、回收站、恢复及 UI/API。不得借 V1-2 引入 Public、Source、Graph、WP11 或通用知识平台。

## 待负责人确认

1. OIDC 真实 `(issuer, subject)` 仍在接入时绑定；是否采用新增 `editorial_admin` enum（推荐）而不复用 reviewer 权限。
2. 标题/摘要/bullets/labels 的最终长度上限和中文编辑规则。
3. 回收站是否需要显示删除原因及保留期（本阶段不做自动永久删除）。
4. 被删除文档是否禁止任何 reanalysis（推荐禁止，恢复后再由管理员主动触发）。
5. “采用 AI 建议”是按单字段采用还是按任务结果采用（推荐按字段/任务显式选择）。
