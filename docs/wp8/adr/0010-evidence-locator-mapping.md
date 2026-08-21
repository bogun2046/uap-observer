# ADR-0010：Evidence Locator、Extraction 锚定与 Span 身份

- 状态：Accepted for `G8-FROZEN-20260821-02`
- 日期：2026-08-21
- 前置：WP6 location_map、WP7 schemas、G3 locator CHECK

## 1. 背景

WP7 对五种 locator 都要求 `start/end`，但没有验证 `end>start`、边界或页/时间轴一致性。G3 的 `evidence_spans` 列布局则要求：text/html 只用字符列，PDF 只用页码列，audio/video 只用时间列。

另外，`model_runs` 只保存 `input_sha256`，没有 extraction id；而 `extractions` 的唯一键包含 extractor name/version，因此同一 document version 和 output hash 可以对应多行。只取“最新”或任意一行都不能证明模型实际使用的坐标空间。

## 2. Extraction 锚定

由 analysis_result 关联 model_run，取：

```text
document_version_id
model_runs.input_sha256
```

查询成功 extraction：

```text
document_version_id = analysis_result.document_version_id
outcome = 'succeeded'
output_sha256 = model_runs.input_sha256
```

并验证 extraction 的 `(text_object_id, storage_domain='derived', output_sha256)` 仍与 stored object 一致。按匹配数冻结：

| 匹配数 | anchor status | extraction_id | 非空结果处理 |
|---|---|---|---|
| 0 | `missing` | NULL | terminal `knowledge_extraction_missing` |
| 1 | `matched` | 唯一 id | 使用该行 |
| >1 | `ambiguous` | NULL | terminal `knowledge_extraction_ambiguous` |

禁止用 created_at、id、extractor version 或“最新”打破歧义。新增不同 hash 的 extraction 不改变已经入队的 payload。

合法空数组不需要 locator：即使 anchor missing/ambiguous，仍按 §6 成功零物化。

## 3. 公共字符坐标与 Span Identity

所有 locator 的 `start/end` 都表示模型实际消费正文的 Python `str` Unicode 码位索引，区间为左闭右开 `[start,end)`：

```text
0 <= start < end <= len(extracted_text)
```

不是 UTF-8 字节、grapheme、PDF 页内偏移或毫秒。

每个成功 span 保存下列 canonical envelope 到 `evidence_spans.locator`：

```json
{
  "locator_schema_version": "evidence-locator.v2",
  "document_version_id": "<uuid>",
  "extraction_id": "<uuid>",
  "input_sha256": "<64 hex>",
  "source_locator": {
    "locator_type": "pdf",
    "start": 120,
    "end": 180,
    "page_start": 3,
    "page_end": 3
  }
}
```

`source_locator` 只含 WP7 字段；可选 NULL 字段省略。materialize 函数把该对象转成 `jsonb` 后构造 envelope，调用 ADR-0011 冻结的数据库 hash 函数；Python 不提供最终 hash。

```text
locator_sha256 = core.compute_evidence_locator_sha256(envelope::jsonb)
```

数据库函数以 PostgreSQL 16 稳定 `jsonb` 文本表示的 UTF-8 字节计算 SHA-256。因此相同 document version 和坐标，只要 extraction id 或 input hash 不同，就不会复用旧 span。现有唯一约束 `(document_version_id, locator_sha256)` 保留；冲突时还要逐字段比较 envelope，防止 hash 冲突被误当成幂等。

## 4. 分类型列映射

| locator_type | 强类型列 | 额外要求 |
|---|---|---|
| text | `char_start=start`, `char_end=end` | page/time 全部缺席 |
| html | 同 text | page/time 全部缺席 |
| pdf | `page_start/page_end`；`char_*` NULL | page 必填，`page_end>=page_start`，time 缺席 |
| video | `time_start_ms/time_end_ms`；char/page NULL | time 必填，`time_end_ms>time_start_ms` |
| audio | 同 video | 同 video |

完整字符坐标始终保存在 envelope；PDF/媒体不把它们写入被 G3 禁止的强类型列。

`evidence_text` 严格取锚定正文 `[start,end)`。UTF-8 编码后不得超过 8192 bytes；超限拒绝，不截断。

