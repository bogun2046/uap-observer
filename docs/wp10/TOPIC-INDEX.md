# WP10 冻结主题索引

| 主题 | 权威 ADR / 文档 | 阶段 | 验收 |
|---|---|---|---|
| 产品 SHA、tree、设计根和线性链 | BASELINE | 设计、10.6 | G10-01、G10-28 |
| WP1 草案与 0019 权威枚举差异 | ADR-0020、api-contract | 10.1、10.4 | G10-02、G10-16 |
| `uap_api` / Publisher 裸 DML 收口 | ADR-0020、permissions | 10.1 | G10-03、G10-24 |
| grant 同事务 typed manifest | ADR-0021、projection-contract | 10.1 | G10-04、G10-05 |
| 旧 v1 grant/outbox 隔离 | ADR-0021、migration-plan | 10.1 | G10-05 |
| Publisher 专用 claim、apply+ack | ADR-0022、projection-contract | 10.2 | G10-06–G10-10 |
| document/entity 当前投影 | ADR-0022 | 10.2 | G10-07、G10-08 |
| claim/evidence/document_entities | ADR-0022 | 10.3 | G10-11–G10-14 |
| withdraw、republish、rebuild | ADR-0022 | 10.3、10.6 | G10-13、G10-27 |
| PostgreSQL FTS 与 facets | ADR-0022、api-contract | 10.3、10.4 | G10-15、G10-18 |
| 公开 API 可见性、游标和排序 | ADR-0023、api-contract | 10.4 | G10-16–G10-20 |
| OIDC issuer/sub、RBAC、GUC | ADR-0023、permissions | 10.5 | G10-21、G10-22 |
| WP9 case/decision/grant API | api-contract | 10.5 | W01–W04、G10-22 |
| selection/candidate API | api-contract | 10.5 | W05–W07 |
| merge/reverse 包装 API | api-contract、permissions | 10.5 | W08–W09、G10-24 |
| manual claim API | api-contract | 10.5 | W10 |
| publication dead-event replay | ADR-0022、api-contract | 10.5 | W11 |
| 所有写操作 request_id / payload hash / rollback | ADR-0023、acceptance-cases | 10.5 | 每个 Wxx-S/P/M/R/C/T |
| 稳定错误码与无内部泄漏 | error-codes、api-contract | 10.4–10.6 | G10-19、G10-23 |
| relation fail-closed | ADR-0024 | 全阶段 | G10-09、G10-20、G10-29 |
| migration/downgrade fail-closed | migration-plan | 10.1–10.6 | G10-05、G10-26 |
| 性能、runtime probe、CI 与整体签署 | acceptance-ticket | 10.6 | G10-25–G10-29 |

## 结论索引

1. WP10 只发布 document / claim / entity；relation 不生成 manifest、不投影、不注册 HTTP 路由。
2. `publication.granted` 不等于内容已公开；只有 Publisher 原子 apply+ack 提交后才 `visible`。
3. HTTP 管理写请求的 `Idempotency-Key` 必须是 UUID，并原样绑定 `uap.request_id`；数据库现有 operation+request_id 键空间继续权威。
4. public API 每页事务内一致、跨页为当前投影 keyset 语义，不宣称历史 snapshot。
5. 不提供独立 grant 写 API；grant 仍只由 `record_review_decision` 的 approve/revise/withdraw 私有副作用产生。
