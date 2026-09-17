# G8 R2 冻结验收用例

- 冻结编号：`G8-FROZEN-20260821-02`
- 父基线：`a34acd3282001421de1376e6e62ca3d7cf0f4233`
- 用例：G8-01–G8-20
- 维度：正向、反向、并发、幂等、权限、迁移与可追溯性

所有“行数为 0”均指事务结束后的可见状态；所有角色拒绝项使用真实登录连接。

## G8-01 valid 分析同事务入队且与 selections 解耦（WP8.1）

前置：分别产生 valid `claim_extraction` 和 `entity_extraction`；`analysis_selections` 无当前行。再产生同类型第二条 valid 分析，并仅选择第一条。

预期：

- 每条 valid 分析各有且仅有一个 `resolve_claims` / `resolve_entities` job；
- key 分别为 `resolve-claims:{analysis_result_id}` / `resolve-entities:{analysis_result_id}`；
- job 的列与 payload 均声明 `knowledge.v2`；
- payload 含冻结字段及当时计算的 extraction anchor；
- analysis_result 与 job 同事务提交、同事务回滚；
- 无 selection 时入队，未被选择的第二条 valid 结果也入队；
- WP8 不 INSERT/UPDATE/DELETE `analysis_selections`，不产生 `resolve_relations`。

## G8-02 invalid、非目标类型与回滚不入队（WP8.1）

分别插入：invalid claim、valid summary、valid classification，以及插入 valid claim 后整事务回滚。

预期：零条对应 resolve job；回滚场景 analysis_result 与 job 均不可见；知识表无写入。

## G8-03 入队幂等、并发和 payload 冲突（WP8.1）

操作：

1. 对同一 analysis_result 并发调用私有 enqueue 路径；
2. 用相同 key 和完全相同 job_type/schema/payload 重入；
3. 用相同 key 但修改 `analysis_result_id`、`result_sha256`、`extraction_id` 或任意 payload 字段。

预期：

- (1)(2) 只有一行 job，返回同一 id，不创建 attempt，不 UPDATE 原 payload；
- (3) 失败，SQLSTATE `23505`，稳定错误 `knowledge_idempotency_payload_conflict`；
- 不能把冲突旧 job 当作新 analysis_result 已成功交接。

## G8-04 存量补偿与死信复用（WP8.1）

在触发器创建前构造 valid claim/entity、invalid claim 和 valid summary，并预置一条完全相同 job。执行迁移回填，再按返回 cursor 分页调用 `ops.reconcile_knowledge_jobs` 两轮。

预期：

- 每条目标 valid 分析恰好一个 job；invalid/summary 不入队；
- 第二轮 reconciliation 返回同一 job id 集且不改 payload；分页 cursor 不漏行、不重复页；
- payload 与触发器逐字段相同，含相同 anchor 状态；
- reconciliation 只写 jobs，不写知识表；
- 已 dead 的同一 job 只能走 `ops.requeue_dead_letter`，不得插入第二 job。

另在隔离事务预置同 key 不同 payload：迁移回填或 reconciliation 必须以 `knowledge_idempotency_payload_conflict` 失败，不能因“job 已存在”而跳过。

## G8-05 最小权限、租约和受控 metrics 收口（WP8.1）

使用真实角色执行：

| 操作 | 预期 |
|---|---|
| Worker/API 直接 INSERT/UPDATE/DELETE 知识表 | `42501` |
| Worker 直接 UPDATE `ops.job_attempts.metrics` | `42501` |
| Model governance / Publisher / Scheduler / Public reader 执行 `finish_knowledge_job` | `42501` |
| Worker 伪造或过期 lease 调用 lease guard / finish | `40001`，metrics 不变 |
| Worker 在隔离测试夹具中持 valid 空 claim result 的 `resolve_claims` lease，以合法 metrics succeeded 收口 | attempt 和 job 同事务闭环，metrics 写入；未调用 materialize |
| Worker 持非 resolve job lease调用 finish_knowledge_job | `42501` 或冻结的类型拒绝码 |
| metrics 含未知键、负数、正文、超长 sample 或 >64 KiB | `22023`，attempt 仍 running |

同时验证 Worker 对 `ops.model_runs` / `ops.prompt_versions` 仍无 SELECT，对 `analysis_results` 只有冻结的 SELECT。

