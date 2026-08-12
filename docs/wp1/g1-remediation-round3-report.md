# G1 第三次复验阻断缺陷补充整改报告

- 整改单：`G1-REMEDIATION-R3-20260812-01`
- 来源：`G1-REJECT-R3-20260812-01`
- 状态：补充整改完成，提交第四次独立复验
- 门禁：第四次独立复验通过前不得进入 WP2

## 整改对应

| 缺陷 | 补充整改 | 新增验证 |
|---|---|---|
| G1-R3-D01 恢复校验引用已删除字段 | 恢复演练改为经 `ingest.artifact_versions.stored_object_id -> core.stored_objects.id` 关联，将 `core.stored_objects.object_key/content_sha256/byte_length` 与对象存储清单逐项核对 | `recovery_object_registry_consistency` 同时检查服务目标中的新路径、旧路径消失及数据模型中的对象登记/外键/字段所有权 |

## 复现命令

```bash
python3 -m pip install --target /private/tmp/uap-wp1-schema-tools \
  -r tools/requirements-wp1-validation.txt
.venv/bin/ruff check tools/wp0_baseline.py tools/validate_wp1_design.py
.venv/bin/python tools/validate_wp1_design.py
shasum -a 256 -c artifacts/wp1-design-remediation-r3-20260812/MANIFEST.sha256
shasum -a 256 -c artifacts/wp1-design-remediation-r3-20260812/MANIFEST.sha256.sha256
```

## 证据

- `artifacts/wp1-design-remediation-r3-20260812/design-validation.json`
- `artifacts/wp1-design-remediation-r3-20260812/MANIFEST.sha256`
- `artifacts/wp1-design-remediation-r3-20260812/MANIFEST.sha256.sha256`
- `docs/wp1/service-targets.md`
- `docs/wp1/data-model.md`
- `tools/validate_wp1_design.py`

由于当前交付物尚未纳入 Git，本轮另提供 `MANIFEST.sha256.sha256` 作为完整清单文件的分离式 SHA-256 锚点。该锚点证明当前待验版本，不替代独立验收签署。
