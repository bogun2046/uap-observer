# WP10.2 本轮可复核验收结果

- 记录日期：2026-08-30（Asia/Shanghai）
- 验证分支：`codex/wp10.2`
- 验证起点：`8b6ae4fc6e0b46e1cf724742ded6a3db70d7f3a5`
- migration：`0021_wp10_publisher_projection`，父 revision：`0020_wp10_publication_contract`
- 验证范围：仅 document/entity Publisher；G10-06–G10-10
- 当前状态：**G10-06 至 G10-10 已在隔离 PostgreSQL 16 数据库真实通过；不签署 `G10-GATE-10.2`，不进入 WP10.3**

## 可校验结果

| 检查 | 执行环境/命令 | 结果 |
|---|---|---|
| 静态契约 | `python tools/validate_wp10_2.py /repo/platform` | passed；单一 0021 head、0020 parent、权限边界、downgrade guard、禁止范围均通过 |
| WP10.2 静态 pytest | `pytest -q tests/test_wp10_2_publisher.py tests/test_wp10_foundation.py` | 8 项运行至 100%；exit 0 |
| 全量 Python tests | `pytest -q` | 250 项收集并全部通过；exit 0（输出完成至 100%） |
| Ruff | `ruff check src/uap_platform/publishing tools/validate_wp9.py tools/validate_wp10_1.py tools/validate_wp10_2.py tools/wp10_2_migration_probe.py tools/wp10_2_runtime_probe.py tests/test_wp10_2_publisher.py tests/test_wp10_foundation.py` | `All checks passed!` |
| Python 编译 | `python -m compileall -q src tools tests alembic/versions/0021_wp10_publisher_projection.py` | exit 0 |
| 差异卫生 | `git diff --check` | exit 0 |
| 隔离 migration | `tools/wp10_2_migration_probe.py` | passed；空状态 `0020 → 0021 → 0020 → 0021`；6 条 0020 outbox 状态和 pending document/entity v2 event 可 downgrade；含 3 条 attempt history 的有状态场景，以及 attempt 行为 0 的 terminal-only、retry-only 两个独立场景，均以 `22023 / publication_contract_state_blocks_downgrade` 拒绝并保持状态不变 |
| 真实 Publisher | `tools/wp10_2_runtime_probe.py` | `G10-06 G10-07 G10-08 G10-09 G10-10 runtime probe passed` |

两份 JSON 是脱敏后的提交证据：runtime 记录包含 160 个检查项，其中 129 个断言全部为 true、17 个预期 SQLSTATE/错误码对和 14 个 before/after 状态记录；SQLSTATE 摘要包括 `40001/publication_lease_lost`（4）、`22023/publication_failure_parameters_invalid`（6）、`40001/publication_delivery_attempt_conflict`（1）、`22023/publication_payload_hash_mismatch`（1）、`22023/publication_event_aggregate_mismatch`（1）及权限拒绝（2）。migration 记录包含 0020 outbox 保留、空状态 roundtrip、含 attempt history 的 fail-closed downgrade，以及无 delivery-attempt 的 terminal-only、retry-only 两项独立 fail-closed downgrade，共 5 项；后两项均保存 `22023`、稳定错误码、attempt 前后 0 行、event 状态不变与 0021 version 保留证据。

## 真实探针摘要

- G10-06：publisher-only claim、并发单租约、错误 token、generic ack/裸 public DML 拒绝、参数校验和排除事件均通过。
- G10-07：document stable public ID/slug、grant/revise/withdraw、stale event 不降级、identity 保留均通过。
- G10-08：entity stable public ID/slug、同名实体隔离、active-canonical 校验（retired/merged/disputed 均不投影）、状态锁竞争、revise/withdraw、identity 保留均通过。
- G10-09：正常 analysis 不入队 `resolve_relations`；worker 误领得到 terminal `knowledge_relation_task_not_in_wp8`；relation review 拒绝；真实 Publisher claim 返回集合排除 relation，同时将 relation event terminal 为 `publication_event_schema_unsupported`、保持 `published_at=NULL`；relation grant/core/public relation 表零写入。
- G10-10：五个 failure-injection 点、payload/aggregate 篡改保护、非权威/NULL/超长摘要拒绝、错误/过期/伪造 token 优先返回 `publication_lease_lost`、retry/新租约、旧 token 拒绝、同 token replay 无写入、operation conflict 均通过；各负测保存 event/attempt/projection before/after 状态。

探针数据库为一次性隔离数据库；探针完成后不作为产品数据保留。命令未启动 CI、未写 Makefile/workflow、未 push、未创建 PR。

## 证据文件

- [wp10.2-migration-evidence.json](wp10.2-migration-evidence.json)
- [wp10.2-runtime-evidence.json](wp10.2-runtime-evidence.json)
- 本文件及上述 JSON 的最终 SHA256 记录在 [SHA256SUMS](SHA256SUMS)。

本记录只固定 WP10.2 实现和 G10-06 至 G10-10 结果，不构成 `G10-GATE-10.2` 签署。完成普通追加提交后立即 STOP，等待独立审核。
