# WP9 冻结主题索引

## 1. 会话绑定、写权限与幂等

- ADR-0014：GUC、`require_active_role`、无裸 DML、event_key
- G9-01–G9-05、G9-29–G9-31、G9-34–G9-38
- 结论：acting principal 只来自 `SET LOCAL uap.principal_id`；写函数强制 `request_id`；同键同摘要重放，同键异摘要冲突。

## 2. Review case 与决定

- ADR-0015：open/assign/close、追加决定、自审隔离、structured_changes 键
- G9-06–G9-10
- 结论：同一 subject 至多一个未关闭 case；relation case 在 WP9 拒绝。

## 3. Publication grant 与 Outbox

- ADR-0016：approve/revise→grant，withdraw→撤回，live 唯一索引，只写 outbox
- G9-11–G9-16、G9-27–G9-28
- 结论：superseded 不填 `withdrawn_at`；唯一键改为 `grant_status='active'`；并发 revise 串行双成功；不写 `public.*`。

## 4. 分析选择与 candidate 晋升

- ADR-0017：`analysis_selections`、`accept_entity_candidate`
- G9-17–G9-19
- 结论：selection 不是物化前置；同名不自动合并。

## 5. 授权合并

- ADR-0018：包装函数调用默认关闭的 merge/reverse
- G9-20–G9-22、G9-25
- 结论：登录角色仍无 `core.merge_entities` EXECUTE。

## 6. 手工 Claim 与 subject 绑定

- ADR-0019：手工约束、私有副作用、AI 不变量保持
- G9-23–G9-24、G9-32–G9-33
- 结论：改绑/撤回 evidence 只经 `record_review_decision`；AI 最后一条 supports 不可删。

## 7. 关系与公开层

- ADR-0013（WP8，仍有效）、ADR-0016
- G9-08、G9-16、G9-25
- 结论：relation 成功路径与 `public` 投影均非 WP9。
