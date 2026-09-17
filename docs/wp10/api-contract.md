# WP10 HTTP API 冻结契约

状态：所有路由均为 `planned/closed`，直至对应阶段门禁签署。

## 1. 进程、媒体类型与通用响应

- Public API 与 Admin API 是两个进程、两个数据库角色，见 permissions。
- JSON 请求/响应为 UTF-8；成功 `application/json`，错误 `application/problem+json`。
- 所有 response 返回 `X-Request-ID`。读请求缺该 header 时服务生成 UUID；写请求的 request ID 必须来自规范 UUID `Idempotency-Key`。
- Problem：`{type,title,status,code,request_id,detail}`；`type=/problems/{code}`，生产 `detail` 仅可为安全固定文案或 null。
- 未列出的字段因 `additionalProperties=false` 拒绝。

## 2. 权威枚举与 WP1 草案差异

| 概念 | WP10 权威值 | WP1 草案差异 |
|---|---|---|
| review case type | `document,claim,entity`；`relation` closed | 草案 `document_version` 改为数据库 `document` |
| review status | `open,assigned,approved,rejected,disputed,withdrawn,closed` | 删除草案 `candidate,in_review` |
| decision | `approve,reject,dispute,withdraw,revise` | 一致 |
| entity type | `person,organization,location,event,object,concept` | 草案 `place,document,topic` 非 DB 值 |
| locator type | `text,html,pdf,video,audio` | 草案 `characters,page,timecode,structured` 非 DB 值 |
| claim type | `observation,attribution,event,assessment,other` | WP1 示例 `source_action` 非 DB 值 |
| assertion status | `reported,corroborated,disputed,unverified,false` | WP1 示例 `official_record` 非 DB 值 |
| selection result type | `claim_extraction,entity_extraction` | WP9 明确拒绝 summary/classification selection |
| document category | `official_report,government_document,military,scientific_research,historical_event,sighting,disputed_event,other` | WP10.1 从实际 legacy enum 前向冻结 |
| public fact status | `official_record,corroborated,source_reported,unverified,disputed,opinion` | WP10.1 从实际 legacy enum 前向冻结 |
| Idempotency-Key | canonical UUID，且等于数据库 `uap.request_id` | 草案允许任意 16–128 字符串；与 WP9 UUID GUC 冲突 |
| relation DTO/route | closed，不出现在 EntityDetail | 草案公开 `/v1/relations` 并要求 `relations` 字段，但仓库无成功链 |

不得在 handler 做同义词静默映射；非法旧值返回 `api_request_invalid`。

## 3. Public API 路径

| 方法与路径 | Query | 200 schema | 其它状态 |
|---|---|---|---|
| `GET /v1/documents` | `limit,cursor,category,fact_status,published_after` | `DocumentPage` | 400 bad cursor/query |
| `GET /v1/documents/{document_id}` | — | `DocumentDetail` | 404 不可见/不存在 |
| `GET /v1/claims/{claim_id}` | — | `ClaimDetail` | 404 |
| `GET /v1/entities` | `limit,cursor,type` | `EntityPage` | 400 |
| `GET /v1/entities/{entity_id}` | — | `EntityDetail` | 404 |
| `GET /v1/search` | `q,limit,cursor,category,fact_status` | `SearchPage` | 400 q/cursor |

`/v1/relations` 和 relation detail 不注册业务 handler；API 的 exact-known-closed capability fallback 只把这些精确路径转换为 404/problem code `api_capability_closed`。它不得查询数据库、返回空列表或接受方法/body，未知普通路径仍为 `api_resource_not_found`。

### 3.1 Public DTO

```text
PublicSource = {name:string, url:absolute-http(s)-uri}

DocumentSummary = {
  id:uuid, slug:string, title:string, summary:string|null,
  category:DocumentCategory, fact_status:FactStatus,
  source:PublicSource, source_published_at:datetime|null,
  published_at:datetime, revised_at:datetime|null, revision:integer>=1
}

DocumentDetail = DocumentSummary + {
  claims: ClaimDetail[], related_entities: EntitySummary[]
}

PublicEvidence = {
  id:uuid, excerpt:string(1..2000), locator_type:LocatorType,
  page_start:int|null, page_end:int|null,
  time_start_ms:int|null, time_end_ms:int|null,
  locator:object, source_url:absolute-http(s)-uri
}

ClaimDetail = {
  id:uuid, document_id:uuid, ordinal:int>=0, text:string,
  type:ClaimType, assertion_status:AssertionStatus,
  attribution:string|null, revision:int>=1,
  evidence:PublicEvidence[1..]
}

EntitySummary = {
  id:uuid, slug:string, type:EntityType, name:string,
  description:string|null, country_code:string|null, revision:int>=1
}

EntityDetail = EntitySummary + {related_documents:DocumentSummary[]}

Page<T> = {items:T[], next_cursor:string|null}
SearchHit = {document:DocumentSummary, highlights:string[]}
SearchPage = Page<SearchHit>
```

