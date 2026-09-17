# WP10 实施任务书（设计冻结候选）

当前状态：WP10.1–WP10.5 均有项目负责人签署记录。WP10.6 的 R1 修复已独立验收并固定为提交
`64306280d5c2cd8eb5eef2977269a12e7297b8e2`；C/G10-27 已独立 live 验收并固定为提交
`40044f48cd92e7748f88fedde38108c3dd176f65`；D 材料已准备并通过独立材料审核。G10-28/29 尚未整体关闭，
`G10-GATE-10.6` 未签署，WP11 保持关闭。

- 实施编号：`WP10-IMPL-20260828-01`
- 建议冻结号：`G10-FROZEN-20260828-01`
- 设计起点：最终签署的 `G10_DESIGN_SHA`
- 阶段签署记录：WP10.1 `8b6ae4fc6e0b46e1cf724742ded6a3db70d7f3a5`；WP10.2 `2718ba2dc8f6773e088f5cfb8cfe90d5070aa7ae`；WP10.3 `10773f135ef5fca236db2eebee75fd6b15f3cd0f`；WP10.4 `19ee4f436179740c002f05f597e456d42b1a17db`；WP10.5 `4e15bdd8cdb92d4406cc46b38f8cef92320a1881`。
- 历史限制：当前恢复仓库以 `34c57bcadfeb67053c4c47f8cde237a3af185ba8` 为 shallow boundary，更早签署 Git 对象缺失；签署记录存在不等于完整祖先链已恢复或已独立验证，历史恢复状态仍为 `PARTIAL`。

## 1. 目标与非目标

目标：

1. 把 WP9 grant 冻结成不可漂移的 publication manifest v2；
2. 以 `uap_publisher` 专用、可租约重放的权威路径生成 document/claim/evidence/entity/search 当前投影；
3. 交付只读 `/v1` API 和 PostgreSQL FTS；
4. 以 OIDC bearer token 把 WP9 case、decision/grant、selection、candidate、merge、manual claim 能力映射为受控管理 API；
5. 收紧登录角色裸 DML，并以 runtime probe/CI 证明隔离、幂等、回滚和性能。

非目标：

- relation extraction/materialization/review/grant/projection/API；
- 历史 SQLite 导入、双写、切流或旧系统回切（WP11+）；
- frontend、审核 UI、cookie session、CDN/public-assets release；
- sources/jobs/prompts/role-bindings 的通用管理 API；
- 外部搜索集群、向量搜索、推荐、分析聚合；
- 修改 WP1–WP9 文档或追认未实现能力为已交付。

## 2. 串行链和通用规则

```text
signed G10_DESIGN_SHA
  -> WP10.1 accepted SHA
  -> WP10.2 accepted SHA
  -> WP10.3 accepted SHA
  -> WP10.4 accepted SHA
  -> WP10.5 accepted SHA
  -> WP10.6 / G10 accepted SHA
```

每阶段：

1. 项目负责人给出上一门禁签署 SHA 和当前阶段口令；
2. 只实现当前阶段；migration → SQL/static tests → Python service → handler（如本阶段有）→ runtime probe 的顺序不得颠倒；
3. 创建普通追加提交和审核包后 STOP；
4. 独立审核不通过只整改本阶段，不得夹带下一阶段；
5. 后一阶段在前一门禁签署前保持关闭。

## 3. WP10.1：发布权威、manifest v2 与权限收口

- 合法起点：项目负责人指定的 signed `G10_DESIGN_SHA`。
- 范围：只追加 publication contract migration、manifest/identity/quarantine 与权限收口；Publisher 成功消费和 HTTP 仍关闭。
- 输入：0019 schema、WP9 active/superseded/withdrawn grants、`publication-outbox.v1`、G8-16C。
- 建议 migration：`0020_wp10_publication_contract`。

必须输出：

- document/claim/entity typed publication manifest 与 evidence 明细；
- stable public identity 映射表；
- `public.document_category`、`public.fact_status` 权威枚举；
- `publication-outbox.v2` 及 grant payload hash 的 canonical 规则；
- v1 active grant/outbox 的 quarantine/terminal 标记，不自动猜内容；
- 更新 `record_review_decision`：document approve/revise 必须携带 publication object；claim/entity 从决定事务完成后的内部状态捕获 manifest；
- `uap_api` 对 `core`/`ops` 裸 INSERT/UPDATE/DELETE 全撤销；`uap_publisher` 对 `public` 裸 DML 全撤销；默认权限同样收口；
- migration upgrade、空库 downgrade/upgrade、含状态 downgrade 拒绝探针。

