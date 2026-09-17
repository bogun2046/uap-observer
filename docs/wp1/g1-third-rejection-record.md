# G1 第三次独立复验不通过记录

- 验收编号：`G1-REJECT-R3-20260812-01`
- 前序整改：`G1-REMEDIATION-R2-20260812-01`
- 结论：第三次独立复验不通过，存在 1 项阻断缺陷
- 门禁：G1 继续待独立复验，WP2 继续关闭
- 复验日期：2026-08-12（Asia/Shanghai）

## 用例结论

| 用例 | 第三次复验结论 |
|---|---|
| G1-01 数据分层 | 通过 |
| G1-02 任务状态解耦 | 通过 |
| G1-03 主键、唯一约束与所有者 | 通过 |
| G1-04 AI 多版本追加 | 通过 |
| G1-05 公开角色最小权限 | 通过 |
| G1-06 PostgreSQL 唯一权威 | 通过 |
| G1-07 单一迁移源 | 通过 |
| G1-08 交付物一致性 | 不通过 |

## 阻断缺陷

- `G1-R3-D01`：`service-targets.md` 的对象恢复演练仍核对已经从 `ingest.artifact_versions` 删除的 `object_key/content_sha256/byte_length` 字段，未按 `stored_object_id` 关联 `core.stored_objects`，与冻结数据模型的数据路径不一致。

## 补充整改要求

恢复演练必须通过 `ingest.artifact_versions.stored_object_id -> core.stored_objects.id` 关联，再将对象登记中的 `object_key/content_sha256/byte_length` 与对象存储清单核对；增加该路径的跨文档语义检查，生成新的验证报告和完整 SHA-256 清单后再申请复验。本记录永久保留第三次“不通过”结论。

## 后续状态

2026-08-12 已完成补充整改并提交第四次独立复验。本记录继续永久保留第三次“不通过”结论；第四次复验通过前 G1 不得登记为通过，WP2 继续关闭。

2026-08-12 第四次独立复验确认通过，阻断缺陷 `G1-R3-D01` 已关闭。该后续结论不改写本记录所载第三次复验“不通过”的历史事实；最终通过结论见 `g1-acceptance-record.md`。
