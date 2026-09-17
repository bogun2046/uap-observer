# WP8 R2 实施任务书：主张、证据、实体与知识交接

- 实施编号：`WP8-IMPL-20260821-02`
- 冻结标准：`G8-FROZEN-20260821-02`
- 父基线：G7-R2 `a34acd3282001421de1376e6e62ca3d7cf0f4233`
- 实施起点：本设计目录所在 docs-only 提交 `G8_R2_DESIGN_SHA`
- 当前状态：**设计已冻结；等待项目负责人授权 WP8.1**
- 实施者：Grok（或项目负责人指定的实施工程师）
- 架构与审核：Codex

## 1. 目标与范围

把 WP7 已校验并持久化的 valid `claim_extraction` / `entity_extraction` 结果，经 WP4 durable job 物化为可审核的内部知识对象：

```text
analysis_results
  -> resolve_claims / resolve_entities
  -> evidence_spans
  -> claims + claim_evidence
  -> entity_candidates + entity_candidate_evidence
```

WP8 不把 AI 输出当事实，不读写 `analysis_selections`，不写 `public`，不实现审核界面，也不交付 AI relation 物化。

代码布局跟随 WP5–WP7，新增 `platform/src/uap_platform/knowledge/`；本阶段不重构完整 `domains/` 树。

## 2. 权威设计

- [ADR-0008](adr/0008-knowledge-job-handover.md)：事务性交接、payload、补偿和消费者顺序
- [ADR-0009](adr/0009-claim-document-evidence-constraints.md)：Claim / Candidate / Evidence 一致性
- [ADR-0010](adr/0010-evidence-locator-mapping.md)：extraction、locator、span identity 和结果语义
- [ADR-0011](adr/0011-knowledge-write-authority.md)：权限、bundle、metrics 和事务
- [ADR-0012](adr/0012-entity-merge-state-machine.md)：合并、撤销、并发与授权
- [ADR-0013](adr/0013-relations-out-of-scope.md)：关系非范围

实施者发现冲突时必须停止并报告，不得修改 ADR 或选择替代架构。

## 3. 阶段链

```text
G7-R2 SHA
  -> G8 R2 docs-only design commit (G8_R2_DESIGN_SHA)
  -> WP8.1 accepted SHA
  -> WP8.2 accepted SHA
  -> WP8.3 accepted SHA
  -> WP8.4 accepted SHA
  -> WP8.5 accepted SHA
  -> WP8.6 / G8 accepted SHA
```

规则：

1. WP8.1 只能从项目负责人启动口令中的 `G8_R2_DESIGN_SHA` 开始。
2. 后续阶段只能从上一阶段的 Codex 已验收 SHA 开始。
3. 一个阶段完成、提交固定 SHA 并交付审核包后立即停止；审核期间不得 amend 或 force-push。
4. 审核不通过时只整改当前阶段，并形成新 SHA；不得开始下一阶段。
5. 不得从 G7 或设计提交并行分叉多个 WP8.x 后再合并。

## 4. 阶段交付

### WP8.1：交接、数据库地基与权限收口

起点：`G8_R2_DESIGN_SHA`。建议迁移：`0010_knowledge_foundation`，`down_revision=0009_model_governance_boundaries`。

必须交付：

- 私有 payload builder、`ops.enqueue_followup_job` 与 `AFTER INSERT` 触发器；
- `knowledge.v2` payload、严格幂等冲突检查；
- 迁移窗口存量回填及 `ops.reconcile_knowledge_jobs` 受控补偿入口；
- `ops.require_active_resolution_job_lease`；
- `ops.finish_knowledge_job(..., p_metrics jsonb)`；
- 私有 `core.compute_evidence_locator_sha256`；
- 私有 `core.compute_claim_fingerprint`；
- Claim / Claim Evidence 复合文档版本约束；
- `core.entity_candidate_evidence`、复合 FK、回填及至少一条 evidence 的延迟约束；
- AI Claim / Entity Candidate 必须来自 valid 对应分析的数据库约束；
- 从 `uap_worker` 与 `uap_api` 收回知识表直接 DML；Worker 只获得完成 WP8 所需的最小 SELECT；
- 更新既有迁移兼容检查：WP3 的原始 49 表集合继续逐项断言，同时当前总表数变为 50；WP3/WP4 验证器改为验证其历史 revision suffix，不再假定 0009 永远位于 head；
- `verify-migration-chain.sh` 与 runtime head 断言更新到本阶段唯一 head，后续有 migration 的阶段同步推进；不得删除旧约束检查；
- `validate_wp8.py` 的 WP8.1 静态断言与数据库契约测试。

