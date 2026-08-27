# ADR-0009：Claim、Entity Candidate 与 Evidence 的数据库一致性

- 状态：Accepted for `G8-FROZEN-20260821-02`
- 日期：2026-08-21
- 前置：WP1 data model、G3 `0002`/`0004`、ADR-0005

## 1. 背景

G7 基线存在三处无法仅靠应用代码保证的不变量：

- `core.claims` 与 `core.claim_evidence` 没有直接 document version 载体；
- AI Claim 未被数据库强制要求 valid claim origin 与 supports evidence；
- WP7 `EntityCandidate.evidence` 允许 1–20 条 locator，但 `entity_candidates` 只有一个可空 `evidence_span_id`。

WP8 使用受控函数写入，但裸 SQL、迁移错误或并发仍可能绕过 Python，因此跨版本、origin 和 evidence 完整性必须由 PostgreSQL 强制。

## 2. Claim 锚定 document version

`core.claims` 追加：

```text
document_version_id uuid NOT NULL
  REFERENCES core.document_versions(id)
UNIQUE (id, document_version_id)
```

AI Claim 追加复合 FK：

```text
(origin_analysis_result_id, document_version_id)
  -> core.analysis_results(id, document_version_id)
```

保留 `(origin_analysis_result_id, ordinal)` 唯一索引，并冻结：

```text
(origin_analysis_result_id IS NULL) = (ordinal IS NULL)
(origin_analysis_result_id IS NULL) = (created_by IS NOT NULL)
```

即 AI Claim 有 origin/ordinal、无人工 created_by；手工 Claim 无 origin/ordinal、必须有 actor。WP8 只创建 AI Claim。

## 3. Claim 与 Evidence 同版本

`core.claim_evidence` 追加 `document_version_id uuid NOT NULL`，并建立：

```text
FOREIGN KEY (claim_id, document_version_id)
  REFERENCES core.claims(id, document_version_id)

FOREIGN KEY (evidence_span_id, document_version_id)
  REFERENCES core.evidence_spans(id, document_version_id)
```

跨文档版本绑定必须在数据库层失败，不由 Python 比较后补删。

## 4. AI Claim origin 与 supports

DEFERRABLE 约束触发器要求：

- 非空 origin 对应 `analysis_results.result_type='claim_extraction'`；
- `validation_status='valid'`；
- 每个 AI Claim 在事务提交时至少一条 `claim_evidence.support_type='supports'`。

触发器覆盖 Claim INSERT/UPDATE 以及 claim_evidence UPDATE/DELETE。手工 Claim 的 evidence 规则留给 WP9。

## 5. Entity Candidate 多 Evidence

新增真实关联表：

```text
core.entity_candidate_evidence (
  id uuid PRIMARY KEY,
  entity_candidate_id uuid NOT NULL,
  evidence_span_id uuid NOT NULL,
  document_version_id uuid NOT NULL,
  evidence_ordinal integer NOT NULL CHECK (evidence_ordinal >= 0),
  created_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (entity_candidate_id, evidence_ordinal),
  FOREIGN KEY (entity_candidate_id, document_version_id)
    REFERENCES core.entity_candidates(id, document_version_id),
  FOREIGN KEY (evidence_span_id, document_version_id)
    REFERENCES core.evidence_spans(id, document_version_id)
)
```

为此给 `core.entity_candidates` 增加 `UNIQUE (id, document_version_id)`。

设计语义：

1. `evidence_ordinal` 是 WP7 `candidate.evidence[]` 的零基 ordinal；
2. 同一 locator 在源数组中重复时允许多个 ordinal 指向同一 span，避免丢失 provenance；
3. 每个来自分析的 candidate 在提交时至少一条 join row，使用 DEFERRABLE 约束触发器保证；
4. candidate 的 analysis_result 必须 valid `entity_extraction`；
5. `entity_candidates.evidence_span_id` 保留为 legacy 兼容列，不删除、不作为 WP8 权威；
6. WP8 新 materialize 行把 legacy 列设为 NULL，所有读写使用 join 表；
7. WP9 若要删除最后一条 evidence，必须先按未来审核状态机处理，不能绕过延迟约束。

## 6. Evidence Span

保留 G3-R2 `ck_evidence_locator_fields`，不允许：

- PDF 写 `char_*`；
- audio/video 写字符或页码列；
- text/html 写页码或时间列。

WP8 新 span 必须有非空锚定 extraction（由 materialize 函数保证），并按 ADR-0010 的 `evidence-locator.v2` envelope 计算 hash。基线列仍保持可空，以兼容明确标记的历史/人工 span。

## 7. 迁移与历史数据

迁移不得假设生产表为空，使用以下 fail-closed 顺序：

1. 先添加可空新列和未验证约束；
2. AI Claim 的 document version 从 origin analysis_result 推导；
3. 手工 Claim 仅当既有 evidence spans 全部指向唯一 document version 时推导；无 evidence 或跨多个版本则中止迁移并报 `knowledge_claim_backfill_required`；
4. claim_evidence 的 document version 从 claim 推导，并验证 span 同版本；不一致则中止；
5. 新建 candidate evidence 表；对 legacy 非空 `evidence_span_id` 回填 `evidence_ordinal=0`；
6. 对无 legacy evidence 的既有 candidate，若不能提供明确修复数据则中止，不创建伪 span；
7. 验证所有 FK/触发器后再设 NOT NULL；
8. downgrade 只撤销 WP8 新对象和列，不改写 `0002`；如果任一 candidate 有多条 join evidence，downgrade 必须明确拒绝，除非操作员已完成备份和经独立审批的数据迁移，禁止静默压缩为 legacy 单值列。

实现任务书要求：在尚未加载 WP8 多 evidence 业务夹具的独立空白验证库执行 upgrade/downgrade/upgrade；在带历史夹具环境验证 upgrade fail-closed/backfill；另用多 evidence 夹具验证 downgrade 数据保护拒绝。

## 8. 禁止事项

- 不给 Claim 或 Candidate 增加 job 状态；
- 不复制来源可信度为 Claim 真值；
- 不把 join 表压缩回“第一条 evidence”；
- 不用 JSON-only evidence 替代真实 FK；
- 不放宽 locator CHECK；
- 不在提交后用补偿删除修复违反约束的行。

## 9. 后果

- Claim、Candidate、Evidence 和来源分析均有可由 SQL 探针证明的同版本关系；
- Entity Candidate 的 1–20 条 evidence 完整、可排序、可追溯；
- 需要 WP8.1 新表、复合约束、延迟触发器和安全回填。