EntityDetail 不返回 `relations` 字段，避免把 relation closed 伪装为已处理空集合。WP1 草案字段在未来 relation 整链签署后才可通过版本化扩展加入。

## 4. Public 读取一致性、排序与 cursor

- 单个请求在 `READ ONLY REPEATABLE READ` 事务读取当前 public projection；响应前结束事务。
- 跨页不是历史 snapshot。新增行不会因 keyset 进入已翻过区间；撤回可能使后页变短；当前 revision 更新可能改变搜索 rank。客户端不得把 cursor 当可恢复快照。
- documents 固定排序：`published_at DESC, id DESC`。
- entities 固定排序：`published_at DESC, id DESC`。
- document claims：`ordinal ASC, id ASC`；related entities：`name COLLATE "C" ASC,id ASC`。
- search：`ts_rank_cd DESC,published_at DESC,document_id DESC`，相同查询/投影下确定。
- limit 默认 20，范围 1–100。
- cursor 为 base64url canonical JSON + HMAC-SHA256，包含 `v,resource,sort,last,filters_sha256`；过期/篡改/跨资源/改变 filter 重用均 `api_cursor_invalid`。
- q 先做 Unicode NFKC、trim，长度 2–200 code points；SQL 使用参数化 `websearch_to_tsquery('simple',q)` 和 `public.search_documents.search_vector`，禁止动态 SQL。
- 可见性只由 public 当前表决定；内部存在但未投影/已撤回的 ID 一律 404，不暴露状态差异。

## 5. Admin read 路径

全部要求 bearer OIDC 与 reviewer/senior，除 publication events 如表所示。

| 方法与路径 | 过滤/响应 | 角色 |
|---|---|---|
| `GET /admin/v1/review-cases` | `status,case_type,assigned_to,limit,cursor` → `ReviewCasePage` | reviewer+ |
| `GET /admin/v1/review-cases/{id}` | subject、sanitized provenance/evidence、decisions、grant/projection state | reviewer+ |
| `GET /admin/v1/analysis-results` | `document_version_id,result_type,validation_status,limit,cursor`；不含 model raw I/O | reviewer+ |
| `GET /admin/v1/entity-candidates` | `status,analysis_result_id,limit,cursor` | reviewer+ |
| `GET /admin/v1/evidence-spans` | 必须 `document_version_id`；只含 span/locator，不含完整 extracted body | reviewer+ |
| `GET /admin/v1/entities` | `q,status,type,limit,cursor` | reviewer+ |
| `GET /admin/v1/publication-events` | `state=retry_wait|terminal,limit,cursor`，错误摘要净化 | senior/data_operator |

Admin list 同样使用 signed keyset cursor；排序分别为 case `priority DESC,opened_at,id`、analysis/candidate/span/entity/event 的 `created/occurred_at,id`。查询只读，不接受 request_id。

## 6. Admin write 路径与 schema

全部写路由要求 `Idempotency-Key: <uuid>`，成功统一 `200 OK`，响应：

```text
WriteResult = {
  operation:string, resource_id:uuid, request_id:uuid,
  publication:null|{
    grant_id:uuid, revision:int>=1,
    grant_status:active|withdrawn|superseded,
    outbox_event_id:uuid|null,
    projection_state:pending|visible|blocked|withdrawn
  }
}
```

相同请求重放仍返回 200 和首次 `resource_id`；publication state 可反映查询时的更新状态，因此不承诺响应字节相同。所有 body unknown field 均拒绝。

| W | 方法与路径 | Body | 数据库函数 | 角色 |
|---|---|---|---|---|
| W01 | `POST /admin/v1/review-cases` | `OpenCaseRequest` | `audit.open_review_case` | reviewer+ |
| W02 | `PUT /admin/v1/review-cases/{id}/assignment` | `{assignee_id:uuid}` | `audit.assign_review_case` | reviewer+ |
| W03 | `POST /admin/v1/review-cases/{id}/close` | `ReasonRequest` | `audit.close_review_case` | reviewer+ |
| W04 | `POST /admin/v1/review-cases/{id}/decisions` | `DecisionRequest` | `audit.record_review_decision` | reviewer+；withdraw senior |
| W05 | `POST /admin/v1/analysis-results/{id}/selection` | `ReasonRequest` | `audit.select_analysis_result` | reviewer+ |
| W06 | `POST /admin/v1/entity-candidates/{id}/accept` | `ReasonRequest` | `audit.accept_entity_candidate` | reviewer+ |
| W07 | `POST /admin/v1/entity-candidates/{id}/bind` | `{entity_id:uuid,reason:string}` | `audit.bind_entity_candidate` | reviewer+ |
| W08 | `POST /admin/v1/entities/merges` | `{source_entity_id,target_entity_id,reason}` | `audit.apply_entity_merge` | senior |
| W09 | `POST /admin/v1/entities/merge-events/{id}/reverse` | `ReasonRequest` | `audit.apply_entity_merge_reverse` | senior |
| W10 | `POST /admin/v1/claims/manual` | `ManualClaimRequest` | `audit.create_manual_claim` | reviewer+ |
| W11 | `POST /admin/v1/publication-events/{id}/replay` | `ReasonRequest` | `audit.requeue_publication_event` | data_operator |

