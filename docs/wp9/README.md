# WP9 审核、授权绑定与发布授权

状态：`G9-FROZEN-20260825-04` 设计待 Codex 冻结（R4 整改）；编码门禁关闭，直至项目负责人发出 WP9.1 启动口令。
父基线：G8-GATE-8.6 已签署 `8550b8fe2d3322428fc9487e91aeb830425b0ed1`（PR #36，CI run 32834875538）。

WP9 把 WP8 已物化的内部 Claim / Entity Candidate 纳入审核状态机：绑定登录会话与 `senior_reviewer` 授权、追加审核决定、签发/撤回 publication grant，并经 Outbox 通知 Publisher。不写 `public`，不交付公开 API 或搜索（WP10），不实现 AI relation 成功路径。

## 核心冻结

1. 审核写入只经 owner `SECURITY DEFINER` 函数；`uap_api` 无审核表裸 DML。
2. 授权主体只来自事务内 `SET LOCAL uap.principal_id`；函数参数不能充当 acting principal。
3. active principal ≠ 角色授权；必须存在未撤销的 `audit.role_bindings`。
4. `senior_reviewer` 蕴含 `reviewer`；撤回与实体合并/撤销仅 senior_reviewer。
5. `core.merge_entities` / `core.reverse_entity_merge` 对所有登录角色保持 `REVOKE EXECUTE`；只经审核包装函数调用。
6. 审核决定追加，不覆盖；grant 必须绑定同一 review case 的 approve/revise；撤回绑定同一 case 的 withdraw。
7. WP9 只写 `ops.outbox_events`，不让 `uap_api`/`uap_worker` 入队 `publish_*` job，不写 `public.*`。
8. `analysis_selections` 只表示“当前供审核展示的分析结果”，不是物化前置；不得回改 WP8 交接。
9. 同名不自动合并；candidate 晋升为 `core.entities` 必须显式、可审计。
10. relation review / `resolve_relations` 成功路径仍关闭。
11. WP9.x 串行独立验收；WP9.1 从本 docs-only 设计提交开始。
12. 写函数 `event_key` 仅为 `操作类型:{request_id}`；全部业务输入进入 `payload_sha256`。
13. Claim subject / 最后一条 evidence 只经 `record_review_decision` 私有副作用；AI supports 不变量不改。
14. grant live 唯一索引以 `grant_status='active'` 为准；并发 revise 为串行双成功。

## 文档

| 文件 | 作用 |
|---|---|
| [BASELINE.md](BASELINE.md) | 固定父基线、设计提交与放行规则 |
| [R2-REMEDIATION.md](R2-REMEDIATION.md) | R2：grant 索引 / evidence / 私有副作用 / 初版幂等 |
| [R3-REMEDIATION.md](R3-REMEDIATION.md) | R3：并发串行双成功与全写函数幂等验收 |
| [R4-REMEDIATION.md](R4-REMEDIATION.md) | R4：event_key 去业务参数，payload_sha256 覆盖冲突 |
| [TOPIC-INDEX.md](TOPIC-INDEX.md) | 冻结主题到 ADR / 用例的索引 |
| [implementation-ticket.md](implementation-ticket.md) | WP9.1–9.6 实施边界和阶段链 |
| [acceptance-ticket.md](acceptance-ticket.md) | 独立验收责任、证据和门禁 |
| [acceptance-cases.md](acceptance-cases.md) | G9-01–G9-38 正反向验收用例 |
| [adr/0014-review-session-and-write-authority.md](adr/0014-review-session-and-write-authority.md) | 会话 GUC、角色绑定与写权限 |
| [adr/0015-review-case-and-decision-lifecycle.md](adr/0015-review-case-and-decision-lifecycle.md) | case 开闭、决定追加、职责分离 |
| [adr/0016-publication-grants-and-outbox.md](adr/0016-publication-grants-and-outbox.md) | grant、撤回与 Publisher Outbox |
| [adr/0017-analysis-selection-and-candidate-promotion.md](adr/0017-analysis-selection-and-candidate-promotion.md) | 当前分析选择与 candidate 晋升 |
| [adr/0018-authorized-entity-merge.md](adr/0018-authorized-entity-merge.md) | senior_reviewer 包装 merge/reverse |
| [adr/0019-manual-claims-and-subject-binding.md](adr/0019-manual-claims-and-subject-binding.md) | 手工 Claim、证据与 subject 绑定 |
| [SHA256SUMS](SHA256SUMS) | 除自身外冻结文档的 SHA-256 |

## 明确不授权

设计冻结不等于自动开工。项目负责人未提供完整 WP9.1 启动口令前，不得写业务代码、迁移、CI、commit 或 PR；实施中也不得修改本冻结标准来适配代码。