本阶段不交付：locator 生产映射器、materialize 函数、handler、merge 函数、CI 接线。不得要求 materialize 成功用例通过。

迁移数据策略必须 fail closed：先加可空列、可验证回填、检查不可推导或跨版本历史行计数为 0，再设 NOT NULL / VALIDATE CONSTRAINT；不得臆造 document_version。

### WP8.2：Evidence locator 与 extraction 锚定

起点：WP8.1 已验收 SHA。原则上无迁移。

必须交付纯知识映射库和测试：

- 固定 extraction 锚定状态 `matched/missing/ambiguous`；
- `evidence-locator.v2` envelope 与 span SHA-256；
- text/html/pdf/video/audio 分类型映射；
- PDF 与音视频的字符轴—页码/时间轴双向对应；
- candidate / locator ordinal 分类；
- 合法空数组、部分成功、非空全拒绝的结果分类；
- 摘录 8 KiB 上限与稳定原因码。

仍不得领取 `resolve_claims` / `resolve_entities`，不得写知识表，不得用不存在的 handler 证明成功。

### WP8.3：Claims 物化

起点：WP8.2 已验收 SHA。建议迁移：`0011_claim_materialization`。

必须交付：

- `core.materialize_claim_bundle`；
- bundle 对 job、analysis_result、model_run、`ai.v1`、result hash、candidate/locator ordinal 的精确校验；
- Claim fingerprint、缺省值、支持证据和幂等写入；
- `claims.subject_entity_id` 保持 NULL；WP8 不做实体解析或回填；
- `resolve_claims` handler；
- 空数组成功、部分成功、非空全拒绝终态失败；
- SAVEPOINT + `finish_knowledge_job` 原子收口；
- handler 部署完成后才把 `resolve_claims` 加入 Worker 领取列表。
- migration-chain / WP8 head 断言推进到 `0011_claim_materialization`。

不得同时实现实体 handler。

### WP8.4：Entities 物化

起点：WP8.3 已验收 SHA。建议迁移：`0012_entity_materialization`。

必须交付：

- `core.materialize_entity_bundle`；
- 每个候选 1–20 条 evidence 全量写入 `entity_candidate_evidence`；
- `proposed_aliases=[]`（WP7 无 alias 字段），不得臆造 identifier/description；
- `entity_candidates.evidence_span_id` 对 WP8 新行保持 NULL，不作为权威；
- 同名实体不自动合并；本阶段只创建 pending candidate，不创建 canonical entity；
- 不 UPDATE WP8.3 Claim 的 `subject_entity_id`；
- 与 Claims 同构的 provenance、幂等、空数组、部分成功、全拒绝和事务收口；
- handler 部署完成后才把 `resolve_entities` 加入领取列表。
- migration-chain / WP8 head 断言推进到 `0012_entity_materialization`。

不得创建 relation，也不得实施 merge。

### WP8.5：Entity merge/reverse 状态机（默认关闭）

起点：WP8.4 已验收 SHA。建议迁移：`0013_entity_merge_state_machine`。

必须交付：

- `entity_merge_events.event_kind` 与 `uq_open_merge_source`；
- `core.canonical_entity_id`；
- `core.merge_entities` / `core.reverse_entity_merge`；
- 固定图级 `pg_advisory_xact_lock(824, 1)`，随后 UUID 升序实体行锁；
- 循环、长链、重复 source、撤销与审计；
- 不改写 candidate、alias、claim、relation 或 evidence FK；
- 对所有登录运行时角色保持 `REVOKE EXECUTE`。
- migration-chain / WP8 head 断言推进到 `0013_entity_merge_state_machine`。

本阶段只证明状态机正确且默认不可调用，不宣称 senior_reviewer API 已交付。WP9 冻结授权绑定前不得开放运行时执行权。

### WP8.6：运行态探针、CI 与冻结证据