该用例直接调用数据库函数验证基础设施，不把 `resolve_claims` 加入生产 Worker 领取配置，也不要求 handler 存在。

## G8-06 数据库一致性与 Entity Candidate 多证据地基（WP8.1）

在独立空白库验证升级、降级、回升；在另一个带历史/多 evidence 夹具的库验证回填和数据保护拒绝。随后验证下列约束：

- `claims.document_version_id NOT NULL`；AI origin 与 document version 复合 FK；
- `claim_evidence.document_version_id` 同时复合 FK 到 claim 和 span；
- AI Claim origin 必须 valid `claim_extraction`，且提交时至少一条 `supports`；
- Entity Candidate origin 必须 valid `entity_extraction`；
- `entity_candidate_evidence` 对 candidate/span 使用同一 document version 的双复合 FK；
- candidate 提交时至少一条关联 evidence；一个 candidate 可关联 20 条不同 span；
- legacy `entity_candidates.evidence_span_id` 可回填入 join 表，但 WP8 新行不使用该列；
- 无法推导 document version 或存在跨版本历史行时迁移 fail closed，不填伪值。
- 已有 candidate 多 evidence 时 downgrade 不得静默丢数据。
- `0002` 定义的原始 49 表仍完整，当前 schema 仅因新增 join 表变为 50；旧 validator 的历史约束未被删除。

跨版本、invalid origin、裸 AI Claim、无 evidence candidate 均以 `23503`/`23514` 失败且无残行。

## G8-07 extraction 唯一锚定（WP8.2）

三组夹具：

1. 同 document version/input hash 恰好一个 succeeded extraction；
2. 零匹配；
3. 两个不同 extractor name/version 的 succeeded extraction 具有同一 output hash。

预期 payload anchor 分别为：

- `matched` + 唯一 `extraction_id`；
- `missing` + `extraction_id=null`；
- `ambiguous` + `extraction_id=null`。

在 analyze 后插入不同 hash 的更新 extraction，不改变既有 payload。resolver 不使用 `ORDER BY ... DESC LIMIT 1`。missing/ambiguous 的非空结果最终分别映射稳定终态错误。

## G8-08 span 身份与五类基础映射（WP8.2）

对 text/html/pdf/video/audio 构造合法 locator。

预期：

- `0 <= start < end <= len(text)`，Unicode 码位、`[start,end)`；
- text/html 写 `char_*`；PDF 只写 `page_*`；媒体只写 `time_*`；
- `locator` 保存 `evidence-locator.v2` 完整 envelope；
- `locator_sha256` 对 canonical envelope 计算，包含 document version、extraction id、input hash 和 source locator；
- 同 extraction 同 locator 复用 span；
- 相同 document version/坐标但不同 extraction id 或 input hash 产生不同 span；
- 同一 Claim 内重复 locator 的首个 ordinal 接受、后续 ordinal 以 `locator_duplicate` 拒绝；
- 不放宽 `ck_evidence_locator_fields`。

## G8-09 PDF/媒体跨轴对应（WP8.2）

对锚定 `location_map` 计算：字符区间命中的 map row 集合 `C`，页码或时间区间命中的 map row 集合 `A`。

预期：

- PDF 和 audio/video 仅在 `C` 非空且 `C=A` 时通过；
- 字符只命中第 2 页但 payload 写第 3 页、字符命中两个 cue 但时间只命中一个、时间额外命中无关 cue，均拒绝；
- 缺失、格式错误、重复/重叠导致无法唯一判定的 location_map fail closed；
- 不猜最近页、最近 cue，不 clamp 轴；
- 原因码为 `locator_location_map_invalid` 或 `locator_cross_axis_mismatch`。

## G8-10 纯映射结果分类（WP8.2）

不调用 materialize/finish，只验证映射器返回值：

| 输入 | 分类 |
|---|---|
| valid `claims=[]` / `entities=[]` | `empty_valid_result`，不是错误 |
| 非空，至少一个候选仍有合法 evidence | `materializable`，保留拒绝计数 |
| 非空，全部候选无合法 evidence | `terminal_unmappable` |
| extraction missing / ambiguous 且非空 | 对应 terminal anchor error |

