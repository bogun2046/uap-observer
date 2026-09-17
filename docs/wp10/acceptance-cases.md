# G10 冻结验收用例

所有能力在对应门禁前为 planned/closed。每条 runtime 用例必须保存真实 SQLSTATE、稳定 code、HTTP status、request_id、相关 ID 和 before/after digest。

## WP10.1：authority 与 manifest

### G10-01 基线、线性 migration 与 docs-only 起点

证明 WP10.1 从 signed `G10_DESIGN_SHA` 开始，0020 单一 head，0001–0019 hash 不变；设计候选相对 `8a956f3` 仅 `docs/wp10/**`。

### G10-02 WP1 草案差异与权威枚举

对 OpenAPI 旧值逐项负测：`document_version/candidate/in_review/place/document/topic/characters/page/source_action/official_record(assertion)` 均不得静默转换。实际 DB/API enum 正向通过；document category/fact status 仅接受本设计注册值。

### G10-03 登录角色 DML/EXECUTE 收口

真实 `uap_api` 对所有 core/ops 和 review/grant/public 写表 INSERT/UPDATE/DELETE 均 `42501`；Publisher 对 public 裸 DML `42501`；Worker/Publisher/public reader 不能执行 WP9 review 函数；所有登录角色不能执行 bottom merge/reverse。允许函数仍可成功。

### G10-04 typed manifest、hash 与决定原子性

document approve/revise 携带合法 publication，claim/entity 捕获 post-change 状态。验证 decision、grant、manifest、manifest evidence、outbox v2、audit event 同事务；manifest canonical hash=grant hash=outbox hash。未知字段、错误 basis、无 URL、无 evidence、超长 excerpt 全部稳定拒绝且零写入。

### G10-05 legacy quarantine、upgrade/downgrade fail-closed

构造 active v1 grants/outbox：upgrade 保留历史、写 quarantine/terminal、不写 public、不标 published；合法 revise 生成 v2 并解决 quarantine。Claim 在没有 active v2 document grant/manifest 时 approve/revise 以 `publication_document_grant_required` 全回滚。空状态 downgrade/upgrade 成功；含 v2/quarantine/terminal/public 状态 downgrade 以 `publication_contract_state_blocks_downgrade` 在任何删除前拒绝。

## WP10.2：document/entity Publisher

### G10-06 专用领取与 lease

两个 Publisher 连接并发 claim 同 event 只有一个 token/attempt；过期/错误 token apply/fail 为 `publication_lease_lost`/`40001`。非 Publisher、v1、terminal、relation、未知 event 不进入 claim 集合；空 dispatcher、limit 0/101、lease 0/86401 稳定拒绝且零 lease/attempt 变化。丢失 claim 响应只能等 lease 到期，不能用 dispatcher 参数伪装业务幂等重放。

### G10-07 Document grant/revise/withdraw

首次 v2 grant 创建 stable public ID/slug、revision 1；revise 复用 ID/slug、更新 grant/revision、保留 published_at 并设置 revised_at；stale event 不降级；withdraw 只删除匹配当前 grant。所有 projection 与 event ack 同事务。

### G10-08 Entity grant/revise/withdraw

同构验证 active canonical entity manifest；同名实体保持不同 public ID；merged/retired/disputed input 不投影；withdraw 清关联后删除 entity。

### G10-09 G8-16C 与 relation 多层关闭

正常 analysis 不入队 resolve_relations；误领仍 dead/terminal_failure/`knowledge_relation_task_not_in_wp8` 且 core.relations 零写。relation review仍拒绝；Publisher relation event terminal且 public.relations 零写；不得 no-op ack。

### G10-10 apply+ack 崩溃与重放

在 attempt、identity、public upsert、deferred constraint、ack 前分别注入失败：全部回滚，event可重领。同 lease token 的 apply 或 fail 同 operation payload 重放返回首次结果；同 token 改 op/error/retry/terminal 为 `publication_delivery_attempt_conflict`；新 attempt 后旧 token 拒绝。apply 提交后进程崩溃再重放 no-op success，不新增 identity/public 行、不改变 slug/revision、不重复 ack。hash/aggregate tamper terminal 且 public 零变化。

## WP10.3：claim/evidence/关联/search

### G10-11 Claim/evidence 成功与负例

active document 下发布 AI claim 和 manual claim；每个 public claim 至少一 evidence，identity/ordinal稳定，locator/source snapshot 正确。document 不可见→retry；空 evidence、跨文档 span、hash mismatch、最后 evidence 删除→事务拒绝。

### G10-12 Revision 与并发 ordinal

同 document 多 analysis 重复 internal ordinal、manual NULL ordinal、两个并发首次 claim publish：public display ordinal 唯一且稳定，无 deadlock。claim revise 原子替换 fields/evidence，重放无重复。

