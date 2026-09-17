# WP10 publication manifest、Outbox 与 public projection 契约

## 1. 权威链

```text
review decision
  -> active/withdrawn grant
  -> immutable typed manifest + publication_payload_sha256
  -> publication-outbox.v2
  -> leased Publisher apply
  -> public current projection + outbox ack (one transaction)
  -> uap_public_reader
```

任何一步缺失都 fail closed。grant 存在只表示允许发布；不表示 public 已可见。

## 2. Manifest v2

WP10.1 新增下列 planned 表，均由 `uap_owner` 持有，登录角色无 DML：

| 表 | 主/外键与字段 |
|---|---|
| `audit.document_publication_manifests` | `grant_id` PK/FK、`decision_id`、`document_version_id`、title/summary/category/fact_status、source snapshot、optional summary basis、`manifest_sha256`、`created_at` |
| `audit.claim_publication_manifests` | `grant_id` PK/FK、`decision_id`、`claim_id`、`document_version_id`、claim fields、`manifest_sha256`、`created_at` |
| `audit.claim_publication_manifest_evidence` | `(grant_id,ordinal)` PK、evidence span id、excerpt、locator fields/JSON/hash、source URL；1–20 行 |
| `audit.entity_publication_manifests` | `grant_id` PK/FK、`decision_id`、`entity_id`、entity fields、`manifest_sha256`、`created_at` |
| `audit.document_public_identities` | `document_id` PK/FK、`public_id` UNIQUE、`slug` UNIQUE、created_at |
| `audit.claim_public_identities` | `claim_id` PK/FK、`document_id`、`public_id` UNIQUE、`display_ordinal`、created_at；同 document ordinal UNIQUE |
| `audit.evidence_public_identities` | `evidence_span_id` PK/FK、`public_id` UNIQUE、created_at |
| `audit.entity_public_identities` | `entity_id` PK/FK、`public_id` UNIQUE、`slug` UNIQUE、created_at |
| `audit.publication_quarantine` | grant/event identity、`publication_manifest_required`、created/resolved timestamps；只追加解决记录 |

WP10.2 另增 `audit.publication_delivery_attempts`：`(event_id,attempt_no)` 主键、dispatcher、lease token hash、operation payload hash、outcome、sanitized code、started/finished timestamps。WP10.3 另增 `audit.publication_rebuild_runs`：`rebuild_id` 主键、active-manifest input digest、result digest/counts、status/timestamps。二者同样无登录角色 DML。

公开 document identity 锚定 `core.documents.id`，不是单个 version；新 document version 的有效 grant 更新同一 public ID。claim/entity/evidence identity 也不把内部 ID直接暴露。slug 首次生成后不可改：document `d-` + public UUID 32 位小写 hex，entity `e-` + 同格式。

Manifest 必须在 `record_review_decision` 的同一事务、case 行锁内、应用 claim structured changes 后捕获。grant 与 manifest 是 1:1；任何可变内部字段之后变化不影响该 revision。公开 source URL 只接受 collector 已规范化的 absolute HTTP(S)、无 userinfo/fragment；缺失或非法时不得生成 manifest。Claim approve/revise 还必须锁定并验证同 document version 的 active v2 document grant/manifest；缺失时 `publication_document_grant_required`，不得创建 claim grant/outbox。

## 3. Canonical payload 与 Outbox v2

manifest canonical JSON：UTF-8、object key C 排序、compact、UUID 小写、数值按 WP9 value-based 规则；evidence 按 manifest ordinal ASC。包含所有会进入 public 的字段和 basis IDs，不含 reviewer identity、时钟、raw body、object key、Prompt/model I/O。

`grant.publication_payload_sha256 = manifest_sha256`。Outbox：

```json
{
  "schema": "publication-outbox.v2",
  "grant_id": "uuid",
  "decision_id": "uuid",
  "subject_type": "document|claim|entity",
  "subject_id": "uuid",
  "revision_no": 1,
  "payload_sha256": "64-lower-hex"
}
```

event types/key 沿用 WP9：

