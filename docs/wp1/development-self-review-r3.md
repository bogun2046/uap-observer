# WP1 第三轮补充整改设计自检记录

- 自检编号：`WP1-REMEDIATION-R3-DEV-20260812-01`
- 冻结标准：`G1-FROZEN-20260811-01`
- 对应不通过记录：`G1-REJECT-R3-20260812-01`
- 自检工具：`tools/validate_wp1_design.py`
- 自检结论：第三轮补充整改开发自检通过
- G1 门禁：已提交第四次独立复验，复验通过前 WP2 继续关闭

## 用例结果

| 用例 | 补充整改后实际结果 | 自检 | 证据 |
|---|---|---|---|
| G1-01 至 G1-07 | 第三次复验已确认通过，本轮未改变相关设计路径 | 通过 | `g1-third-rejection-record.md`、前两轮整改证据 |
| G1-08 | 恢复演练由 artifact version 的 `stored_object_id` 关联对象登记，再核对对象 key、内容哈希和字节长度；陈旧字段路径已删除并加入跨文档检查 | 通过 | `service-targets.md`、`data-model.md`、`recovery_object_registry_consistency` |

## 自动检查摘要

- 检查项：29/29 通过。
- 清单文件：28/28 个设计与验证文件哈希通过。
- 恢复数据路径：`ingest.artifact_versions.stored_object_id -> core.stored_objects.id`。
- 清单核对字段：`core.stored_objects.object_key/content_sha256/byte_length`。
- 陈旧路径：`artifact_versions.object_key/content_sha256/byte_length` 已不存在。
- OpenAPI 3.1、三个详情 DTO 正反例及 Ruff 检查继续通过。
- 完整清单：`artifacts/wp1-design-remediation-r3-20260812/MANIFEST.sha256`；清单自身的分离式哈希锚点为同目录 `MANIFEST.sha256.sha256`。

## 待独立复验

- 架构负责人：未签署
- 数据负责人：未签署
- 安全负责人：未签署
- 最终 G1 结论：待第四次独立复验；本自检不宣布 G1 通过