### G10-13 Withdraw、document cascade 与 republish

claim withdraw 删除 joins/claim/orphan evidence/link/search 文本；document withdraw 隐藏所有 dependent claims/search 但不改 active claim grants；document republish 复用 ID/slug并从 active v2 manifests 恢复 claims。撤回/恢复中 public reader永不看到半状态。

### G10-14 document_entities 可见性

已公开 claim 绑定已公开 entity 时产生同 document evidence basis；entity 尚未公开不泄露 internal ID，后续 entity event补链；claim/entity/document withdraw正确清链；revision mismatch constraint 保持。

### G10-15 Search 原子性与重建

document/claim publish/revise/withdraw 同事务重算 search vector/display/facets；不索引 evidence/full body/raw/model内容。active v2 manifests完整重建与在线 projection 内容 digest一致；同 rebuild UUID/同 input digest 重放同报告，同 UUID/异 digest 为 `publication_rebuild_id_conflict`；`uap_api`/Publisher 调用 rebuild 为 42501；tamper/missing manifest 全回滚 `publication_rebuild_mismatch`，run 记 failed 但 public/staging 全不替换。

## WP10.4：Public HTTP

### G10-16 路径与 DTO

六个开放 GET 的正常/空/404/validation 响应符合 api-contract；公开 DTO 不出现禁止字段。EntityDetail 无 relations 字段；`/v1/relations` 明确 404 `api_capability_closed`。

### G10-17 Keyset cursor 与当前投影一致性

对 documents/entities/search 执行多页、并发 insert/revise/withdraw；无因新插入造成的重复，固定排序/tie-breaker成立。篡改签名、跨 route、改 filter、坏版本、超长 cursor 均 400 `api_cursor_invalid`。

### G10-18 Search query/facets/排序

中英文黄金查询、category/fact status filter、相同 rank tie-breaker、q 1/2/200/201 边界；参数化 SQL抵御注入。只返回 public.search_documents 可见结果，撤回在同事务消失。

### G10-19 Public reader 与无泄漏

真实 Public API 环境只含 public reader URL；对 ingest/core/ops/audit SELECT/USAGE、所有函数、public DML 均拒绝。Problem/log/header 不泄漏 SQLSTATE、表/函数、reviewer、object key、raw/model内容、token/cost/error summary。

### G10-20 缓存与可见性

ETag/Cache-Control（若实现）不得让撤回内容超出冻结 max-age；不存在/未投影/withdrawn均同 404，不可枚举内部存在性。grant pending时 public仍404，apply commit后一次性可见。

## WP10.5：Admin HTTP

### G10-21 OIDC 与 principal 绑定

缺 token、坏签名、alg confusion、错 iss/aud、expired/nbf、无 sub均401；未知/inactive/service principal 403。token role claim不能替代 DB binding；revoked/scoped binding按WP9稳定码拒绝。request结束后 GUC不泄到复用连接。

### G10-22 Admin reads 与 grant 状态

reviewer/senior可读取冻结的 sanitized case/analysis/candidate/evidence/entity；data_operator只读 publication events。无 raw extracted body/model I/O。decision response/detail准确区分 grant committed、projection pending/visible/blocked/withdrawn；无 standalone grant写路径。

### G10-23 HTTP error mapping 与事务卫生

error-codes 中每组至少一实例；未知 DB primary message统一 api_internal_error。异常后 connection rollback/idle，可服务下一请求；响应发送前 commit失败不得返回成功。只接受 bearer，不接受 cookie认证旁路。

### G10-24 底层函数和私有副作用不可达

从真实 HTTP 进程凭据直接调用 `core.merge_entities`、reverse、manifest capture、grant apply、outbox enqueue、public DML均42501；W08/W09包装成功，审计 actor来自 GUC。handler source不存在 bottom/private直接 SQL。

## 外部主体写操作六项矩阵（WP10.5）

每个 Wxx 必须分别执行：

- `S`：授权主体首次成功，恰好一个业务结果和 audit event；
- `P`：错误/缺角色被拒绝，零业务/audit/outbox变化；
- `M`：缺 Idempotency-Key/request_id → 400 `review_request_id_missing`，不得调用 DB写函数；
- `R`：同 UUID、完全相同 path/body/defaults → 200、同 resource ID，无新增行；
- `C`：同 UUID 改任一业务字段 → 409 `review_idempotency_payload_conflict`，旧状态不变；
- `T`：在领域写、audit、grant/manifest/outbox或 commit处注入失败 → 全事务回滚，之后同请求可成功。