- `publication.granted` → `publication-granted:{grant_table}:{grant_id}`；
- `publication.superseded` → `publication-superseded:{grant_table}:{old}:{new}`；
- `publication.withdrawn` → `publication-withdrawn:{grant_table}:{grant_id}:{decision_id}`。

同 key 同 payload 返回同 event；异 payload `review_idempotency_payload_conflict`。decision 重放不新增 grant/manifest/outbox。

## 4. Legacy v1

upgrade 不从 latest valid result、core 当前行或 legacy SQLite 猜 document title/category/fact status。所有无 typed manifest 的 active grant 和未 ack v1 event：

1. 写 quarantine；
2. event 标 `terminal_at` / `publication_manifest_required`；
3. 专用 claim 永不领取；
4. 不修改 grant 历史、不写 public；
5. reviewer 以新 request_id 对 approved case 提交符合 v2 的 `revise`，旧 grant superseded，新 grant/manifest/outbox v2 进入正常链。

withdraw 必须仍可撤销 legacy active grant，不要求先补 manifest。

## 5. Publisher claim/apply

`ops.claim_publication_outbox`：

- session_user 必须 `uap_publisher`；
- 只选 `published_at IS NULL AND terminal_at IS NULL AND available_at<=now()`；
- payload schema v2、event type/aggregate allowlist；
- `occurred_at,id` ASC，`FOR UPDATE SKIP LOCKED`；
- limit 1–100，lease 1–86400 秒；每领一次增加 `publish_attempts`。

`ops.apply_publication_event(event_id,lease_token)` 在一个数据库事务：

1. 锁 event；若同 lease token 的 attempt 已完成 applied，则验证 event/manifest/projection digest 后返回首次结果；若 operation payload 不同则冲突；其余情况验证 token 未过期；
2. 验证 schema/event/aggregate/grant/subject/revision；
3. 读取 typed manifest，重算 hash 并与 grant/outbox 三方相等；
4. 获得/创建 stable public identity；
5. 按 event 和当前 active grant 幂等 upsert/delete/reconcile；
6. 执行 deferred constraints；
7. 设置 `published_at`、清 lease/error；
8. commit。

调用方不得另行调用 `ack_outbox`。第 1–7 步任一失败，projection 与 ack 全回滚。

### 5.1 Publisher 服务操作幂等

Publisher 函数是仅授予 `uap_publisher` 的内部租约协议，不是 HTTP/主体业务写入口，因而不接受或伪造 `uap.request_id`。其稳定 operation key 是 `(event_id,attempt_no,lease_token)`：

- claim 每次成功是一个新的 lease attempt；并发时一个 event 最多一个 active token。调用方丢失响应只能等待该 lease 到期，不能用相同 dispatcher 参数要求返回同一批次；
- apply 的 operation payload 为 `{op:"apply",event_id}`。首次提交把 attempt 标 applied；同 token/同 payload 重放返回首次 `published_at/projection_digest` 且零写入；同 token 不同 op/payload 为 `publication_delivery_attempt_conflict`；错误/过期 token 为 `publication_lease_lost`；
- fail 的 operation payload 覆盖 `{op:"fail",event_id,error_code,error_summary,retry_delay_seconds,terminal}`；summary 必须是该稳定 code 注册的固定净化模板且不超过 300 code points。首次提交记录 payload hash 并 release/terminal；同 token/同 hash 重放返回首次结果；同 token/异 hash冲突。新 attempt 已开始后，旧 token 一律 lease lost；
- attempt/history 与 outbox 状态必须在同一事务更新。无 attempt 行、伪造 token、越权调用、非法参数、hash/event tamper、注入回滚均为必测负例。

`ops.rebuild_public_projection(rebuild_id uuid)` 的 command key 为 `rebuild_id`。首次运行固定 active-manifest input digest；同 ID/同 digest 重放返回首次报告，同 ID/异 digest 为 `publication_rebuild_id_conflict`，失败运行保留净化状态但不替换 public。

冻结返回形状：claim 返回 event/outbox payload、`attempt_no`、token、expiry；apply 返回 `event_id,published_at,projection_digest,attempt_no,replayed`；fail 返回 `event_id,attempt_no,outcome(retry_wait|terminal),available_at,terminal_at,replayed`；rebuild 返回 `rebuild_id,input_digest,result_digest,counts,status,replayed`。返回值不得含 manifest 内容、source URL、lease token hash 或错误明细。

