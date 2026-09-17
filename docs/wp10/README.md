# WP10 发布投影、查询与 HTTP 边界

- 建议冻结号：`G10-FROZEN-20260828-01`
- 产品父基线：`8a956f32f6462a37043ac0ab6fffec46a5d830db`
- 文档状态：**`G10-GATE-10.1`–`G10-GATE-10.5` 均有项目负责人签署记录；WP10.6 的 R1 修复和 C/G10-27 已独立验收并固定提交，D 材料已准备并通过材料审核**
- 当前门禁：**G10-28/29 尚未整体关闭，`G10-GATE-10.6` 未签署，WP11 保持关闭；历史恢复状态仍为 `PARTIAL`**

WP10 把 WP9 已签发的 document / claim / entity publication grant 转换成可重放的公开投影，并交付公开只读 API、PostgreSQL 全文搜索和受 OIDC 保护的 WP9 管理 API。发布内容必须在 grant 同一事务冻结为 typed manifest；Publisher 只能通过 owner `SECURITY DEFINER` 权威函数写 `public`，不得裸 DML。

WP10 不交付 relation 成功路径。实际仓库没有 `relation_extraction` model task、版本化输出 Schema、materializer 或 review/grant 成功链，因此 ADR-0013 / G8-16C 在 WP10.1–WP10.6 全程有效。`/v1/relations` 不注册；relation 全链延期至 WP11+ 新冻结。

## 串行阶段

| 阶段 | 主题 | 门禁 |
|---|---|---|
| WP10.1 | publication manifest v2、旧 grant 隔离、DML/函数权限收口 | `G10-GATE-10.1` |
| WP10.2 | Outbox 专用领取、document/entity 投影、原子 apply+ack | `G10-GATE-10.2` |
| WP10.3 | claim/evidence/document_entities、撤回/重建与 search projection | `G10-GATE-10.3` |
| WP10.4 | 公开只读 HTTP、游标、搜索、可见性与容量索引 | `G10-GATE-10.4` |
| WP10.5 | OIDC 管理 HTTP、WP9 全部受控写入口与运维重放 | `G10-GATE-10.5` |
| WP10.6 | runtime probe、性能、安全、CI 与整体签署包 | `G10-GATE-10.6` |

每个阶段只能从上一门禁的已签署 SHA 开始，形成普通追加提交后立即 STOP。WP10.1 的整改启动证据见 [implementation-start.md](implementation-start.md)；WP10.2 的授权与停止线见 [implementation-start-wp10.2.md](implementation-start-wp10.2.md)；WP10.3 的签署起点与停止线见 [implementation-start-wp10.3.md](implementation-start-wp10.3.md)；WP10.4 的签署起点与停止线见 [implementation-start-wp10.4.md](implementation-start-wp10.4.md)。

## 权威文档

| 文件 | 作用 |
|---|---|
| [BASELINE.md](BASELINE.md) | SHA、父链、tree、合法起点与设计身份 |
| [TOPIC-INDEX.md](TOPIC-INDEX.md) | 主题、ADR、阶段与验收映射 |
| [implementation-ticket.md](implementation-ticket.md) | WP10.1–10.6 的输入、输出、权限和停止线 |
| [acceptance-ticket.md](acceptance-ticket.md) | 独立验收责任、证据与门禁 |
| [acceptance-cases.md](acceptance-cases.md) | G10 固定正反向用例与全部写操作矩阵 |
| [permissions.md](permissions.md) | 登录角色、函数 EXECUTE 与裸 DML 边界 |
| [api-contract.md](api-contract.md) | HTTP 路径、DTO、状态码、授权、分页和一致性 |
| [projection-contract.md](projection-contract.md) | manifest、Outbox、Publisher、撤回、重放与搜索投影 |
| [migration-plan.md](migration-plan.md) | 0020–0023 顺序、数据隔离和 downgrade fail-closed |
| [error-codes.md](error-codes.md) | HTTP/数据库/Publisher 稳定错误码注册表 |
| [adr/0020-wp10-scope-and-authority.md](adr/0020-wp10-scope-and-authority.md) | WP10 范围和写权威 |
| [adr/0021-publication-manifests.md](adr/0021-publication-manifests.md) | grant 同事务冻结内容 manifest |
| [adr/0022-outbox-projector.md](adr/0022-outbox-projector.md) | 原子投影、ack、失败和重建 |
| [adr/0023-http-consistency-and-auth.md](adr/0023-http-consistency-and-auth.md) | OIDC、事务、读一致性和游标 |
| [adr/0024-relations-remain-closed.md](adr/0024-relations-remain-closed.md) | G8-16C 延续及未来解除条件 |
| [independent-review-prompt.md](independent-review-prompt.md) | 另一 Codex 任务的完整独立审核提示词 |
| [implementation-start.md](implementation-start.md) | WP10.1 整改启动口令、设计 SHA、范围和停止线 |
| [implementation-start-wp10.2.md](implementation-start-wp10.2.md) | WP10.2 签署起点、授权范围和停止线 |
| [implementation-start-wp10.3.md](implementation-start-wp10.3.md) | WP10.3 签署起点、授权范围和停止线 |
| [implementation-start-wp10.4.md](implementation-start-wp10.4.md) | WP10.4 签署起点、授权范围和停止线 |
| [runtime-validation.md](runtime-validation.md) | WP10.1–WP10.4 可独立复跑的迁移/真实角色探针命令及凭据要求 |
| [validation-results-20260828.md](validation-results-20260828.md) | WP10.1 命令输出摘要、SQLSTATE 与结果校验索引 |
| [validation-results-wp10.2-20260828.md](validation-results-wp10.2-20260828.md) | WP10.2 实现、真实探针结果与证据索引 |
| [validation-results-wp10.3-20260830.md](validation-results-wp10.3-20260830.md) | WP10.3 实现、隔离迁移/真实角色探针结果与证据索引 |
| [validation-results-wp10.4-20260831.md](validation-results-wp10.4-20260831.md) | WP10.4 Public API、读取索引、真实角色与 HTTP 验收证据索引 |
| [SHA256SUMS](SHA256SUMS) | 除自身外全部冻结文件的内容校验和 |

## 明确状态

- WP10.1–WP10.5 的签署 SHA 依次记录为 `8b6ae4fc6e0b46e1cf724742ded6a3db70d7f3a5`、`2718ba2dc8f6773e088f5cfb8cfe90d5070aa7ae`、`10773f135ef5fca236db2eebee75fd6b15f3cd0f`、`19ee4f436179740c002f05f597e456d42b1a17db` 和 `4e15bdd8cdb92d4406cc46b38f8cef92320a1881`。这些记录证明阶段签署状态，但当前恢复仓库缺少 `34c57bcadfeb67053c4c47f8cde237a3af185ba8` 之前的 Git 对象，不能据此声称完整祖先链已恢复或已独立验证。
- R1 权限恢复修复已独立验收，并固定为普通提交 `64306280d5c2cd8eb5eef2977269a12e7297b8e2`。
- C/G10-27 容量、性能与发布新鲜度已独立 live 验收，并固定为普通提交 `40044f48cd92e7748f88fedde38108c3dd176f65`。
- D 阶段已完成审核包、G10-28/29 核查和总门禁缺口材料，独立材料审核接受其判断；完整历史链、最终候选全量复跑和 required CI 仍是未满足项。
- 上述 R1/C/D 记录不构成 `G10-GATE-10.6` 签署，也不授权 WP11。