权限边界：只给 `uap_api` 保留冻结的 `audit.*` 公共包装函数 EXECUTE；不授予 `core.merge_entities`。WP10.1 不提供 Publisher 消费成功路径。

禁止夹带：projector、HTTP、relation、CI/Makefile/workflow 接线和任何 WP10.2 对象。

停止线：G10-01–G10-05 通过、固定 SHA、STOP。不得写 projector、HTTP、relation、CI 接线。

## 4. WP10.2：document/entity Publisher

- 合法起点：`G10-GATE-10.1` 签署 SHA。
- 范围：只实现 document/entity 的专用 Publisher 租约、投影、失败和重放；claim/search/HTTP 仍关闭。
- 输入：v2 manifests/outbox、public 既有表、WP4 lease 模型。
- 建议 migration：`0021_wp10_publisher_projection`。

必须输出：

- `ops.claim_publication_outbox(text, integer, integer)`：只领 v2 publication event，忽略 terminal/v1；
- `ops.apply_publication_event(uuid, uuid)`：验证租约、event/grant/manifest/hash，在同一事务 upsert/delete projection 并 ack；
- `ops.fail_publication_event(...)`：可重试退避、次数上限与 terminal；
- immutable delivery-attempt 行：event/attempt/token/payload hash，使 apply/fail 的响应丢失重放与异 payload 冲突可证明；
- document/entity stable public id 和不可变 opaque slug；
- document/entity grant、revision、stale event、withdraw/revise/replay 规则；
- `platform/src/uap_platform/publishing/` service 与专用 Publisher loop；
- 进程崩溃、过期 token、同事件重放、同内容重建与恶意 payload 探针。

权限边界：`uap_publisher` 只可 EXECUTE 专用 claim/apply/fail；不能直接 ack publication event、不能裸写 public、不能执行审核函数。HTTP 仍关闭。

禁止夹带：claim/evidence/search 投影、HTTP route、relation 和下一阶段 migration。

停止线：G10-06–G10-10 通过、固定 SHA、STOP。不得投影 claim/search，不得注册 HTTP。

## 5. WP10.3：claim/evidence、关联、撤回和搜索投影

- 合法起点：`G10-GATE-10.2` 签署 SHA。
- 范围：只扩展当前投影到 claim/evidence/document_entities/search 及 rebuild；HTTP 和 relation 仍关闭。
- 输入：已验收 document/entity projector、claim manifests。
- 建议 migration：`0022_wp10_claim_search_projection`。

必须输出：

- claim + 至少一项 evidence 原子投影；
- manual/AI claim 的稳定 public ordinal，不复用 analysis ordinal 作为全局唯一；
- evidence excerpt/locator/source URL 的 manifest 快照；
- claim subject 对应的 `public.document_entities`，并在 claim/entity/document withdraw/revise 时重算；
- document withdraw 的依赖隐藏、republish 的 active-claim 重建；
- `public.search_documents` 与 document upsert/delete 同事务更新；
- 从全部 active v2 manifest 重建 public 当前投影的受控入口和校验报告。
- rebuild UUID/input digest/result digest 的不可变运行记录，防止同 command ID 漂移；

权限边界不变；relation 表保持零写入，`public.relations` 不投影。

禁止夹带：HTTP dependency/handler、relation manifest/materializer、外部搜索和下一阶段索引。

停止线：G10-11–G10-15 通过、固定 SHA、STOP。不得注册 HTTP、不得解除 G8-16C。

## 6. WP10.4：公开只读 HTTP 和查询

- 合法起点：`G10-GATE-10.3` 签署 SHA。
- 范围：只交付 public projection 上的匿名只读 HTTP、查询索引与可见性；Admin 写 HTTP 仍关闭。
- 输入：已验收 public 当前投影。
- 建议 migration：`0023_wp10_api_read_indexes`（只增 keyset/filter 查询索引）。

必须输出：

