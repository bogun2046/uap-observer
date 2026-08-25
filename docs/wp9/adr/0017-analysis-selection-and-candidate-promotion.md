# ADR-0017：分析选择与 Entity Candidate 晋升

- 状态：Proposed for `G9-FROZEN-20260825-01`
- 日期：2026-08-25
- 前置：0002 `analysis_selections` / `entity_candidates` / `entities`、ADR-0008、ADR-0012、WP8.4

## 1. 背景

WP8 对每条 valid claim/entity 分析都入队物化，**禁止** JOIN `analysis_selections`。0002 已有 `analysis_selections`：每个 `(document_version_id, result_type)` 至多一条 `superseded_at IS NULL`。WP8.4 只写 `entity_candidates`，不 `INSERT core.entities`。entity review case 的 subject 却是 `core.entities.id`。

## 2. 决策：Selection 只是当前展示指针

`audit.select_analysis_result(p_analysis_result_id, p_reason)`：

1. `require_active_role('reviewer')`；
2. 目标必须 `validation_status='valid'`，`result_type` 为 `claim_extraction` 或 `entity_extraction`（其它类型 `22023` / `review_selection_type_unsupported`）；
3. 将同 `(document_version_id, result_type)` 的当前行 `superseded_at=clock_timestamp()`；
4. 插入新行：`selected_by`=GUC principal；
5. 不入队、不重放物化、不修改 claims / candidates。

invalid / pending 结果不可选。WP8 交接触发器与 `finish_knowledge_job` 保持不读 selections。

## 3. 决策：显式晋升 candidate → entity

`audit.accept_entity_candidate(p_candidate_id, p_reason)`：

1. `require_active_role('reviewer')`；
2. candidate 存在，`status='pending'`，其 `analysis_result` valid `entity_extraction`；
3. 至少一条 `entity_candidate_evidence`；
4. **插入新的** `core.entities` 行：
   - `canonical_name` = candidate `proposed_name`；
   - `entity_type` = candidate `proposed_entity_type`；
   - `status='active'`；
   - 不复制 identifier（candidate 无 identifier 字段则保持 NULL）；
5. 将 candidate `status='resolved'`，`resolved_entity_id`=新实体；
6. 不因库中已有同名 active 实体而复用或合并；
7. 不调用 `core.merge_entities`；
8. 不自动打开 entity review case（由调用方随后 `open_review_case('entity', new_id, ...)`）。

绑定到已有实体必须另用 `audit.bind_entity_candidate(p_candidate_id, p_entity_id, p_reason)`：

- 目标实体必须 `status='active'` 且为当前 canonical（`core.canonical_entity_id(p_entity_id)=p_entity_id`）；
- 仍不自动 merge；
- 同名不是充分条件。

拒绝：

- 已 resolved/rejected candidate；
- merged/retired/disputed 实体作为 bind 目标；
- 用 candidate id 打开 entity review case。

## 4. 不做

- 同名自动合并或自动 canonical 创建；
- 把 selection 当作 resolve job 前置；
- 回填 WP8 已物化行的 selection；
- 晋升时写 publication grant。