还覆盖 `end<=start`、越界、轴冲突、缺页/时间、跨轴不符、摘录 >8 KiB。WP8.2 门禁不要求任何 job 状态变化。

## G8-11 Claim 正向物化与精确 provenance（WP8.3）

合法 `resolve_claims` attempt 调用 `materialize_claim_bundle`。

预期：

- job payload、analysis_result、model_run、document version、result type、`ai.v1`、result hash、input hash 和 extraction 全部一致；
- bundle 中 claim ordinal、原始 claim 文本和 locator ordinal 与 `analysis_results.result.claims[]` 精确对应；
- `claim_text` 保存模型原值；fingerprint 由数据库私有函数按冻结 NFKC/空白规则计算，调用者不能覆盖；
- `claim_type='other'`、`assertion_status='reported'`；不复制来源可信度；
- `subject_entity_id IS NULL`；
- Claim、span、claim_evidence 的 document version 一致，至少一条 supports；
- job succeeded，metrics 计数准确，知识行与 attempt 同事务提交。

## G8-12 Claim 空、部分、全拒绝及 SAVEPOINT 闭环（WP8.3）

分别执行：

1. valid `claims=[]`；
2. 三个 claim 中一个可物化、两个 locator 不合法；
3. 非空结果全部不可定位；
4. materialize 触发一个已冻结的确定性 SQL RAISE。

预期：

- (1) job `succeeded`、零 Claim、`empty_valid_result=true`；
- (2) job `succeeded`、只提交成功 Claim，metrics 拒绝计数和样本正确；
- (3) `terminal_failure/knowledge_locator_unmappable`、零新行；
- (4) handler `ROLLBACK TO SAVEPOINT` 后调用 `finish_knowledge_job`，领域写入为 0，attempt 终态；
- lease `40001` 场景整事务回滚且不伪收口，由 WP4 过期恢复处理。

## G8-13 Claim 防篡改、幂等与至少一次执行（WP8.3）

逐项篡改 payload/bundle：model_run id、schema version、result hash、input hash、extraction id、claim ordinal/text、locator ordinal/content；以及遗漏一个未声明拒绝原因的候选。

预期：全部 fail closed，零新行，稳定 `knowledge_payload_mismatch` / `knowledge_bundle_mismatch` / anchor error。

同一 `(origin_analysis_result_id, ordinal)` 在重领或重放时返回既有不可变行，不更新内容、不新增 span/claim/evidence。并发与 lease expiry 后至多一个 attempt succeeded，所有 attempt 最终闭环。

## G8-14 Entity Candidate 多证据正向物化（WP8.4）

一个 candidate 带三条合法 locator，另一个带一条。执行 `resolve_entities`。

预期：

- 每个 candidate ordinal 一行 `entity_candidates`，状态 `pending`；
- join 表分别有 3 行和 1 行，evidence ordinal 与分析 JSON 一致；
- `entity_candidates.evidence_span_id IS NULL`；
- proposed name/type 与 JSON 精确对应，`candidate_payload` 保留版本化候选；
- `proposed_aliases=[]`，不臆造 identifier 或 description；
- 不创建 canonical entity，不因同名自动合并，不创建 relation；
- 不回填 Claim subject；
- job 与 metrics 成功闭环。

## G8-15 Entity 空、部分、全拒绝、篡改和幂等（WP8.4）

与 G8-12/G8-13 同构，使用 `entities=[]`、部分 locator 成功、非空全拒绝、多 evidence 遗漏/替换和重领夹具。

预期：

- 空数组 succeeded 且零 candidate；
- 部分成功只写 evidence 至少一条的 candidate；
- 非空全拒绝 terminal failure；
- 任一 source evidence ordinal 未被 accepted 或明确 rejected 时 bundle 失败；
- 同一 `(analysis_result_id, ordinal)` 不重复，join 表不丢证据、不重复；
- SAVEPOINT、metrics 和 lease 规则与 Claims 完全一致。

## G8-16 消费者激活与关系非范围（WP8.3 / 8.4 / 8.6）

### G8-16A Claims（WP8.3）

WP8.1/8.2 时触发器已排队但领取集合不含 resolve 类型。部署 claim handler 后才加入 `resolve_claims`。验证排队期间未被领取，激活后正常消费。

