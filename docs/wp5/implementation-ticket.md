# WP5 实现单：统一采集器插件

- 实现编号：`WP5-IMPL-20260816-01`
- 冻结标准：`G5-FROZEN-20260816-01`
- 前置提交：`add1b9505012a0c7d40831bd31e1dea4a6dc6fef`
- 前置门禁：G4 通过，WP5 已开启
- 负责人：后端负责人 + 数据负责人
- 当前状态：整改自检完成（RSS、持久化、同源配置约束、任务状态闭环、运行策略、版本化 payload 和 G5 证据清单已完成，待 Draft PR、required CI 与独立验收）

## 输入与边界

- 使用 `ingest.sources`、`ingest.source_config_versions`、`ingest.source_runs`、`ingest.artifacts` 和 `core.documents` 作为权威写入模型。
- 使用 WP4 的 `fetch_source` 任务类型，不新增第二套队列状态。
- RSS 试点必须使用固定响应快照；网络访问仅作为可选的真实来源演练路径。
- Collector 不得直接写 `public`，不得把原始正文写入日志或任务错误摘要。

## 实现要求

1. 定义稳定的 Collector 协议及版本化 payload，明确 fetch、parse、normalize、persist 的输入输出和异常边界。
2. 实现 RSS adapter：解析标题、链接、发布时间、条目唯一标识和必要元数据；畸形条目必须可计数并隔离。
3. 实现规范化：URL 去除可配置追踪参数、统一主机/路径表示；无稳定链接时使用来源内条目 ID，不能凭标题去重。
4. 实现 source run 生命周期：开始、完成、结果分类、HTTP 状态、计数器、条件请求头和可审计错误摘要。
5. 实现幂等持久化：同一来源同一 canonical locator 不产生重复 artifact；重复运行必须只增加本次 run 统计，不重复插入业务条目。
6. 实现限速和冷却策略，并将 403、429、超时和 5xx 映射到 WP4 可重试/终态失败语义。
7. 实现来源健康检查，至少输出最近运行结果、连续失败次数、最后成功时间和冷却截止时间。
8. 为固定快照、304、空结果、403、429、超时、5xx、畸形 XML 和重复条目补充契约测试。

## 数据与迁移要求

- 优先复用 G3 已验收表结构；G5 仅追加线性 Alembic revisions：`0006_collectors` 表达采集追溯，`0007_source_run_lease_guard` 提供最小权限的 source-run 租约守卫。
- 若新增字段或表，必须同时更新数据字典、权限矩阵、验证器、测试和证据清单。
- 所有来源配置变更必须产生版本记录，当前配置只能有一个有效版本。

## 开发自测门槛

- Ruff、mypy、全量既有测试和新增 Collector 契约测试通过。
- 固定快照两次执行的持久化结果、计数和哈希一致。
- 对每个 G5 反例确认正确的 `source_run.outcome`、HTTP 状态、任务状态和重试时间。
- 生成 WP5 自检报告、证据清单和 SHA-256 外层锚点。

## 不做事项

- 不把采集逻辑写入 `news` 或其他旧系统主表。
- 不在本工作包实现正文提取、AI 分析、审核或公开发布。

## 首轮实现记录

- 已完成 Collector 四阶段的可测试边界、RSS 解析、URL 规范化、条目键规范化和重复计数。
- 已完成 304、空响应、403、429、408、5xx、其他终态 HTTP 状态分类。
- 已完成条件请求头传递、urllib 传输适配器和注入式持久化回调；连接/HTTP 错误均转换为可记录的 FetchResponse。
- 已完成 `RssSourceRunRunner` 生命周期编排和 `PostgresSourceRunStore` 的 source run、artifact、document 事务写入适配器。
- 已建立 `source_run → artifact_version → document_version` 追溯链；原始 RSS 条目通过对象登记表进入 raw 域。
- 失败路径采用已提交的 source-run checkpoint、业务写入回滚和独立失败收口；canonical URL 冲突先复用已有 document。
- 同一任务重试仅重置运行状态，不允许改写既有 source run 的来源或配置版本；source run 最终更新和 `ops.finish_job` 在同一事务提交。
- source-run checkpoint 必须携带 attempt/token；`ops.require_active_source_job_lease` 在同一事务锁定 job/attempt，并用 `clock_timestamp()` 拒绝过期、已结束或已换主的租约，不扩大 `uap_worker` 的 ops 表写权限。
- canonical URL 非空分支使用对应部分唯一索引的原子 `INSERT ... ON CONFLICT ... RETURNING`，已通过真实双连接首次并发复用验证。
- raw 对象登记在任何 `stat/PUT/读取校验` 前取得同内容地址的事务级 advisory lock，并保持至数据库提交或回滚；已通过真实双事务失败/重试竞态验证。
- 提供 `tools/reconcile_object_storage.py` 定期一致性扫描；清理失败不再只有日志路径，可由扫描重试未登记 raw 对象。
- XML 解析使用 `defusedxml`，并将运行库与类型存根写入 `pyproject.toml`/`uv.lock`。
- 固定快照 `platform/tests/fixtures/rss/g5-fixed-feed.xml` 的 SHA-256 为 `b3a998a48fecd9c18bfb75d294a60465aad12a55490b1c72e6629ebcf9dd73c8`，解析结果会重复计算并校验该哈希。
- WP5 运行态探针自建独立 principal，并以最高优先级入队、逐次核对领取的 job ID；全新数据库直接执行及 `WP3 → WP4 → WP5` 顺序执行均通过。
- 下一步：整理 G5 完整证据清单、运行态报告和 required CI，然后申请独立验收。
