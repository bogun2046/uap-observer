# WP10.1 / WP10.2 / WP10.3 / WP10.4 运行时验收复跑

本文件只提供人工复跑入口，不包含密码。命令必须在 PostgreSQL 16、真实登录角色凭据和已加载完整 Python 依赖的环境中执行。

本轮已复跑的命令输出、SQLSTATE、状态计数和结果哈希见
[validation-results-20260828.md](validation-results-20260828.md)。该记录不替代复跑；缺少真实 role password、PostgreSQL 或完整依赖时仍必须记录为“未复跑”。

WP10.2 当前实现的静态检查、真实隔离数据库探针和证据哈希见
[validation-results-wp10.2-20260828.md](validation-results-wp10.2-20260828.md)。
WP10.3 当前实现的对应结果见
[validation-results-wp10.3-20260830.md](validation-results-wp10.3-20260830.md)。
后者不签署 `G10-GATE-10.3`，仅固定本轮可供独立审核的结果。
WP10.4 当前实现的对应结果见
[validation-results-wp10.4-20260831.md](validation-results-wp10.4-20260831.md)。
该记录不签署 `G10-GATE-10.4`，WP10.5 仍关闭。

## 静态与单元检查

```text
cd platform
python tools/validate_wp10_1.py
pytest -q tests/test_wp10_foundation.py tests/test_wp9_foundation.py
```

## 隔离迁移探针

`tools/wp10_1_migration_probe.py` 会在指定管理员数据库中创建随机命名的临时数据库，建立到 0019 的基线，注入 legacy active grant 与未 ack 的 v1 publication event，执行 0020，并验证：

- legacy grant/event 各有 `publication_manifest_required` quarantine；
- v1 event 变为 terminal，而不是 published；
- 0020 downgrade 以 `publication_contract_state_blocks_downgrade` 拒绝，并保持 version/state 不变；
- 0019 空状态 downgrade → upgrade roundtrip 可完成；
- public 非空 preflight 在 0019 → 0020 时以 `publication_manifest_invalid` 拒绝。

示例（管理员 DSN 只通过环境或受控终端提供，不写入日志）：

```text
cd platform
python tools/wp10_1_migration_probe.py \
  --admin-url 'postgresql://<admin>@<host>:5432/<maintenance-db>'
```

若环境已有隔离的 0019 数据库，可使用 `--database-url` 模式；探针不会自动清理用户指定的数据库。自动创建的随机临时数据库在正常结束或失败时清理。

## WP10.2 隔离迁移探针（G10-06 / G10-10 的数据库基线）

`tools/wp10_2_migration_probe.py` 从空的 0020 数据库执行
`0020 → 0021 → 0020 → 0021` 空状态 roundtrip，并另起含 delivery-attempt
状态的数据库，以及两个分别只含 terminal event、只含 retry event 且 delivery-attempt
均为零的独立数据库，验证 downgrade 在任何 DROP 前以
`22023 / publication_contract_state_blocks_downgrade` 拒绝且 event、attempt、
migration version 状态不变。

```text
cd platform
python tools/wp10_2_migration_probe.py \
  --admin-url 'postgresql://<admin>@<host>:5432/<maintenance-db>' \
  --evidence-out ../docs/wp10/wp10.2-migration-evidence.json
```

## WP10.2 真实 Publisher 探针（G10-06 至 G10-10）

在已完成 0021 upgrade 的隔离数据库中提供真实 `uap_publisher`、`uap_api`、
`uap_worker` 和 `uap_public_reader` 凭据，再执行：

```text
cd platform
UAP_DATABASE_URL='postgresql://<owner>@<host>:5432/<db>' \
  UAP_PUBLISHER_PASSWORD='<secret>' \
  UAP_API_PASSWORD='<secret>' \
  UAP_WORKER_PASSWORD='<secret>' \
  UAP_PUBLIC_READER_PASSWORD='<secret>' \
  python tools/wp10_2_runtime_probe.py \
  --evidence-out ../docs/wp10/wp10.2-runtime-evidence.json
```

探针必须保存 G10-06 专用领取/租约、G10-07 document、G10-08 entity、G10-09
relation claim 排除但真实 Publisher terminal、G10-10 原子 apply+ack/失败注入/重放的真实 SQLSTATE、稳定 code、
before/after 计数与 digest；G10-10 还必须证明 lease 错误优先于 caller summary 校验，且负测不改变 event/attempt/projection。
缺少 Docker、PostgreSQL、真实 role password 或完整
Python 依赖时只能记录为“未复跑”，不能伪报 G10-06–G10-10 通过。

## WP10.3 隔离迁移探针（G10-15）

