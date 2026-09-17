# WP10 独立验收任务书

- 验收编号：`WP10-ACCEPT-20260828-01`
- 建议冻结号：`G10-FROZEN-20260828-01`
- 前置：`G10-GATE-10.1` 已签署，签署 SHA 为
  `8b6ae4fc6e0b46e1cf724742ded6a3db70d7f3a5`；当前验收范围仅为 WP10.2

## 1. 责任与环境

实施者不得自签门禁。独立审核者必须在干净 clone/worktree、PostgreSQL 16、真实登录角色连接上复现。OIDC 可使用本地签名测试 issuer/JWKS，不调用生产身份服务；模型输入用固定脱敏 fixture，不调用收费 provider。

任何用例失败、证据不完整、SHA 不固定或工作区不干净，当前门禁不通过，后一阶段保持关闭。

## 2. 每阶段固定证据

- 启动口令、冻结号、起点/父/最终 SHA、分支；
- `git status --short`、`git diff --name-status <start>..<head>`、第一父链；
- Alembic current/head、upgrade/downgrade/upgrade 与阻断场景 SQLSTATE/稳定 code；
- 角色 table/function/schema privilege 查询；
- 每条用例的命令、预期、实际、HTTP status、problem code、request_id 和相关数据库 ID；
- request payload、数据库 `metadata.payload_sha256`、outbox payload hash（不得含机密/全文）；
- Outbox lease/retry/terminal/replay 时间线、public before/after digest；
- 事务失败前后行数和 digest，证明完整 rollback；
- 性能样本、EXPLAIN、p95/p99、日志净化检查；
- CI required jobs、文件清单、内外层 SHA-256。

## 3. 门禁

| 门禁 | 必须通过 | 明确不要求 | 通过后才可 |
|---|---|---|---|
| `G10-GATE-10.1` | G10-01–05 | projector、HTTP、relation | 项目负责人可授权 WP10.2 |
| `G10-GATE-10.2` | G10-06–10 | claim/search/HTTP | 可授权 WP10.3 |
| `G10-GATE-10.3` | G10-11–15 | HTTP | 可授权 WP10.4 |
| `G10-GATE-10.4` | G10-16–20 | admin HTTP | 可授权 WP10.5 |
| `G10-GATE-10.5` | G10-21–24、W01–W11 全实例 | CI 总接线、WP11 | 可授权 WP10.6 |
| `G10-GATE-10.6` | G10-25–29、G10-01–24 与 W 全量复跑 | relation、WP11 | 可签署 G10 |

## 4. 关键判定

### 发布可见性

grant/decision/outbox 成功不等于 public 可见。只有 `ops.apply_publication_event` 在有效 lease 下同事务完成 projection 与 ack 后才可见。失败或崩溃不得出现“ack 已提交、projection 未提交”。

### 幂等与冲突

W01–W11 的每个 HTTP 写实例都必须执行六项：首次成功、权限拒绝、缺 request_id、同 payload 重放、同 request_id 异 payload 冲突、注入失败全事务回滚。不得以一个代表操作替代其它操作。

### 旧数据与 downgrade

v1 grant/outbox 只能 quarantine/terminal；不得补猜 manifest。空状态可 downgrade/upgrade；任何会丢失 v2 manifest/identity/projection/terminal 历史的 downgrade 必须稳定拒绝。

### 安全

真实 `uap_api`、`uap_publisher`、`uap_worker`、`uap_scheduler`、`uap_public_reader`、`uap_model_governance` 逐项验证。owner 结果不能替代登录角色拒绝项。公开 DTO/日志/problem 不含冻结禁止字段。

### relation

每一门禁都必须证明没有 relation enqueue/materialize/review/grant/project/API 成功路径；G8-16C 误领仍 terminal failure `knowledge_relation_task_not_in_wp8`。

## 5. G10 整体签署条件

1. 六个阶段分别有独立签署 SHA 且第一父线性；
2. G10-01–29 和 W01–W11 全部通过；
3. WP1–WP9 required 回归和 G8-16C/G9 权限幂等回归通过；
4. public reader、API、Publisher 三凭据边界与 projection 重建成立；
5. 公开文档/详情满足 p95≤300ms、搜索 p95≤500ms 的冻结容量测量方法；
6. 发布新鲜度 p95≤2 分钟有可重复测量证据；
7. 无 P0/P1 泄漏、裸 DML、未审内容公开或 ack/projection 双写缺口；
8. required CI 全绿、HEAD 固定、工作区干净、审核包哈希一致；
9. relation 和 WP11 明确保持关闭。

签署后实施者仍必须 STOP，等待项目负责人后续口令。
