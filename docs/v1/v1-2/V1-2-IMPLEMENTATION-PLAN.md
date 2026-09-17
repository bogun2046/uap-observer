# V1-2 Contract Freeze — Implementation Plan

## 可直接复用

- `v1/library.py` 的列表、详情、object-store 校验读取、usage/budget 和单任务 reanalysis 编排。
- `v1/server.py` 的 Internal Library 响应结构作为兼容参考。
- `model_governance` 的 `ModelTaskType`、active prompt、model run、schema validation、usage/cost、retry/lease/idempotency。
- `core.analysis_results`、`ops.model_runs`、`ops.prompt_versions`、`core.evidence_spans`、`core.analysis_selections`、entity candidates/claims materialization。
- Admin OIDC、cursor/problem/Idempotency-Key、`review.session` GUC 和 append-only `audit.audit_events`。
- `v1/library.html` 作为最小 UI 外壳，不引入前端框架。

## 需要新增/修改（后续获授权实施）

### 必需 migration

现有 schema 无 EDITORIAL 字段、revision、document deleted state，不能仅靠查询表达。需要最小 migration：

1. `core.editorial_revisions`（append-only revision snapshot + source map/adopted result references + current uniqueness）；
2. `core.documents` lifecycle 字段或等价受控 lifecycle 表；
3. `editorial_admin` role（若负责人确认推荐方案）；
4. owner-owned editorial save/adopt/trash/restore/audit functions 与显式 grants；不授予 API/Worker 裸 DML。

不迁移/不复制 RAW、AI_RESULT、canonical entity registry、publication 表。

### 应用/API

- `platform/src/uap_platform/admin_api/contracts.py`：editorial DTO、revision、trash/audit response、严格长度/枚举。
- `admin_api/handler.py` / `service.py`：新读写路由、OIDC role check、If-Match/base revision、统一 Problem/Idempotency。
- `v1/library.py`：复合详情、默认隐藏 trash、受控 reanalysis guard；保留预算与现有 worker enqueue。
- `v1/server.py`：仅在兼容窗口保留 local read/reanalysis；不把 local bearer 当正式 OIDC 权限。
- `v1/library.html`：列表、详情/编辑、Recycle Bin、AI adoption、audit 摘要。

### 测试

DTO/schema unknown-field、role/function negatives、revision conflict、idempotent replay、editorial source map、AI_RESULT immutable、trash/restore、reanalysis race、budget/retry、公共投影空集、真实 summary reanalysis 一次；不新增通用验收框架。

## 最小实施顺序

1. 先落 migration/functions 和数据库负例；
2. 再做 Admin read/write contract 与 service；
3. 接 Library projection/UI；
4. 做并发/回收站/审计回归；
5. 以单一真实文档执行 `V1-2-ACCEPTANCE.md`；
6. 通过审查后才考虑部署或后续公开阶段。

## 粗略投入与风险

估算 4–6 个有效工作日：底座与权限 1–2 日，API/服务 1–2 日，UI 0.5–1 日，测试与真实验收 1 日。若负责人选择复用 reviewer 或改变 overlay 形态，需重新估算。

主要风险：editorial overlay 与现有 materialized claim/entity 的显示一致性、并发保存与重分析竞态、角色新增的 migration/权限回归、trash 过滤遗漏、AI adopt 的字段级 provenance。解决方式是先冻结上述关系与负例，再实施；不通过“兼容旧字段”或放宽权限解决。

## 本轮停止边界

本 Contract Freeze 不修改业务代码、schema/migration、prompt、provider、数据库、V1-1 archive、Public、WAR.GOV 或 WP11；不调用 DeepSeek、不启动 Worker/Scheduler/Library，不 push、不建 PR、不 merge main。
