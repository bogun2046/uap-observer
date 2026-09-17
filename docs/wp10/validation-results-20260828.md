# WP10.1 本轮可复核验收结果

- 记录日期：2026-08-28（Asia/Shanghai）
- 验证分支：`codex/wp10.1`
- 验证起点：上一轮整改提交 `8e201d6` 加本轮 WP10.1 整改工作树
- 验证范围：仅 WP10.1；不签署 `G10-GATE-10.1`，不进入 WP10.2
- 本文件的 SHA-256：见同目录 [SHA256SUMS](SHA256SUMS) 的
  `validation-results-20260828.md` 条目

## 命令输出摘要

| 检查 | 命令/入口 | 已保存的结果 |
|---|---|---|
| 全量回归（排除既有 policy 环境测试） | `pytest -q --ignore=tests/test_platform_policy.py` | `246 passed` |
| WP10.1/WP9 定向回归 | `pytest -q tests/test_wp10_foundation.py tests/test_wp9_foundation.py` | `13 passed` |
| 静态契约 | `python tools/validate_wp10_1.py` | `WP10.1 static contract validation passed` |
| 代码质量 | `ruff check ...` | `All checks passed!` |
| Python 编译 | `python -m py_compile ...` | exit 0，无输出 |
| 格式卫生 | `git diff --check` | exit 0，无输出 |
| 真实角色 runtime probe | `python tools/wp10_1_runtime_probe.py` | `WP10.1 runtime validation passed: G10-03 G10-04` |
| 隔离 migration probe | `python tools/wp10_1_migration_probe.py --admin-url <admin-dsn>` | `G10-05 migration probe passed` |

隔离 migration probe 还输出并断言以下摘要：

```text
G10-05 SQLSTATE summary: legacy_downgrade=22023/publication_contract_state_blocks_downgrade; document_structured_changes=22023/review_structured_changes_unsupported; missing_document_v2_grant=23514/publication_document_grant_required; public_preflight=22023/publication_manifest_invalid
G10-05 fail-closed counts: document and claim before=after=(0, 0, 0, 0)
```

自动创建的随机数据库已在探针结束时清理；用户指定的 `--database-url` 不会被删除。

## 负测与状态不变性

| 场景 | SQLSTATE / primary message | 不变性断言 |
|---|---|---|
| 0020 非空 legacy 状态 downgrade | `22023 / publication_contract_state_blocks_downgrade` | migration version 与 legacy 状态不变 |
| 空状态 `0020 → 0019 → 0020` | 全程成功 | 最终回到 `0020_wp10_publication_contract` |
| downgrade 后 document approve 携带 `publication` | `22023 / review_structured_changes_unsupported` | decision/grant/manifest/outbox 计数 `before=after=(0,0,0,0)` |
| 0020 claim approve 且无 document v2 grant | `23514 / publication_document_grant_required` | decision/grant/manifest/outbox 计数 `before=after=(0,0,0,0)` |
| 0019 → 0020 public preflight 非空 | `22023 / publication_manifest_invalid` | version 保持 0019，public marker 保留 |

上述结果仅证明本轮 WP10.1 整改的本地复跑状态，不构成 `G10-GATE-10.1` 签署，也不授权后续阶段。

## 被验证文件哈希

以下 SHA-256 固定本轮被验证的关键代码与测试文件；文档文件及本结果记录的哈希由同目录 `SHA256SUMS` 管理。

```text
149840e438ad6ee73e77f945f5799c016d22a3d12d2e6d45756d7e532bee3fc5  platform/alembic/versions/0020_wp10_publication_contract.py
272a392d374117b5e17e24ebaed4ae98cc5d031be18268fbcab559315fdeb472  platform/tools/validate_wp10_1.py
adbcc49cd90290986206bd7b7df43f95e95740fa24bc5a9fcb0823c37bbd8e35  platform/tools/wp10_1_migration_probe.py
232877d958250b02f2772a7d8ffe92ba3c0a4134ed69fe72b2f41312b0e5c546  platform/tools/wp10_1_runtime_probe.py
e14147308cba5ea29e08d12d15a8069d0ed2c1ec40e18222f40c9a40e62ff8c8  platform/tests/test_wp10_foundation.py
```