| W | 操作 | C 必改字段（至少） | T 必观察表 |
|---|---|---|---|
| W01 | open case | case_type、subject_id、priority、reason | review_cases/audit_events |
| W02 | assign case | case_id、assignee_id | review_cases/audit_events |
| W03 | close case | case_id、reason | review_cases/audit_events |
| W04 | decision/grant | case_id、decision、reason、structured_changes/publication | decisions/case/grant/manifest/outbox/audit |
| W05 | analysis selection | analysis_result_id、reason | analysis_selections/audit |
| W06 | candidate accept | candidate_id、reason | entities/candidate/audit |
| W07 | candidate bind | candidate_id、entity_id、reason | candidate/audit |
| W08 | merge | source、target、reason | entities/merge_events/audit |
| W09 | reverse merge | merge_event_id、reason | entities/merge_events/audit |
| W10 | manual claim | document、text/type/status/attribution/span_ids | claims/evidence/audit |
| W11 | publication replay | event_id、reason | outbox/quarantine/audit |

固定实例 ID 为 `G10-W01-S`…`G10-W11-T`。六项中的任一缺失即 G10-GATE-10.5 不通过。

完整实例集合：

| 操作 | 必须存在的六个实例 |
|---|---|
| W01 | `G10-W01-S`、`G10-W01-P`、`G10-W01-M`、`G10-W01-R`、`G10-W01-C`、`G10-W01-T` |
| W02 | `G10-W02-S`、`G10-W02-P`、`G10-W02-M`、`G10-W02-R`、`G10-W02-C`、`G10-W02-T` |
| W03 | `G10-W03-S`、`G10-W03-P`、`G10-W03-M`、`G10-W03-R`、`G10-W03-C`、`G10-W03-T` |
| W04 | `G10-W04-S`、`G10-W04-P`、`G10-W04-M`、`G10-W04-R`、`G10-W04-C`、`G10-W04-T` |
| W05 | `G10-W05-S`、`G10-W05-P`、`G10-W05-M`、`G10-W05-R`、`G10-W05-C`、`G10-W05-T` |
| W06 | `G10-W06-S`、`G10-W06-P`、`G10-W06-M`、`G10-W06-R`、`G10-W06-C`、`G10-W06-T` |
| W07 | `G10-W07-S`、`G10-W07-P`、`G10-W07-M`、`G10-W07-R`、`G10-W07-C`、`G10-W07-T` |
| W08 | `G10-W08-S`、`G10-W08-P`、`G10-W08-M`、`G10-W08-R`、`G10-W08-C`、`G10-W08-T` |
| W09 | `G10-W09-S`、`G10-W09-P`、`G10-W09-M`、`G10-W09-R`、`G10-W09-C`、`G10-W09-T` |
| W10 | `G10-W10-S`、`G10-W10-P`、`G10-W10-M`、`G10-W10-R`、`G10-W10-C`、`G10-W10-T` |
| W11 | `G10-W11-S`、`G10-W11-P`、`G10-W11-M`、`G10-W11-R`、`G10-W11-C`、`G10-W11-T` |

W04 还必须覆盖 approve/revise/withdraw 的 document、claim、entity：同 request重放不新增 grant/manifest/outbox；并发不同 request revise保持 WP9串行双成功、revision严格递增、最终一个active。

## WP10.6：整体

### G10-25 全链、回归与 CI

从空库/升级库跑 WP3→WP10 probes、全部 unit/integration、mypy/ruff/Bandit/secret/pip audit/coverage、migration chain；G8-16C、G9-01–38关键权限/幂等全部保持。required CI四组全绿。

### G10-26 数据迁移与 downgrade 矩阵

空库、只有 v1、混合 v1/v2、active/superseded/withdrawn、pending/retry/terminal outbox、非空 public逐种 upgrade/downgrade。任何拒绝均在破坏前，失败后 schema/data/privilege digest不变。

### G10-27 容量、性能与新鲜度

冻结生成器产生10万公开文档及代表 claims/entities；缓存未命中 documents/detail p95≤300ms/p99≤800ms，search p95≤500ms/p99≤1200ms；正常负载 grant commit→visible p95≤2分钟。保存硬件/PG参数/样本seed/EXPLAIN和原始统计。

### G10-28 SHA、审核包与工作区

每阶段起点/父/HEAD第一父链正确；最终 diff范围符合任务；manifest与审核包内外SHA一致；`git status --short`为空；没有 amend/rebase/merge/force-push 证据。

### G10-29 G10完成边界和WP11关闭

自动/人工清单证明 relation、legacy SQLite migration、frontend/CDN、通用admin API、external search均未交付；文档/状态页只把已签阶段标 delivered。G10签署后STOP，无WP11分支/代码/迁移/CI动作。