- 独立 Public API 进程，只配置 `uap_public_reader` pool；
- `GET /v1/documents`、detail、claim detail、entity list/detail、`/v1/search`；
- RFC 9457 风格 problem DTO、稳定 code、请求 ID；
- HMAC 签名 keyset cursor、固定排序、filter hash、limit 1–100；
- page 内只读事务一致性、跨页当前投影语义、可见性和 404 规则；
- 无内部字段泄漏、SQL 注入、坏 cursor、容量和 p95 探针。

权限边界：Public API 进程不得持有 `uap_api`/Publisher/Worker URL；`/v1/relations` 不注册。

禁止夹带：Admin/OIDC 写 route、cookie session、core 查询兜底、relation 和 WP10.5 依赖。

停止线：G10-16–G10-20 通过、固定 SHA、STOP。不得注册 admin API。

## 7. WP10.5：OIDC 管理 HTTP 与 WP9 能力

- 合法起点：`G10-GATE-10.4` 签署 SHA。
- 范围：只把冻结的 WP9/WP10 运维包装函数映射为 OIDC Admin API；不新增领域能力或独立 grant 入口。
- 输入：WP9 Python review services、0020 write semantics、OIDC issuer/audience/JWKS 配置。

必须输出：

- 独立 Admin API 进程，只配置 `uap_api` pool；
- 严格 bearer OIDC 校验：alg/iss/aud/exp/nbf/sub；不接受 cookie session；
- `(issuer,sub)` 只能解析已存在 active person principal，禁止请求内自动提权/自动 role binding；
- 每个写事务把 UUID `Idempotency-Key` 原样 `SET LOCAL uap.request_id`，principal 原样 `SET LOCAL uap.principal_id`；
- api-contract 中全部 admin read/write 路由；grant 只作为 decision 副作用和响应状态，不设独立 grant 写路由；
- `audit.requeue_publication_event` 的 data_operator 包装路径；
- W01–W11 每个实例的 S/P/M/R/C/T 六类验收。

权限边界：HTTP handler 不写表、不调用私有 `_apply_*`、不调用 `core.merge_entities`；每请求只调用一个公开写函数并在发送响应前 commit。

禁止夹带：独立 grant 写路由、通用 sources/jobs/roles 管理、cookie/bulk API、CI 总接线和 WP11。

停止线：G10-21–G10-24 与全部 W 矩阵通过、固定 SHA、STOP。不得开始 CI 总接线或 WP11。

## 8. WP10.6：总探针、CI、性能与签署

- 合法起点：`G10-GATE-10.5` 签署 SHA。
- 范围：只做全链回归、性能/灾备证据、validator/CI 接线与签署包；不新增 schema/route/领域能力。
- 输入：WP10.1–10.5 全部签署 SHA 和证据。
- 新领域 migration：无。

必须输出：

- `wp10_runtime_probe.py` 按 WP3→…→WP10 顺序执行；
- `validate_wp10.py`、migration-chain head、Makefile 和 workflow 接线；
- 所有 G10 与 G8-16C/G9 权限/幂等关键回归；
- 10 万公开文档样本的目标查询计划和 p95 测量；
- quarantine、dead event、rebuild、撤回传播、灾难恢复演练证据；
- 完整实现审核包、内外层 SHA-256 和 required CI 结论。

权限边界：不得新增或扩大任何登录角色 grant；所有 runtime probe 必须用真实最小权限角色，不能用 owner 结果替代。

禁止夹带：新 migration、route、依赖、领域功能、relation、legacy 切流和任何 WP11 动作。

停止线：G10-25–G10-29 及全量复跑通过后，只能提交 G10 签署候选并 STOP。签署不会自动授权 WP11。

## 9. 全阶段禁止夹带

- 修改 0001–0019 或 docs/wp1–wp9；
- 登录角色裸写 `public` 或恢复 `uap_api` 底层 DML；
- 直接授予 `core.merge_entities` / reverse；
- 从最新 analysis/grant 猜 publication 内容；
- no-op succeeded relation handler；
- 直接 ack 未投影 publication event；
- 在 HTTP 返回 raw/derived/model-io 内容、object key、Prompt、provider 响应、token/cost 或未净化数据库错误；
- 通过降低测试、mypy、ruff、Bandit、secret scan 或 coverage 过门；
- push、PR、CI 或下一阶段动作超出当前授权。
