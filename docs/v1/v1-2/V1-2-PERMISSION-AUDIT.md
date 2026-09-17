# V1-2 Contract Freeze — Permission and Audit

## 单管理员政策

V1-2 绑定一个明确的 OIDC person principal，推荐新增不蕴含其它角色的 `editorial_admin` application role。该角色只允许 Internal Library 的 document/editorial/reanalysis/trash/restore 受控函数；不授予 reviewer、senior_reviewer、data_operator、publisher 或 service 权限。采集、模型、Worker、Scheduler、Publisher 仍无编辑或发布权限。

若负责人决定复用 `reviewer`，也必须只绑定该一个 principal 并在函数级白名单限制为 editorial 操作；不得把所有 reviewer 扩展成编辑者。推荐的独立 role 可避免把“同一人编辑/审阅”伪装为第二审核身份，并为未来多人模式保留严格 RBAC 边界。

现有 `audit.require_active_role`、OIDC issuer/sub → active person、role_bindings、`review.session` GUC 绑定继续复用；不删除既有 self-review/separation-of-duties 检查。V1-2 的例外仅是：editorial_admin 对自己的内部资料可以编辑、采用 AI、trash/restore；它不产生 review decision，也不产生 publication grant。

## 审计要求

复用 append-only `audit.audit_events`，不建立第二套日志。每次保存、采用 AI、reanalysis request、trash、restore 至少记录：

- active person principal、request_id、UTC timestamp；
- action、target_type=document、target_id；
- before_digest、after_digest、字段级 diff 或净化 metadata；
- reason（若动作要求）；
- base_revision、new_revision、analysis_result_id/model job ID（适用时）。

Audit history 只读；不允许编辑、删除或覆盖历史。查询不得返回 API key、raw model I/O 或其他 secret。

## 权限负例

- 未认证/无效 OIDC：401；无 active person 或无 editorial_admin binding：403。
- reviewer/senior_reviewer 不因既有角色自动获得 editorial 写权限，除非负责人显式绑定 editorial_admin。
- service principal、Worker、Scheduler、Publisher 调用 editorial function 一律拒绝。
- editor 不能调用 publication、entity merge、relation、永久删除函数。
- trash 文档的 reanalysis、编辑和公开读取按状态契约拒绝/隐藏；restore 后才可继续内部操作。
