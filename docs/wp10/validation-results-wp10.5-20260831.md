# WP10.5 验收结果记录（2026-08-31）

状态：第二次独立审核 REJECT（W04 replay 断言依赖锁顺序）后于 2026-09-01 Asia/Shanghai 完成探针整改并连续三次真实运行全量 runtime probe；供父代理复核后做一次普通线性追加提交；**不签署 `G10-GATE-10.5`，不进入 WP10.6**。未 commit、未 push、未停止 `uap-wp10-impl-postgres-1`、未修改主工作树。

## 基线与范围

- 签署起点：`19ee4f436179740c002f05f597e456d42b1a17db`
- 仓库 HEAD（整改前已提交）：`1ea9ae046b2f636c17574dff805044b9f2382948`（父 `b5ad2715b96556cd954defc3c0be03def7ce3ceb`）
- 分支：`codex/wp10.5`（无 upstream）
- 第一父链：`10773f1` → `ec06ab8` → `19932ae` → `15b85ed` → `19ee4f4` → `b5ad271` → `1ea9ae0`
- 本轮未提交；工作树含 W04 replay 顺序无关整改
- migration：`0024_wp10_admin_replay`，父 revision `0023_wp10_api_read_indexes`
- 0001–0023 migration 与冻结契约文档内容未修改
- 独立 OIDC Admin API 进程：`python -m uap_platform.admin_api.server`，仅 `UAP_ADMIN_*`，DSN 角色 `uap_api`
- 本轮为 Codex 独立审核 REJECT 整改，不是门禁签署

## REJECT 项整改

- 第二次 P1（本轮唯一）：`wp10_5_runtime_probe.py` W04 并发后只 replay thread 0，并要求 `publication.grant_id` / `publication.revision` 等于该线程历史值。`publication` 是当前 active grant 快照；thread 1 后提交时 replay thread 0 仍应 HTTP 200 且 grant/manifest/outbox 零增量，快照可以是 revision 3。已改为并发 Barrier 仍保留；之后分别 replay 两个 idempotency key；每路断言 200、`resource_id` 与该线程原始响应相同、replay 前后 grant/manifest/outbox 快照相等；不要求历史 grant_id/revision；可选断言 replay 的 publication.grant_id 等于当前 active（max revision）。证据记录 `lock_order` / `order_2_then_3` / `order_3_then_2` / `winner_thread`。未改 Admin 生产行为，未改 0001–0023，未改冻结契约。
- 上一轮已证明且本轮未回归：P1-1 `UAP_ADMIN_CURSOR_SECRET`；P1-2 66 矩阵 before/after；P1-3 真并发 W04；P1-4 运行时 RSA；P2 ruff / diff-check。本轮 runtime checks=90，matrix instances=66
- P1-2：每个 G10-Wxx-{S,P,M,R,C,T} 保存并断言 before/after 行数与 digest；S 断言恰好一个 request-scoped audit event 与业务结果；P/M/C/R 全表快照不变；T 对契约指定副作用表全部注入 trigger 后回滚再同请求成功。validator 要求每条 `passed=true` 及 before/after（T 另需 inject_tables / retry_before / retry_after）
- P1-3：W04 两个不同 canonical request UUID，threads + Barrier 并发在途 HTTP revise；双 200，revision 2 与 3 严格递增，最终 active grant=1，同请求 replay 不新增 grant/manifest/outbox。in-flight overlap 实测 8.483ms
- P1-4：RSA N/E/D 运行时生成，仓库与待提交文件不再含固定私钥指数
- P2-1：去掉 implementation-start 与本文件尾随空白。工作树 `git diff --check 19ee4f4` exit 0
- P2-2：临时 helper 已移出工作树到 `/tmp/uap-wp10.5-sidecar/`，`ruff check .` exit 0

## 命令与结果（本轮 sidecar 实跑，Python 3.12 / `uap-platform:development` / 网络 `uap-wp10-impl_backend`）