### G8-16B Entities（WP8.4）

同构验证 `resolve_entities`；在 entity handler 部署前不得加入领取集合。

### G8-16C Relations（WP8.6）

正常 analysis 路径从不入队 `resolve_relations`。若人工构造并误领该 job，通用分发器调用现有 `finish_job` 结束为 `terminal_failure`、`knowledge_relation_task_not_in_wp8`；relations 表无写入，禁止 no-op succeeded。

## G8-17 Merge 方向、canonical 与端点稳定（WP8.5）

owner 验收连接调用默认关闭的函数：active A 合并入 active B。

预期：

- merge event 为 A→B、`event_kind='merge'`；A merged、B active；
- `canonical(A)=B`；同名不同 id 不触发任何自动调用；
- alias、candidate、claim、relation、evidence FK 均保持原实体 id；
- 自合并、非 active source/target、非 canonical 端点、链超限均拒绝；
- audit event 与领域变化同事务。

## G8-18 Merge 并发与四节点防环（WP8.5）

验证三组并发：

1. `merge(A,B)` 与 `merge(A,C)`；
2. `merge(A,B)` 与 `merge(B,A)`；
3. 用 owner 构造 schema 允许但状态不一致的历史边 A→B、C→D，再并发 B→C 与 D→A。

预期：

- 每个写图事务首先获得固定 advisory lock `(824,1)`，再按 UUID 升序锁实体；
- 无死锁；同 source 至多一个 open merge；
- 四节点场景最多一个新边提交，另一个在锁内重读图后 `knowledge_merge_cycle` 或状态拒绝；
- 最终未撤销 merge 图无环。

## G8-19 Reverse 语义与运行时授权关闭（WP8.5）

对 A→B 执行 reverse。

预期：

- 原 merge 指向新 reverse row；reverse row 仍记录 A/B，但 `event_kind='reverse'`，不成 B→A 或 A→B 边；
- A 恢复 active（满足冻结前置时），`canonical(A)=A`；
- 关系端点不变；重复 reverse、reverse reverse row、用 `merge(B,A)` 冒充撤销均拒绝；
- Worker、API、Scheduler、Publisher、Model governance、Public reader 对 merge/reverse EXECUTE 全部 `42501`；
- active principal 但无 WP9 senior_reviewer 授权不能通过任何运行时入口调用。

## G8-20 阶段链、迁移、回归、CI 与证据（WP8.6）

预期：

- Git 历史为 G7 → R2 docs-only 设计提交 → WP8.1…8.6 已验收父提交链；
- 设计提交相对 G7 只含 `docs/wp8/**`；
- Alembic 从 0009 单 head 线性升级；在加载 WP8 业务夹具前的独立空白验证库中可 downgrade/upgrade，无改写旧 migration；生产/含多 evidence 数据的降级必须遵守 ADR-0009 的数据保护前置；
- WP3/WP4 validator 仍验证原冻结链和原 49 表集合，同时能够在 0010–0013 线性前缀之后运行；migration-chain 与 validate_wp8 精确验证最终 head；
- G8-01–G8-19 在最终 SHA 全量复跑；WP2–WP7 回归全绿；
- Ruff、strict mypy、pytest、coverage、Bandit、依赖和 secret 扫描不降级；
- runtime probe 按 WP3→WP8 顺序通过；
- required `quality/security/integration/gate` 全部 success 且绑定最终 SHA；
- 实现清单逐项校验，内层与外层哈希可复现；
- PR 不夹带下一阶段、旧系统或范围外文件。

## 固定规范

- Claim fingerprint：Unicode NFKC；去首尾空白；内部连续空白折叠为一个 U+0020；UTF-8 SHA-256 小写十六进制；不 casefold。
- AI Claim：`claim_type='other'`，`assertion_status='reported'`。
- Entity Candidate：保持 pending，不自动创建 entity。
- evidence 摘录：严格取锚定正文 `[start,end)`，UTF-8 大小不得超过 8192 bytes；超限拒绝，不截断。
- 任务结果和 metrics：见 ADR-0010 §6、ADR-0011 §5。

任一 required 用例失败，对应阶段门禁失败；后续阶段与 WP9 保持关闭。