起点：WP8.5 已验收 SHA。无新领域行为。

必须交付：

- `wp8_runtime_probe.py`，按 `WP3 -> WP4 -> WP5 -> WP6 -> WP7 -> WP8` 顺序执行；
- `validate_wp8.py` 完整检查；
- Makefile 与 `.github/workflows/platform-ci.yml` 接入 WP8；
- `docs/wp8/**` workflow path；
- G8-01–G8-20 全量复跑；
- required `quality/security/integration/gate` 全绿；
- 实现证据清单、内层和外层 SHA-256。

## 5. 独立门禁映射

| 门禁 | 本阶段必须通过 | 通过后才可 |
|---|---|---|
| `G8-GATE-8.1` | G8-01–G8-06 | 授权 WP8.2 |
| `G8-GATE-8.2` | G8-07–G8-10 | 授权 WP8.3 |
| `G8-GATE-8.3` | G8-11–G8-13、G8-16A | 授权 WP8.4 |
| `G8-GATE-8.4` | G8-14、G8-15、G8-16B | 授权 WP8.5 |
| `G8-GATE-8.5` | G8-17–G8-19 | 授权 WP8.6 |
| `G8-GATE-8.6` | G8-16C、G8-20，且 G8-01–G8-19 全量复跑 | 签署 G8、开启 WP9 |

## 6. 全阶段实现约束

1. 不改写 `0001`–`0009`；Alembic 只能追加单一线性 head。
2. 既有 validator 的前向兼容修改只能把已验收 revision 链改成“必须存在的连续 suffix/子链”，不得删除 WP2–WP7 原约束；当前 WP8 head 的精确性由 `validate_wp8.py` 和 migration-chain 脚本承担。
3. 不改 WP7 Provider、Prompt、Schema、model-io 或 append-only 语义；WP8 接受实际持久化 `analysis_results.schema_version='ai.v1'`。
4. 不新增 `ops.model_runs.extraction_id`；通过 `input_sha256` 唯一锚定。
5. 不读取或写入 `analysis_selections` 作为物化前置。
6. 不扫描 `analysis_results` 后直接写知识表；补偿入口只入队。
7. `uap_worker`/`uap_api` 不得对知识表裸 DML；写入只经冻结函数。
8. `finish_knowledge_job` 与领域写入必须在同一事务；预期 SQL 错误按 ADR-0011 使用 SAVEPOINT。
9. 领取类型集合必须是已部署 handler 集合的子集。
10. 日志和 metrics 只含 ID、哈希、ordinal、计数与稳定原因码，不含全文、Prompt 或原始 Provider 响应。
11. 不调用收费 Provider；验收使用固定脱敏响应和本地对象。
12. 不降低覆盖率、Bandit、mypy、ruff 或 secret scanning 标准，不用宽泛 `noqa`。

## 7. 不做事项

- `analysis_selections`、审核后台、review case、publication grants（WP9）；
- 为 merge/reverse 绑定 senior_reviewer 登录授权（WP9）；
- `public` 投影、公开 API、搜索（WP10）；
- 历史 SQLite 全量迁移（WP11）；
- `relation_extraction` 或 `resolve_relations` 成功路径；
- 同名自动合并、自动 canonical entity 创建；
- 第二套队列、常驻无 attempt 扫描写入器；
- 在遗留 `src/uap_observer/` 添加 WP8 功能。

## 8. 每阶段交付与停止

实施者完成当前阶段后必须提交：

- 起点 SHA、最终 HEAD SHA、分支、PR、CI run；
- 文件清单与任务书逐项映射；
- 迁移 upgrade/downgrade/upgrade 证据；
- 测试命令、结果、SQLSTATE、角色矩阵与并发时间线；
- 阶段验收用例逐项结果；
- `git status --short`、本地/远端/PR HEAD 一致性；
- 明确列出未实施的后续阶段内容。

交付后立即停止。只有项目负责人转告 Codex 已通过并给出下一阶段完整启动口令，才可继续。

## 9. WP8 完成定义

仅当六个独立门禁均签署、G8-01–G8-20 在最终 SHA 全绿、迁移链和阶段父提交链完整、required CI 全绿且证据清单校验通过，才可登记：

```text
G8 技术验收通过；WP9 可由项目负责人另行授权。
```