`tools/wp10_3_migration_probe.py` 从空的 0021 数据库执行
`0021 → 0022 → 0021 → 0022` roundtrip。随后为 claims、evidence、
claim_evidence、document_entities、search_documents、claim identity、evidence
identity 和 rebuild run 分别创建只含该目标状态的独立数据库，证明 downgrade
在 destructive DDL 前稳定返回
`22023 / publication_contract_state_blocks_downgrade`，并保持目标行、0022
version 和 rebuild 函数不变。探针还从 0019 构造 active v1 grant，真实升级为
未解决 quarantine，再与正常 active v2 manifest 混合执行 rebuild；新增合法
quarantine 后以同一 rebuild ID 重放，input/result digest 必须保持不变，且 v1
grant 不得进入 public projection。

```text
cd platform
python -m tools.wp10_3_migration_probe \
  --admin-url 'postgresql://<admin>@<host>:5432/<maintenance-db>' \
  --evidence-out ../docs/wp10/wp10.3-migration-evidence.json
```

## WP10.3 真实 Publisher/Reader 探针（G10-11 至 G10-15）

在全新的 0022 隔离数据库中，以真实密码登录 `uap_publisher` 和
`uap_public_reader`；rebuild 以 `uap_migrator` session authorization 执行。

```text
cd platform
UAP_DATABASE_URL='postgresql://<owner>@<host>:5432/<db>' \
  UAP_PUBLISHER_PASSWORD='<secret>' \
  UAP_API_PASSWORD='<secret>' \
  UAP_WORKER_PASSWORD='<secret>' \
  UAP_PUBLIC_READER_PASSWORD='<secret>' \
  python tools/wp10_3_runtime_probe.py \
  --evidence-out ../docs/wp10/wp10.3-runtime-evidence.json
```

探针覆盖 document 未可见时的依赖重试与零状态变化、claim/evidence 原子发布、
stable identity/display ordinal、revise/withdraw、entity 后发布 reconcile、内部
UUID 隔离、search 重算/撤回，以及 rebuild 的同输入重放、异输入冲突、mismatch
全事务回滚和不 ack 待处理 Outbox。WP10.2 回归必须在另一全新数据库执行，避免
其故障注入终态 manifest 被 rebuild 正确识别后干扰 WP10.3 内容摘要断言。

## 真实角色探针

在已完成 0020 upgrade 的隔离数据库中，向进程提供 `UAP_API_PASSWORD` 与 `UAP_PUBLISHER_PASSWORD`，再执行：

```text
cd platform
UAP_DATABASE_URL='postgresql://<owner>@<host>:5432/<db>' \
  UAP_API_PASSWORD='<secret>' \
  UAP_PUBLISHER_PASSWORD='<secret>' \
  python tools/wp10_1_runtime_probe.py
```

该探针覆盖 `G10-03` 权限边界和 `G10-04` typed manifest/hash/append-only 行为；`G10-05` 由上面的隔离迁移探针独立覆盖。缺少真实 role password、PostgreSQL 或完整依赖时，结果应记录为“未复跑”，不得伪报通过。

## WP10.4 隔离迁移探针（读取索引）

`tools/wp10_4_migration_probe.py` 在随机临时数据库中执行
`0022 → 0023 → 0022 → 0023`，验证 0023 只新增六个读取索引，保留
claim/document_entities/search 的已签署索引，并以 2000 document、2000 entity
夹具保存 documents filter、entity type 和 search GIN 的真实 PostgreSQL 16
`EXPLAIN (FORMAT JSON)` 计划。

```text
cd platform
python tools/wp10_4_migration_probe.py \
  --admin-url 'postgresql://<admin>@<host>:5432/<maintenance-db>' \
  --evidence-out ../docs/wp10/wp10.4-migration-evidence.json
```

## WP10.4 真实 Public API/角色探针（G10-16 至 G10-20）

先把隔离数据库线性升级至 0023，并提供真实 `uap_publisher` 与
`uap_public_reader` DSN。探针启动实际 Public API 子进程；子进程环境只含
`UAP_PUBLIC_DATABASE_URL`，不接收 owner/API/worker/publisher DSN。

```text
cd platform
python tools/wp10_4_runtime_probe.py \
  --admin-url 'postgresql://<admin>@<host>:5432/<db>' \
  --publisher-url 'postgresql://uap_publisher:<secret>@<host>:5432/<db>' \
  --reader-url 'postgresql://uap_public_reader:<secret>@<host>:5432/<db>' \
  --evidence-out ../docs/wp10/wp10.4-runtime-evidence.json
```

探针覆盖六个 GET、404/validation、DTO 字段、relation closed、documents/entities/
search keyset、并发插入不重复、cursor 篡改/跨资源/改 filter/坏版本/超长、
中英文搜索、2–200 code-point 边界、组合 filter、参数化注入、阶段性 p95、真实
reader 权限拒绝，以及 pending → Publisher apply commit → visible → withdraw → 404。
所有 response/problem/header/log 还会做敏感标识扫描。
