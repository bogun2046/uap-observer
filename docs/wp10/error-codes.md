# WP10 稳定错误码注册表

所有 HTTP 错误使用 `application/problem+json`，包含 `code` 与 `request_id`；不返回 SQLSTATE、表名、函数名、raw exception 或内部 ID。数据库 SQLSTATE 只进入脱敏审核证据。

## 1. HTTP 边界码

| code | HTTP | 含义 |
|---|---:|---|
| `api_auth_required` | 401 | 缺 bearer token |
| `api_token_invalid` | 401 | 签名/alg/issuer/audience/exp/nbf/sub 不合法 |
| `api_principal_not_provisioned` | 403 | issuer/sub 未绑定 active person |
| `api_request_invalid` | 422 | path/query/body Schema 失败或未知字段 |
| `review_request_id_missing` | 400 | 写请求缺 `Idempotency-Key` |
| `review_request_id_invalid` | 400 | Idempotency-Key 不是规范 UUID |
| `api_cursor_invalid` | 400 | cursor 签名、版本、资源、filter hash 或字段错误 |
| `api_resource_not_found` | 404 | 资源不存在或对调用者不可见 |
| `api_capability_closed` | 404 | 明确关闭的能力；relation 探针使用 |
| `api_rate_limited` | 429 | 受控限流 |
| `api_internal_error` | 500 | 未分类异常；detail 必须为空 |
| `api_dependency_unavailable` | 503 | 数据库/JWKS 等短暂不可用 |

## 2. WP9 冻结码的 HTTP 映射

| code | HTTP |
|---|---:|
| `review_principal_missing`、`review_service_principal_denied`、`review_session_role_denied`、`review_role_denied`、`review_scope_unsupported` | 403 |
| `review_self_review_denied`、`review_assignee_mismatch` | 403 |
| `review_subject_missing`、`review_case_missing`、`review_selection_missing`、`review_candidate_missing`、`review_entity_missing`、`knowledge_merge_missing_entity`、`knowledge_merge_missing_event` | 404 |
| `review_idempotency_payload_conflict`、`review_case_already_open` | 409 |
| `review_case_not_assignable`、`review_case_already_closed`、`review_case_not_decidable`、`review_decision_not_allowed` | 409 |
| `review_grant_not_active`、`review_grant_already_active`、`review_candidate_not_pending`、`review_subject_already_bound` | 409 |
| `review_bind_target_not_active`、`review_bind_target_not_canonical`、`review_subject_not_active`、`review_subject_not_canonical` | 409 |
| `review_reason_too_short`、`review_structured_changes_unsupported` | 422 |
| `review_assignee_invalid` | 422 |
| `review_selection_type_unsupported`、`review_selection_not_valid`、`review_candidate_evidence_missing`、`review_candidate_origin_invalid` | 422 |
| `manual_claim_requires_supports`、`review_ai_evidence_immutable`、`review_decision_not_in_transaction` | 422 |
| `knowledge_relation_review_not_in_wp9` | 404 `api_capability_closed` at HTTP boundary |
| `review_grant_superseded_blocks_downgrade` | 不经 HTTP；migration 阻断 |

WP10.5 必须把实际 migration 已存在但 WP9 Python registry 尚未收录的 `review_subject_missing` 和下列 bottom merge 码显式加入稳定 registry；不得直接透传 primary message：

| code | HTTP |
|---|---:|
| `knowledge_merge_same_entity`、`knowledge_merge_not_active`、`knowledge_merge_not_canonical` | 409 |
| `knowledge_merge_cycle`、`knowledge_merge_chain_too_long` | 409 |
| `knowledge_merge_not_merge_event`、`knowledge_merge_already_reversed` | 409 |
| `knowledge_merge_reason_required` | 422 |
| `knowledge_merge_principal_required`、`knowledge_merge_principal_inactive` | 403 |
| `knowledge_merge_reverse_copy_failed` | 500 `api_internal_error`（日志只记稳定 code） |
| `knowledge_merge_downgrade_blocked` | 不经 HTTP；migration 阻断 |

`review_unclassified` 不是可公开 code；遇到它一律返回 `api_internal_error`。

## 3. WP10 publication 码

| code | 类别 | 处置 |
|---|---|---|
| `publication_manifest_required` | 数据隔离 | legacy grant/event quarantine；不可投影 |
| `publication_manifest_invalid` | 422/terminal | manifest 字段/枚举/关联不合法 |
| `publication_source_url_missing` | 422 | document 无可公开 canonical URL |
| `publication_evidence_required` | 422 | claim manifest 无 supports evidence |
| `publication_evidence_excerpt_too_long` | 422 | excerpt 超过 2000 Unicode code points |
| `publication_document_grant_required` | 409 | claim approve/revise 时对应 document 无 active v2 grant/manifest |
| `publication_payload_hash_mismatch` | terminal | grant/outbox/manifest hash 不一致 |
| `publication_event_schema_unsupported` | terminal | 非 v2 或未知 event type |
| `publication_event_aggregate_mismatch` | terminal | aggregate type/id 与 grant 不符 |
| `publication_grant_missing` | terminal | 引用 grant 不存在 |
| `publication_grant_state_stale` | success/no-op | stale superseded/withdraw event，不覆盖新 revision |
| `publication_dependency_not_ready` | retryable | document/entity 尚未完成所需当前投影 |
| `publication_lease_lost` | retryable/40001 | token 错误或过期；当前事务回滚 |
| `publication_delivery_attempt_conflict` | 40001 | 同 event attempt/lease token 的 operation payload 与首次不同 |
| `publication_retry_exhausted` | terminal | 达到冻结最大次数 |
| `publication_database_unavailable` | retryable | Publisher 的数据库/短暂依赖失败；不暴露底层异常 |
| `publication_claim_parameters_invalid`、`publication_failure_parameters_invalid` | 422 | Publisher 专用函数参数不符合租约协议 |
| `publication_event_not_terminal` | 409 | 对非 terminal event 请求人工重放 |
| `publication_replay_reason_too_short` | 422 | 重放理由少于 10 字符 |
| `publication_contract_state_blocks_downgrade` | migration 阻断 | 降级会丢 v2 状态 |
| `publication_rebuild_mismatch` | 运维失败 | active manifest 与重建结果 digest 不一致 |
| `publication_rebuild_id_conflict` | 运维冲突 | 同 rebuild UUID 对应的 active-manifest input digest 与首次不同 |

terminal 不得设置 `published_at`；必须设置 `terminal_at` 与稳定 code，并从专用 claim 集合排除。人工重放清空 terminal 字段、设置 `available_at`，历史 `publish_attempts` 和审计事件保留。

## 4. 保留的 relation 码

| code | 规则 |
|---|---|
| `knowledge_relation_task_not_in_wp8` | G8-16C 误领必须 terminal failure；WP10 不改名、不改为 succeeded |
| `knowledge_relation_review_not_in_wp9` | DB relation review 入口继续拒绝；HTTP 转为 capability closed |

任何新 code 必须先修改本注册表、实现常量、映射测试和 acceptance case，再进入代码；不得临时使用自由文本作为公开 code。
