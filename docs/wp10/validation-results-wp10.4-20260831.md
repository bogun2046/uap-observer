# WP10.4 验收结果记录（2026-08-31）

状态：独立实施复核于 2026-08-31 15:00 Asia/Shanghai 复跑通过，供独立审核；**不签署
`G10-GATE-10.4`，不进入 WP10.5**。

## 基线与范围

- 签署起点：`10773f135ef5fca236db2eebee75fd6b15f3cd0f`
- 分支：`codex/wp10.4`
- migration：`0023_wp10_api_read_indexes`，父 revision
  `0022_wp10_claim_search_projection`
- 0001–0022 migration 与冻结契约文档内容未修改。
- 未增加 Admin/OIDC/JWT、写 route、relation 成功路径、外部搜索、WP10.5+、
  Makefile、Docker/compose、workflow 或 CI 接线。

## 命令与结果

```text
pytest
271 passed in 1.86s

pytest -q tests/test_wp10_4_public_api.py
14 passed

python -m tools.validate_wp10_4
WP10.4 static contract validation passed; run migration/runtime probes.

python -m tools.wp10_4_migration_probe --admin-url <redacted> \
  --evidence-out ../docs/wp10/wp10.4-migration-evidence.json
WP10.4 migration index roundtrip and EXPLAIN probe passed

python -m tools.wp10_4_runtime_probe --admin-url <redacted> \
  --publisher-url <redacted> --reader-url <redacted> \
  --evidence-out ../docs/wp10/wp10.4-runtime-evidence.json
G10-16 G10-17 G10-18 G10-19 G10-20 runtime probe passed
```

```text
ruff check .
All checks passed!

ruff format --check <WP10.4 changed Python paths>
13 files already formatted

python -m compileall -q src tools tests alembic/versions
exit 0

git diff --check
exit 0

shasum -a 256 -c SHA256SUMS
33 files: OK
```

## 真实数据库与 HTTP 证据摘要

- PostgreSQL：16.14，一次性隔离数据库，迁移后版本
  `0023_wp10_api_read_indexes`。
- migration roundtrip：`0022 → 0023 → 0022 → 0023`；新建六个索引，旧
  claim/document_entities/search 索引保留。
- EXPLAIN：documents 使用 `ix_public_documents_category_fact_published_id`；
  entities 使用 `ix_public_entities_type_published_id`；search 使用已签署
  `ix_search_documents_vector`。
- runtime：125 条证据项，无失败断言；六组 reader 权限负测均返回真实
  SQLSTATE `42501`。
- Public API 子进程环境无 privileged DSN；真实 session user 为
  `uap_public_reader`。
- runtime 容量夹具：2200 documents、2200 entities、2200 search rows；documents、
  entities、search 分页均在 cursor 两次请求之间执行 insert/revise/withdraw，
  且无重复或新行进入已通过区间。
- search 分页负测实际保存 before/after page；独立 withdraw 影响行数为 `1`，
  新增项 rank `2.3` 高于第一页边界 `0.22500001`，且未进入 after page。
- search 容量探针：40 samples，p95 `13.506 ms`，阶段阈值 `500 ms`；migration
  EXPLAIN 另以 2000 documents/entities 夹具证明过滤/排序索引命中。
- relation collection/detail 均为 404 `api_capability_closed`；`/healthz` 返回
  200 JSON 并包含 `X-Request-ID`。
- pending/internal ID 为统一 404；Publisher apply commit 后 stable public ID
  一次性可见；withdraw commit 后同 public ID 立即恢复统一 404，且未发送缓存头。
- problem/response/header/process log 敏感扫描通过：无 SQLSTATE、私有 schema、
  内部身份或私有 evidence 内容。

机器可复核明细：

- [wp10.4-migration-evidence.json](wp10.4-migration-evidence.json)
- [wp10.4-runtime-evidence.json](wp10.4-runtime-evidence.json)

## 停止线

本记录只固定开发端验收证据。最终普通追加提交后立即 STOP；不签署
`G10-GATE-10.4`，不进入 WP10.5，不 push、不创建 PR、不启动 CI。