## 6. Subject 投影

### Document

- granted/revise：upsert logical document 当前行；首次设置 `published_at`，后续保留并设置 `revised_at`；`document_grant_id/revision_no` 必须是新 active grant；同事务更新 search row并 reconcile active claim manifests。
- stale granted/superseded：若 public 已是更高 revision，no-op success，不降级。
- withdrawn：只有 public row 当前 grant/revision 匹配才删除；先删除 dependent document_entities、claim_evidence、claims、orphan evidence、search；entity 保留。
- republish：复用 stable public ID/slug，并从 active v2 claim manifests 重建依赖。

### Entity

- 只投影 active canonical entity 的 manifest snapshot；merge 后旧 manifest 不自动漂移。
- revise upsert同 public identity；withdraw 删除 document_entities 后删除 entity。
- entity grant 事件同时 reconcile 依赖该 entity 的已公开 claim subject links。

### Claim/Evidence

- grant 创建前置：对应 document 已有 active v2 grant/manifest；否则决定事务 `publication_document_grant_required` 全回滚。event apply 前置：public document visible；尚未完成投影时 retryable `publication_dependency_not_ready`。
- 一个 claim manifest 必须有 1–20 supports evidence；每个 excerpt 1–2000 code points；claim、evidence、join 在同事务。
- `display_ordinal` 在首次 public identity 创建时按 logical document 锁串行分配，之后不变；不直接使用可能跨 analysis 重复/为空的 core ordinal。
- claim subject entity 只有在 entity public visible 时创建 document_entities；不可见时 claim仍可公开但不暴露内部 entity，后续 entity event reconcile。
- withdraw/revise 先删/替换 join，清 orphan evidence，重算 document_entities；不得留下无 evidence public claim。

### Relation

`audit.relation_publication_grants`、`public.relations`、`public.relation_evidence` 在 WP10 零写入。Publisher 遇到 relation aggregate 必须 terminal `publication_event_schema_unsupported`，不能 no-op ack 成功。

## 7. Search

- document upsert、withdraw 和 rebuild 与 `public.search_documents` 同事务；API 不查询 core。
- `display_text` 为 title + summary（null 当空）+ 已公开 claim text 的确定拼接；不含 evidence excerpt、完整正文或 entity description。
- `search_vector=to_tsvector('simple',display_text)`；facets 只含 `{category,fact_status,source_name}`。
- claim publish/withdraw/revise 触发所属 document search row 重算；`indexed_at` 记录本次事务时钟。
- 对同 active manifests 完整重建得到相同 display text/facets/digest；时钟字段不进入内容 digest。

## 8. 失败、重试和人工重放

- retryable：dependency not ready、serialization/deadlock、短暂数据库/对象依赖；指数退避 `min(3600,2^(attempt-1))` 秒。
- deterministic terminal：schema/hash/aggregate/grant/manifest 不合法；立即 terminal。
- retryable 达 12 次转 `publication_retry_exhausted` terminal。
- terminal event 保留 `published_at NULL`、`terminal_at/code/summary`，不再自动 claim。
- W11 data_operator 重放要求理由≥10、事件 terminal；同一 request_id/hash 重放幂等，异 hash 冲突；清 terminal、`available_at=now()`，不清 attempts/history，并追加 audit event。

错误摘要只能是固定净化模板；不含 manifest 文本、URL query、SQL、token 或 lease token。

## 9. Rebuild

维护窗内 owner/migrator 调用 `ops.rebuild_public_projection(rebuild_id uuid)`：

1. advisory lock 阻止 Publisher apply；
2. 在事务内构造新 staging projection；
3. 只读取 active v2 grants/manifests；quarantine/withdrawn/disputed/rejected 不进入；
4. 校验 claim evidence、identity、grant hash、行数和内容 digest；
5. 原子替换当前 public 数据；
6. 输出仅含计数/哈希/blocked IDs 的报告。

任何 mismatch 全回滚并报 `publication_rebuild_mismatch`。重建不 ack 待处理事件；重建后这些事件仍可幂等 apply。
