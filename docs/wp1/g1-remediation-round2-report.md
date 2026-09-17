# G1 第二轮复验阻断缺陷补充整改报告

- 整改单：`G1-REMEDIATION-R2-20260812-01`
- 来源：`G1-REJECT-R2-20260812-01`
- 状态：补充整改完成，提交第三次独立复验
- 门禁：第三次独立复验通过前不得进入 WP2

## 整改对应

| 缺陷 | 补充整改 | 新增验证 |
|---|---|---|
| G1-R2-D01 C4、权限与分层冲突 | 在容器图、时序图、部署说明、模块规则和目标代码布局中增加独立 Publisher；普通 Worker 不加载 publishing handler 且不写 public；模型调用事实写 `ops.model_runs`，结构化结果写 core | `publisher_process_boundary`、`model_run_ops_layer_flow` |
| G1-R2-D02 关系字段缺少外键 | 为 `entity_aliases.source_document_version_id` 声明文档版本外键；为三张标签关联表的 `origin_analysis_result_id` 声明分析结果外键 | `dictionary_relationship_fk_completeness` |
| G1-R2-D03 关系授权闭合不足 | `public.relations.relation_grant_id` 增加唯一约束；授权表以 `(withdrawn_by_decision_id, review_case_id)` 复合外键限制撤回决定来自同一审核案件 | `relation_grant_withdrawal_closure` |

## 复现命令

```bash
python3 -m pip install --target /private/tmp/uap-wp1-schema-tools \
  -r tools/requirements-wp1-validation.txt
.venv/bin/ruff check tools/wp0_baseline.py tools/validate_wp1_design.py
.venv/bin/python tools/validate_wp1_design.py
shasum -a 256 -c artifacts/wp1-design-remediation-r2-20260812/MANIFEST.sha256
```

## 证据

- `artifacts/wp1-design-remediation-r2-20260812/design-validation.json`
- `artifacts/wp1-design-remediation-r2-20260812/MANIFEST.sha256`
- `docs/wp1/architecture.md`
- `docs/wp1/data-model.md`
- `docs/wp1/module-boundaries.md`
- `tools/validate_wp1_design.py`

本报告只证明实现侧已完成补充整改和开发自检，不替代架构、数据、安全负责人的独立复验结论。
