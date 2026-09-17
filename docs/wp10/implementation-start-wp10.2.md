# WP10.2 实施启动证据

- 记录日期：2026-08-28（Asia/Shanghai）
- 前置门禁：`G10-GATE-10.1` 已由项目负责人正式签署
- 签署起点 / 父 SHA：`8b6ae4fc6e0b46e1cf724742ded6a3db70d7f3a5`
- 实施分支：`codex/wp10.2`
- 实施工作树：独立 worktree；主工作树原有改动保持不变

## 授权记录

项目负责人明确授权：从上述签署 SHA 线性启动 WP10.2，并要求完成 G10-06 至 G10-10 后创建普通追加提交、固定最终 SHA 和验收证据，然后立即 STOP。该授权不签署 `G10-GATE-10.2`，也不授权 WP10.3。

## 允许范围

1. 新增线性 migration `0021_wp10_publisher_projection`；
2. 新增 `ops.claim_publication_outbox`、`ops.apply_publication_event`、`ops.fail_publication_event`；
3. 新增 immutable delivery-attempt 历史及 document/entity stable public identity、grant/revise/withdraw、stale event、原子 apply+ack；
4. 新增 Python publishing service、专用 Publisher loop、SQL/权限/失败注入/runtime probe；
5. 仅完成 G10-06–G10-10 的验收材料。

## 明确禁止

- claim/evidence/document_entities/search 投影；
- rebuild、HTTP/API/OIDC、relation 成功路径和 WP10.3 以后对象；
- CI、Makefile、workflow 总接线；
- push、PR 或从其他分支/未签署提交启动。

## 停止线

G10-06–G10-10 全部真实通过后，创建普通追加提交，记录最终 SHA、父链、工作区和可校验证据；立即 STOP。不得签署 `G10-GATE-10.2`、不进入 WP10.3、不 push、不创建 PR、不启动 CI，等待独立审核。
