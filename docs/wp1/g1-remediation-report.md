# G1 阻断缺陷整改报告

- 整改单：`G1-REMEDIATION-20260812-01`
- 来源：`G1-REJECT-20260811-01`
- 状态：整改完成，提交独立复验
- 门禁：复验通过前不得进入 WP2

## 整改对应

| 缺陷 | 整改 | 验证 |
|---|---|---|
| G1-D01 详情 DTO 无法校验 | 三个详情 Schema 全部扁平化并保留 `additionalProperties: false` | OpenAPI 3.1 规范校验；三个真实正例通过；三个额外字段反例被拒绝 |
| G1-D02 对象去重语义不可实现 | 新增 `core.stored_objects`，以 `(storage_domain, content_sha256)` 唯一；artifact、extraction、model run 分别使用 `raw/derived/model_io` 域约束复合外键 | `stored_object_registry` 跨文档检查通过 |
| G1-D03 发布授权覆盖不全 | 授权拆为文档、Claim、实体、关系四张真实外键表；公开四类表分别引用授权；新增 `public.document_entities` 及已授权 basis | `publication_authorization_coverage` 检查通过 |
| G1-D04 Worker 公开权限冲突 | `uap_worker.public` 改为 `—`；常驻运行时仅 `uap_publisher` 可写 public | 权限矩阵解析检查通过 |
| G1-D05 AI 选择和实体来源不完整 | selection 改用 `(analysis_result_id, document_version_id, result_type)` 复合外键；新增 `entity_candidates` 来源/证据/解析实体模型 | 两项跨文档复合约束检查通过 |
| G1-D06 检查缺少语义 | 引入 PyYAML、jsonschema Draft 2020-12、openapi-spec-validator；增加正反实例、逻辑表去重、对象/授权/AI/权限一致性检查 | 24/24 检查通过 |

## 复现命令

```bash
python3 -m pip install --target /private/tmp/uap-wp1-schema-tools \
  -r tools/requirements-wp1-validation.txt
.venv/bin/ruff check tools/validate_wp1_design.py
.venv/bin/python tools/validate_wp1_design.py
shasum -a 256 -c artifacts/wp1-design-remediation-20260812/MANIFEST.sha256
```

## 证据

- `artifacts/wp1-design-remediation-20260812/design-validation.json`
- `artifacts/wp1-design-remediation-20260812/MANIFEST.sha256`
- `docs/wp1/openapi-examples.json`
- `tools/requirements-wp1-validation.txt`

整改人员只提交自检结果，不代替架构、数据、安全负责人的独立复验结论。
