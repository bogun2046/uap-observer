# V1-2 Contract Freeze — Acceptance

本文件冻结实施后的单一真实验收链；不以测试数量代替产品完成。

## 正例主链

使用已 `analysis_ready` 的真实 Reddit 文档（优先复用 V1-1 验收文档，不重新抓取）：

1. OIDC 单管理员打开 Internal Library detail，能看到 RAW/extraction、原文、classification、summary/bullets、claims/evidence、entities/evidence、model run/prompt/token/cost、当前 EDITORIAL。
2. 修改中文摘要、一个 bullet、一个 claim 允许字段和一个 entity 允许字段；保存成功，刷新后持久存在。
3. 核对原始 RAW、所有旧 AI_RESULT、prompt/hash/token/cost 未改变；audit 有 principal、request_id、before/after 和字段来源。
4. 仅触发 summary reanalysis；新 model run/AI_RESULT 成功或可见失败，预算仍受 ¥20/月控制；旧 AI_RESULT 保留，EDITORIAL 不被覆盖。
5. 明确执行“采用新的 AI summary”；产生新 EDITORIAL revision，并记录 adopted analysis_result_id；不修改 model result。
6. trash 后正常 Internal Library 隐藏，Recycle Bin 可见，RAW/AI_RESULT/audit 保留。
7. restore 后文档和最后 EDITORIAL revision 恢复，未自动重新分析、未自动公开。
8. 全程 `public_authorized=false`、publication grants/manifests/public documents/search rows 均为 0。

## 必验负例

- stale `base_revision` 保存返回 409 且无部分 revision；相同 Idempotency-Key 重放不产生第二 revision。
- Worker/model/scheduler 不能调用 editorial 写函数。
- extra/unknown editorial fields、修改 RAW URL/hash/evidence locator、越权角色、trash 中 reanalysis、永久删除均拒绝。
- reanalysis 失败只产生可见失败/审计，不覆盖 EDITORIAL、不虚构 token/cost。

## 证据分类

- 真实模型测试：summary 单任务真实 run、usage/cost、旧结果与新结果。
- 模拟/单元测试：schema DTO、revision conflict、idempotency、permission negatives、trash/restore transitions。
- 人工抽查：中文摘要/bullets、claim/evidence、entity/evidence、RAW 对照和页面隐藏性。