```text
ReasonRequest = {reason:string(10..5000)}
OpenCaseRequest = {
  case_type:document|claim|entity, subject_id:uuid,
  priority:int16=0, reason:string(10..5000)
}
ManualClaimRequest = {
  document_version_id:uuid, claim_text:string(1..10000),
  claim_type:ClaimType, assertion_status:AssertionStatus,
  attribution:string|null, span_ids:uuid[1..20]
}
DecisionRequest = {
  decision:Decision, reason:string(10..5000), structured_changes:object={}
}
```

`span_ids` 的顺序和重复项都进入 payload hash；重复 span 在语义校验阶段 422，不在 handler 偷偷去重。

### 6.1 structured_changes

Claim case 沿用 WP9：

- `bind_subject_entity_id:uuid`：approve/revise；
- `replace_supporting_span_ids:uuid[1..20]`：仅 revise；
- `retire_supporting_evidence:true`：reject/withdraw；
- 其它键拒绝。

Document case：

- approve/revise 必须且只可含 `publication`；
- reject/dispute/withdraw 只允许 `{}`；
- `publication={title:string(1..500),summary:string|null(max 20000),category:DocumentCategory,fact_status:FactStatus,summary_analysis_result_id:uuid|null}`；
- 若给 `summary_analysis_result_id`，必须是同 document version 的 valid summary 且 manifest summary 与其 `result.summary` 完全一致；不提供时表示人工摘要；
- source name/URL/date 在同一决定事务从 ingest/core 捕获；URL 必须是现有 collector 规范化后的 absolute HTTP(S)、无 userinfo/fragment，canonical URL 缺失或其它 scheme 则 422。WP10 不擅自把合法 HTTP 改写为 HTTPS。

Entity case 只允许 `{}`；manifest 在决定事务从 active canonical entity 捕获。Relation case 在 handler 前返回 `api_capability_closed`，且 DB 旧拒绝仍保留。

## 7. 写事务、event_key、hash 与 rollback

每个 HTTP 写请求：

1. 验 token、request schema 和 UUID Idempotency-Key；
2. 从 issuer/sub 查询 active person，不信任请求 body principal；
3. checkout 一条 `uap_api` connection，`BEGIN`；
4. `set_config('uap.principal_id',...,true)` 与 `set_config('uap.request_id',...,true)`；
5. 调用表中恰好一个公共写函数；
6. 同事务读取安全响应 DTO；
7. commit 成功后才发送 200；任意异常 rollback，连接归还前验证 idle。

WP9 event_key 保持 operation+request_id：

```text
review.case.open | review.case.assign | review.case.close | review.decision
review.selection | review.candidate.accept | review.candidate.bind
review.entity.merge | review.entity.merge_reverse | review.claim.manual
```

W11 新键：`publication.replay:{request_id}`。

`payload_sha256` 继续使用 `audit._canonical_json` 规则，覆盖 path 参数、规范化默认值和全部业务 body；不含 principal、时钟或 token。W11 payload 为 `{op,event_id,reason}`。同 operation+request_id 同 hash 返回首次对象；异 hash 为 `review_idempotency_payload_conflict`/409。handler 不能建立第二套 idempotency 表或改写 request_id。

## 8. 状态码

| 状态 | 使用 |
|---:|---|
| 200 | 所有成功 GET/管理写及幂等重放 |
| 400 | request_id/cursor/q 格式错误 |
| 401 | 未认证/无效 token |
| 403 | principal/role/职责分离拒绝 |
| 404 | 不可见/不存在/closed capability |
| 409 | idempotency 或当前状态冲突 |
| 422 | schema 或业务语义验证失败 |
| 429 | 限流 |
| 500 | 未分类且已净化的内部错误 |
| 503 | 短暂依赖不可用 |

决定提交不等待 Publisher；不得以 201/202 暗示另一个 grant 写入口或 projection 已可见。

## 9. 明确延期

- `/v1/relations`、relation review/admin write；
- sources/jobs/prompts/role binding/audit 通用 API；
- cookie/browser session 与 CSRF token；
- websocket/SSE、bulk write、GraphQL；
- public revision history/as-of API；
- external search/semantic search；
- frontend 与 CDN cache invalidation。