```text
pytest
295 passed (exit 0)

pytest -q tests/test_wp10_5_admin_api.py tests/test_wp10_5_oidc.py
24 passed (exit 0)

ruff check .
All checks passed! (exit 0)

ruff format --check <WP10.5 python paths>
15 files already formatted (exit 0)

python -m compileall -q src tools tests alembic/versions
exit 0

git diff --check 19ee4f4
exit 0
# 含工作树。19ee4f4..HEAD 在父代理提交前仍为已提交 b5ad271 的旧尾随空白；工作树已去掉。

python -m tools.validate_wp10_5
WP10.5 static contract validation passed; run migration/runtime probes.

python -m tools.validate_wp10_1
WP10.1 static contract validation passed

python -m tools.validate_wp10_2
WP10.2 static contract validation passed

python -m tools.validate_wp10_3
WP10.3 static contract validation passed

python -m tools.validate_wp10_4
WP10.4 static contract validation passed

python -m tools.validate_wp9
passed (exit 0)

python /sidecar/_wp10_5_run.py
# URL 仅经环境变量 UAP_WP10_5_ADMIN_URL，不出现在 argv / evidence
WP10.5 migration probe passed
G10-21 G10-22 G10-23 G10-24 W01-W11 runtime probe passed
```

## 真实数据库与 HTTP 证据摘要

- PostgreSQL：16.14（`uap-postgres:16.14-hardened`，容器 `uap-wp10-impl-postgres-1`，网络 `uap-wp10-impl_backend`，无 host port）
- Admin 子进程环境：`UAP_ADMIN_CURSOR_SECRET`（32+ bytes），非 KEY
- migration roundtrip：`0023 → 0024 → 0023 → 0024`；`audit.requeue_publication_event` 对 `uap_api` EXECUTE，禁止角色无 EXECUTE
- G10-21：12 条全过。inactive principal 实测 **(403, `api_principal_not_provisioned`)**，不是 503。未知/service 同为 403；缺 token/cookie 401 `api_auth_required`；畸形/alg/issuer/aud/exp/nbf/坏签名 401 `api_token_invalid`
- G10-22：15 条全过（含 W04 grant/projection/revision 读路径）
- G10-23：7 条全过
- G10-24：12 条全过；私有函数与 public DML 对真实 `uap_api` 均为 SQLSTATE `42501`；W08/W09 audit wrapper HTTP 200，actor 为 GUC principal
- runtime evidence：90 条断言无失败（含双线程 replay 与零增量）
- W 矩阵：66/66 `passed=true`，每条含 before/after。T 注入表数 W01–W03/W05=2，W04=10，W06–W11=3
- W04 extras：document/claim/entity approve/revise/withdraw 在同一全量 probe 中执行并过；G10-24 同跑。并发不同 request revise 双 200、revision 严格递增、恰好 1 个 active grant。随后分别 replay 两个请求：皆 200，resource_id 与该线程原始响应相同，grant/manifest/outbox 零增量；publication 跟随当前 active（winner revision=3）。三次连续全量 probe：run1 lock_order `3-then-2`（thread 0 赢），run2 `3-then-2`，run3 `2-then-3`（thread 1 赢，正是上一轮独立复跑失败序）；三次 exit 0、residue 0。写入 docs 的 evidence 来自随后一次 orchestrate 全量实跑（该次 lock_order `3-then-2`）
- 敏感扫描：待提交文件无 DSN / Bearer JWT / PEM 私钥 / 固定私钥指数

## 主工作树指纹（只读比较，未修改）

路径：`/Users/shonchun/Documents/UAP平台`
分支：`codex/entity-normalization`
HEAD：`4f09b0aad0b13c94b6435e4205fa02c046de46e5`

| 项 | 值 | 与启动记录 |
| --- | --- | --- |
| tracked index (`git ls-files -s`) | `0279ee036f8bf5893d446d542d602f22f1b6c0e320478e0b84814e5df68d5ec1` | 相同 |
| tracked diff (`git diff HEAD`) | `e651e4f4816ddc9cf725db41fe2c5af88d36a2229bfddbffa7647c89215b9b4c` | 相同 |
| untracked paths | `9f20514f26da889bab489adab9474da3c285e08861ca7ff3fb5346f691da0b67` | 相同 |

预存在脏集未触碰。

## 残留

- 探针隔离库已 drop；`uap_wp10_5_%` = 0；相关 session = 0
- 未 drop `uap_wp10_gate` / `uap_wp10_public_gate`
- 短暂 sidecar `uap-wp10-5-sidecar-probe` / `uap-wp10-5-sidecar-full`（`uap-platform:development`）已删除
- postgres 容器保持 running/healthy
- 允许的临时 helper 在工作树外：`/tmp/uap-wp10.5-sidecar/`（未纳入 git）

## Stop line

实施未提交。父代理复核后做一次普通线性追加提交。不要把 `/tmp/uap-wp10.5-sidecar/` 下 helper 纳入提交。STOP。不签署 `G10-GATE-10.5`。不进入 WP10.6。
