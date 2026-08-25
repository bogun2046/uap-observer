# WP9 实施任务书：审核、授权绑定与发布授权

- 实施编号：`WP9-IMPL-20260825-01`
- 冻结标准：`G9-FROZEN-20260825-01`
- 父基线：G8-GATE-8.6 `8550b8fe2d3322428fc9487e91aeb830425b0ed1`
- 实施起点：本设计目录所在 docs-only 提交 `G9_DESIGN_SHA`
- 当前状态：**设计待 Codex 冻结；等待项目负责人授权 WP9.1**
- 实施者：Grok（或项目负责人指定的实施工程师）
- 架构与审核：Codex

## 1. 目标与范围

把 WP8 已物化的内部知识对象纳入可审计审核：

```text
person principal + role_binding
  -> SET LOCAL uap.principal_id
  -> open review case (document|claim|entity)
  -> append decision
  -> publication grant + outbox
```

以及：当前分析选择、candidate 晋升为 `core.entities`、senior_reviewer 包装 merge/reverse、手工 Claim 与 subject 绑定。

WP9 不写 `public`，不交付公开 API/搜索，不交付 AI relation 成功路径。

代码布局跟随 WP5–WP8，新增 `platform/src/uap_platform/review/`。

## 2. 权威设计

- [ADR-0014](adr/0014-review-session-and-write-authority.md)
- [ADR-0015](adr/0015-review-case-and-decision-lifecycle.md)
- [ADR-0016](adr/0016-publication-grants-and-outbox.md)
- [ADR-0017](adr/0017-analysis-selection-and-candidate-promotion.md)
- [ADR-0018](adr/0018-authorized-entity-merge.md)
- [ADR-0019](adr/0019-manual-claims-and-subject-binding.md)

实施者发现冲突时必须停止并报告，不得修改 ADR 或选择替代架构。不得修改 ADR 0001–0013、migration 0001–0013。

## 3. 阶段链

```text
G8 signed SHA 8550b8fe…
  -> G9 docs-only design commit (G9_DESIGN_SHA)
  -> WP9.1 accepted SHA
  -> WP9.2 accepted SHA
  -> WP9.3 accepted SHA
  -> WP9.4 accepted SHA
  -> WP9.5 accepted SHA
  -> WP9.6 / G9 accepted SHA
```

规则：

1. WP9.1 只能从项目负责人启动口令中的 `G9_DESIGN_SHA` 开始。
2. 后续阶段只能从上一阶段的 Codex 已验收 SHA 开始。
3. 一个阶段完成、提交固定 SHA 并交付审核包后立即停止；审核期间不得 amend 或 force-push。
4. 审核不通过时只整改当前阶段，并形成新 SHA；不得开始下一阶段。
5. 不得从 G8 或设计提交并行分叉多个 WP9.x 后再合并。

## 4. 阶段交付

### WP9.1：会话绑定与写入收口

起点：`G9_DESIGN_SHA`。建议迁移：`0014_review_session_authority`，`down_revision=0013_entity_merge_state_machine`。

必须交付：

- `SET LOCAL` GUC `uap.principal_id` / `uap.request_id` 读取约定；
- `audit.require_active_role`；
- `audit.append_audit_event`（若与现有 INSERT 权限重复，则仅内部使用、不扩大登录 DML）；
- senior_reviewer 蕴含 reviewer；
- `uap_api` 获得最小 EXECUTE；Worker/Publisher/Scheduler/public reader 无审核函数 EXECUTE；
- `uap_api` 仍无 `audit.review_*` 表 DML；
- `validate_wp9.py` 的 WP9.1 静态断言与契约测试；
- migration-chain / WP9 head 断言推进到 `0014_review_session_authority`；WP3 原 49 表集合继续逐项断言，总表数仍为 50，除非本阶段证明必须加表（默认不加表）。

### WP9.2：Case 生命周期

起点：WP9.1 已验收 SHA。建议迁移：`0015_review_case_lifecycle`。

必须交付：`open_review_case` / `assign_review_case` / `close_review_case`；relation 拒绝；唯一开 case；G9-06–G9-09。

### WP9.3：决定、Grant、Outbox

起点：WP9.2 已验收 SHA。建议迁移：`0016_review_decisions_and_grants`。

必须交付：`record_review_decision`；approve/revise→grant；withdraw→撤回 grant；`ops.enqueue_publication_outbox`；禁止 publish job 与 `public` 写入；自审隔离；G9-10–G9-16。

### WP9.4：Selection 与晋升

起点：WP9.3 已验收 SHA。建议迁移：`0017_selection_and_promotion`。

必须交付：`select_analysis_result`；`accept_entity_candidate`；`bind_entity_candidate`；同名不自动合并；不读 selection 作为 WP8 物化前置；G9-17–G9-19。

### WP9.5：授权 Merge

起点：WP9.4 已验收 SHA。建议迁移：`0018_authorized_entity_merge`。

必须交付：`audit.apply_entity_merge` / `apply_entity_merge_reverse`；登录角色仍无核心 merge EXECUTE；G9-20–G9-22；G8-19 复跑。

### WP9.6：手工 Claim、subject 绑定、应用服务与门禁

起点：WP9.5 已验收 SHA。建议迁移：`0019_manual_claims_and_subject_binding`。

必须交付：

- `create_manual_claim`、`bind_claim_subject_entity`、最后一条 evidence 的受控路径；
- `uap_platform/review/` 应用服务；
- `wp9_runtime_probe.py` 与 orchestrator 接入 `WP3 -> … -> WP8 -> WP9`；
- Makefile 与 `platform-ci.yml` 接入 WP9；
- G9-01–G9-26 全量复跑，且 G8-16C 仍 fail-closed；
- required `quality/security/integration/gate` 全绿。

## 5. 独立门禁映射

| 门禁 | 本阶段必须通过 | 通过后才可 |
|---|---|---|
| `G9-GATE-9.1` | G9-01–G9-05 | 授权 WP9.2 |
| `G9-GATE-9.2` | G9-06–G9-09 | 授权 WP9.3 |
| `G9-GATE-9.3` | G9-10–G9-16 | 授权 WP9.4 |
| `G9-GATE-9.4` | G9-17–G9-19 | 授权 WP9.5 |
| `G9-GATE-9.5` | G9-20–G9-22 | 授权 WP9.6 |
| `G9-GATE-9.6` | G9-23–G9-26，且 G9-01–G9-22 全量复跑 | 签署 G9、开启 WP10 |

## 6. 全阶段实现约束

1. 不改写 `0001`–`0013`；Alembic 只能追加单一线性 head。
2. 不改 ADR 0001–0013。
3. 不新增登录角色。
4. 不把 `finish_knowledge_job` 扩展到 relation。
5. 不写 `public.*`。
6. 不调用收费 Provider。
7. 日志和 audit metadata 只含 ID、哈希、枚举与稳定原因码，不含全文、Prompt 或原始 Provider 响应。
8. 不降低覆盖率、Bandit、mypy、ruff 或 secret scanning 标准。

## 7. 不做事项

- `public` 投影、公开 API、搜索（WP10）；
- 历史 SQLite 全量迁移（WP11）；
- `relation_extraction` 或 `resolve_relations` 成功路径；
- 同名自动合并；
- FastAPI/OIDC 生产鉴权服务器；
- 在遗留 `src/uap_observer/` 添加 WP9 功能。

## 8. 每阶段交付与停止

实施者完成当前阶段后必须提交：起点 SHA、最终 HEAD SHA、分支、文件清单、upgrade/downgrade/upgrade 证据、G9 用例逐项结果、`git status --short`。交付后立即停止。
