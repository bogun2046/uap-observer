# WP10.3 本轮可复核验收结果

- 记录日期：2026-08-30（Asia/Shanghai）
- 验证分支：`codex/wp10.3`
- 签署起点：`2718ba2dc8f6773e088f5cfb8cfe90d5070aa7ae`
- migration：`0022_wp10_claim_search_projection`
- 父 revision：`0021_wp10_publisher_projection`
- 验证范围：claim/evidence、document_entities、search、受控 rebuild；G10-11–G10-15
- 当前状态：**实现和本地验收已完成；不签署 `G10-GATE-10.3`，不进入 WP10.4**

## 命令结果

| 检查 | 命令/环境 | 结果 |
|---|---|---|
| 静态契约 | `python tools/validate_wp10_3.py .` | passed；单一 0022 head、0021 parent、对象/权限/downgrade/禁止范围全部通过 |
| WP10.3 定向 pytest | `pytest -q tests/test_wp10_3_projection.py` | 7 项全部通过；exit 0 |
| WP8/WP9/WP10 定向回归 | relation reject、WP9 foundation/session/cases/decisions/promotion/merge/claims、WP10.1/10.2/10.3 | 全部运行至 100%；exit 0 |
| 全量 Python tests | `pytest -q` | 257 项全部通过；exit 0 |
| Ruff | `ruff check src tests tools alembic` | `All checks passed!` |
| Python 编译 | `python -m compileall -q src tests tools alembic` | exit 0 |
| 差异卫生 | `git diff --check` | exit 0 |
| 隔离 migration | `python -m tools.wp10_3_migration_probe ...` | PostgreSQL 16.14；10/10 passed |
| WP10.3 真实角色 runtime | `tools/wp10_3_runtime_probe.py` | `G10-11 G10-12 G10-13 G10-14 G10-15 runtime probe passed` |
| WP10.2 全量 runtime 回归 | 在另一全新 0022 数据库运行 `tools/wp10_2_runtime_probe.py` | `G10-06 G10-07 G10-08 G10-09 G10-10 runtime probe passed` |

全范围 `ruff format --check` 会报告签署起点中 46 个既有文件与当前容器 Ruff
formatter 版本不一致；本提交没有机械重排这些无关历史文件。所有本轮新增/修改
Python 文件均单独通过 `ruff format --check`，全范围 lint 通过。

## PostgreSQL 16 证据摘要

`wp10.3-runtime-evidence.json` 保存 58 个检查项：49 个带 actual/expected 的
断言、5 个真实 SQL error 和 4 个 counts/digest before/after 快照。关键结果包括：

- document 尚未公开时 claim apply 返回
  `40001 / publication_dependency_not_ready`，event、attempt 和 projection 前后相同；
- claim/evidence/claim_evidence 同事务可见，concurrent display ordinal 唯一，revise
  保持 public identity 与 ordinal；withdraw 和 document withdraw 确定性清理 search；
- 两个 claim 指向同一 entity 时按 `(document_id, entity_id)` 稳定去重为一条关联；subject
  entity 未公开时不建关联，entity 后发布后确定性 reconcile；public 输出不含
  internal entity/claim UUID；
- search vector 等于 `to_tsvector('simple', display_text)`，facets 仅含
  `category/fact_status/source_name`，evidence excerpt 与 source URL 不进入文本；
- rebuild 同输入重放成功且不 ack pending event；在线/rebuild 快照 counts 为
  `[1 document, 1 entity, 3 claims, 3 evidence, 3 claim_evidence, 1 link, 1 search]`，
  digest 前后均为
  `28bcc6c74b6a96c9bfd3c097e5c261cd2b6765b887b159a26cf05f1f15d20382`；
- 同 rebuild_id 异输入返回 `40001 / publication_rebuild_id_conflict`；manifest mismatch
  记录 `publication_rebuild_mismatch`，projection counts/digest 完整回滚；成功 rebuild 后
  篡改 manifest 再用同一 ID 返回 `22023 / publication_rebuild_mismatch`，不返回旧报告；
  document/entity/claim 三类 active grant 缺 manifest 均记录 failed +
  `publication_rebuild_mismatch`，projection counts/digest 完整回滚；API 与 Publisher rebuild
  均返回 `42501`。

`wp10.3-migration-evidence.json` 保存空状态
`0021 → 0022 → 0021 → 0022` roundtrip、0019 active v1 quarantine 与正常 v2
manifest 并存的 rebuild 成功/同 ID digest 稳定场景，以及 8 类有状态独立阻断。
混合场景含 3 个 active entity grants、1 个 active v2 manifest 和 2 个未解决
legacy quarantines；rebuild/replay 均 succeeded，仅生成 1 个 public entity/identity，
新增 quarantine 前后 input/result digest 完全一致。每类 downgrade 场景只有目标
guard 表 1 行、其他 WP10.3 guard 表均 0 行；downgrade 均返回
`22023 / publication_contract_state_blocks_downgrade`，目标行前后 1、migration version
保持 0022、destructive DDL 未执行。

`wp10.3-wp10.2-regression-evidence.json` 保存 160 个 G10-06–G10-10 检查项：
129 个断言、17 个真实 SQL error 和 14 个 before/after 状态快照。它同时覆盖
G8-16C relation worker terminal、G9 relation review 拒绝、Publisher 权限边界、租约/
attempt/apply+ack/失败/重放幂等关键回归。

## 范围与冻结核验

- `acceptance-cases.md`、`migration-plan.md`、`projection-contract.md`、`permissions.md`、
  `error-codes.md`、ADR-0024 与签署起点无差异。
- 没有 relation grant/manifest/success projection、HTTP/FastAPI/OIDC/JWT、0023、
  WP10.4、Makefile、compose、workflow 或 CI 变更。
- WP10.3 runtime 与 WP10.2 故障注入回归使用两个独立数据库；所有自动迁移探针
  使用随机临时数据库并在结束时清理。
- 主工作树既有修改及未跟踪文件未被本轮 worktree 修改；最终指纹在提交前复核。

## 证据文件

- [wp10.3-migration-evidence.json](wp10.3-migration-evidence.json)
- [wp10.3-runtime-evidence.json](wp10.3-runtime-evidence.json)
- [wp10.3-wp10.2-regression-evidence.json](wp10.3-wp10.2-regression-evidence.json)
- 本文件和上述证据的最终摘要记录在 [SHA256SUMS](SHA256SUMS)。

本记录不构成 `G10-GATE-10.3` 签署。普通追加提交后立即 STOP，不进入 WP10.4，
不 push、不创建 PR、不启动 CI。
