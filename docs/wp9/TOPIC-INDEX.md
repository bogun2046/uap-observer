# WP9 冻结主题索引

## 1. 会话绑定与写权限

- ADR-0014：GUC、`require_active_role`、无裸 DML
- G9-01–G9-05
- 结论：acting principal 只来自 `SET LOCAL uap.principal_id`；service 账号不能审核。

## 2. Review case 与决定

- ADR-0015：open/assign/close、追加决定、自审隔离
- G9-06–G9-10
- 结论：同一 subject 至多一个未关闭 case；relation case 在 WP9 拒绝。

## 3. Publication grant 与 Outbox

- ADR-0016：approve/revise→grant，withdraw→撤回，只写 outbox
- G9-11–G9-16
- 结论：不写 `public.*`；不让 API/Worker 入队 `publish_*`。

## 4. 分析选择与 candidate 晋升

- ADR-0017：`analysis_selections`、`accept_entity_candidate`
- G9-17–G9-19
- 结论：selection 不是物化前置；同名不自动合并。

## 5. 授权合并

- ADR-0018：包装函数调用默认关闭的 merge/reverse
- G9-20–G9-22、G9-25
- 结论：登录角色仍无 `core.merge_entities` EXECUTE。

## 6. 手工 Claim 与 subject 绑定

- ADR-0019：手工 Claim、最后一条 evidence、`subject_entity_id`
- G9-23–G9-24
- 结论：删除最后一条 evidence 必须走审核状态机。

## 7. 关系与公开层

- ADR-0013（WP8，仍有效）、ADR-0016
- G9-08、G9-16、G9-25
- 结论：relation 成功路径与 `public` 投影均非 WP9。