## 5. `location_map` 跨轴一致性

location_map 必须是数组。相关 row 必须有合法、非空的 `char_start<char_end`，并有该 extraction 类型需要的 page/time 字段。格式错误、轴字段缺失或无法唯一判断时 fail closed。

区间相交统一为半开区间：

```text
[a,b) intersects [c,d) iff a < d AND c < b
```

### 5.1 PDF

按 location_map 数组 ordinal 标识 row：

- `C`：`[char_start,char_end)` 与 locator `[start,end)` 相交的 `pdf_page` row ordinal 集；
- `A`：row 的 `[page_start,page_end]` 与 locator `[page_start,page_end]` 相交的 ordinal 集。

只有 `C` 非空且 `C = A` 时通过。这样既拒绝“文本在第 2 页但声明第 3 页”，也拒绝声明额外无关页。页面间标准分隔符可以位于 map row 外，但 locator 不能只命中分隔符。

### 5.2 Audio / Video

- `C`：字符区间相交的 `subtitle_cue` row ordinal 集；
- `A`：cue `[time_start_ms,time_end_ms)` 与 locator 时间区间相交的 row ordinal 集。

只有 `C` 非空且 `C = A` 时通过。不能用任意相交 cue 为整体 span 背书，也不能让时间轴额外覆盖未引用 cue。

text/html 只需正文边界和轴冲突检查；不要求 locator 等于完整 HTML block。

## 6. Candidate、Locator 与任务结果

WP7 合法 Schema 允许 `claims=[]` / `entities=[]`，因此冻结：

| 输入结果 | 知识写入 | 任务结果 |
|---|---|---|
| valid 空数组 | 0 | `succeeded`，`empty_valid_result=true` |
| 单 locator 失败，但候选仍有至少一条合法 locator | 只写合法 evidence | 候选可物化 |
| 某候选所有 locator 失败 | 不写该候选 | 记录 rejected candidate |
| 非空结果至少一个候选物化 | 只写成功候选 | `succeeded`，metrics 记录部分拒绝 |
| 非空结果零候选物化 | 0 | `terminal_failure/knowledge_locator_unmappable` 或 anchor error |
| origin invalid / schema 不支持 / bundle 篡改 | 0 | 对应 terminal failure |

空数组不是 no-op 掩盖失败，而是模型给出的合法“未发现候选”。非空全拒绝才表示系统无法可靠物化。

每个 candidate 和 locator 使用源 JSON 的零基 ordinal；映射结果必须让全部源 ordinal 被“accepted”或带冻结原因的“rejected”覆盖，供 ADR-0011 bundle 校验和 metrics 使用。

同一 Claim 内重复的 source locator：按 locator ordinal 只接受第一次，后续 ordinal 以 `locator_duplicate` 拒绝，避免现有 `UNIQUE (claim_id,evidence_span_id)` 丢失计数。Entity Candidate 的 join 表有 evidence ordinal，可以保留重复 ordinal 指向同一 span。

## 7. 稳定原因码

- `locator_end_not_after_start`
- `locator_out_of_range`
- `locator_axis_conflict`
- `locator_pdf_page_missing`
- `locator_time_missing`
- `locator_page_range_invalid`
- `locator_time_range_invalid`
- `locator_location_map_invalid`
- `locator_cross_axis_mismatch`
- `locator_excerpt_too_large`
- `locator_duplicate`
- `knowledge_extraction_missing`
- `knowledge_extraction_ambiguous`
- `knowledge_extraction_mismatch`
- `knowledge_locator_unmappable`
- `knowledge_invalid_origin`
- `knowledge_schema_unsupported`
- `knowledge_payload_mismatch`
- `knowledge_bundle_mismatch`

日志不得包含 evidence 正文；只记录 analysis/extraction id、hash、candidate/locator ordinal 和原因码。

## 8. 不采用

- 取最新或任意同 hash extraction；
- 在不同 extraction 上复用仅由 locator 坐标计算的 span；
- 只检查页/时间字段存在；
- clamp 越界、猜最近页/cue、截断摘录；
- 把合法空数组送 dead letter；
- 把非空全拒绝标成 succeeded；
- 放宽 G3 locator CHECK。
