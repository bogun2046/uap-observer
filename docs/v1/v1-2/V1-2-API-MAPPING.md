# V1-2 Contract Freeze — API Mapping

状态含义：**Existing** 可直接满足当前契约；**Partial** 已有底座但缺少 V1-2 语义/权限/DTO；**Missing** 当前没有实现。

| 能力 | 当前实现/证据 | 状态 | V1-2 最小接口方向 |
|---|---|---|---|
| GET document detail | `GET /internal/v1/documents/{id}`，`v1/server.py` + `InternalLibrary.get_document`；local bearer | Partial | 在 OIDC Admin API 提供同等详情，增加 editorial/lifecycle/audit 摘要 |
| 列表/搜索内部文档 | `GET /internal/v1/documents` | Partial | 复用查询，默认排除 trash，增加 `state=trash` 独立列表 |
| 查看 RAW/AI/evidence/provenance | Library 读取 extraction、analysis、model billing；Admin 有 analysis/evidence reads | Partial | 合并为文档详情，净化 raw I/O，明确只读字段 |
| PATCH/PUT editorial fields | 无；Admin handler 仅既有 review writes | Missing | `PUT /admin/v1/documents/{id}/editorial` + revision/If-Match + idempotency |
| claims 编辑 | `POST /admin/v1/claims/manual` 仅新增 manual claim；selection/decision 是 review 流程 | Partial | editorial overlay 保存/替换/撤回引用，不改 AI claim 原行 |
| entities 编辑 | candidate accept/bind/merge 已有；无文档级字段编辑 | Partial | editorial entity override；不自动 merge canonical entities |
| 单任务 reanalysis | `POST /internal/v1/documents/{id}/reanalyze` 已支持四任务、预算、active prompt、job enqueue、audit | Partial | OIDC Admin route；增加 trash guard、统一审计和 DTO；保留旧 local route 仅兼容窗口 |
| soft delete/trash | 无 documents lifecycle 字段和 route | Missing | `POST /admin/v1/documents/{id}/trash`、`GET /admin/v1/trash` |
| restore | 无 | Missing | `POST /admin/v1/documents/{id}/restore` |
| audit history | `audit.audit_events` append-only；现有 review/publication reads；无文档聚合 route | Partial | `GET /admin/v1/documents/{id}/audit`，只读分页、净化 metadata |
| OIDC editor auth | Admin API OIDC + role_bindings 已有；V1 local Library 使用 token | Partial | 窄 `editorial_admin` 绑定单 principal，禁止服务 principal |

## 写接口共通契约

沿用 WP10：`Authorization: Bearer`、OIDC issuer/sub → active person、`Idempotency-Key` 为 UUID、只写事务设置 `uap.principal_id` 和 `uap.request_id`、unknown fields 拒绝、Problem JSON、成功 200、并发冲突 409。新增 DB function 仍由 owner 持有、SECURITY DEFINER、固定 search_path、显式授权，不允许 API 裸写表。

建议的最小路径：

- `GET /admin/v1/documents/{id}`；
- `PUT /admin/v1/documents/{id}/editorial`；
- `POST /admin/v1/documents/{id}/editorial/adopt`；
- `POST /admin/v1/documents/{id}/reanalyze`；
- `POST /admin/v1/documents/{id}/trash`、`POST /admin/v1/documents/{id}/restore`；
- `GET /admin/v1/trash`、`GET /admin/v1/documents/{id}/audit`。

这些是 Contract Freeze 的接口方向，尚未注册路由。
