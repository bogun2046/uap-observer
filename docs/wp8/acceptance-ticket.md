# WP8 R2 独立验收单

- 验收编号：`WP8-ACCEPT-20260821-02`
- 冻结标准：`G8-FROZEN-20260821-02`
- 父基线：`a34acd3282001421de1376e6e62ca3d7cf0f4233`
- 实施起点：本设计 docs-only 提交 `G8_R2_DESIGN_SHA`
- 状态：设计已冻结；实施与验收尚未开始
- 技术审核：Codex
- 阶段放行：项目负责人
- 后置门禁：WP9 关闭，直至 G8 最终签署

## 1. 独立性要求

- Codex 在隔离 clone 中审核固定 SHA，不在审核工作区修改实现。
- Grok 的自测不替代 Codex 审核；整改必须形成新提交和新审核包。
- 每次审核先确认起点、HEAD、远端分支、PR HEAD 和 CI run 指向同一提交。
- 使用全新 PostgreSQL 16 与 S3 兼容对象存储，按 `WP3 -> ... -> WP8` 顺序执行探针。
- 权限测试使用真实 `uap_worker`、`uap_api`、`uap_scheduler`、`uap_publisher`、`uap_model_governance`、`uap_public_reader`；不得以 owner 结果替代拒绝项。
- owner/migrator 只允许用于迁移、构造明确标注的历史异常夹具，以及验证 WP8.5 默认关闭函数。
- 模型输入使用固定脱敏 Provider 响应，不调用收费服务。

## 2. 固定证据

每阶段必须保存：

- 阶段启动口令、冻结编号、起点 SHA、最终 SHA、父提交 SHA；
- `git status --short`、分支与 PR HEAD；
- migration head、upgrade/downgrade/upgrade 输出；
- 每条用例的命令、预期、实际、SQLSTATE、稳定 error code 与相关 ID；
- job、attempt、analysis_result、extraction、span、claim、candidate、merge event 的 ID 集；
- attempt metrics 原始 JSON（不得含正文）；
- 权限查询、并发时间线、锁等待与最终图；
- CI run、四个 required job 结论；
- 文件清单及内外层 SHA-256。

## 3. WP8.x 独立验收门禁

| 门禁 ID | 阶段 | 必须通过 | 明确不要求 | 通过后 |
|---|---|---|---|---|
| `G8-GATE-8.1` | WP8.1 | G8-01–G8-06 | locator 生产映射、materialize、handler、merge、CI 接线 | 项目负责人可授权 WP8.2 |
| `G8-GATE-8.2` | WP8.2 | G8-07–G8-10 | 知识表写入、任务成功路径 | 可授权 WP8.3 |
| `G8-GATE-8.3` | WP8.3 | G8-11–G8-13、G8-16A | entity handler、merge | 可授权 WP8.4 |
| `G8-GATE-8.4` | WP8.4 | G8-14、G8-15、G8-16B | merge | 可授权 WP8.5 |
| `G8-GATE-8.5` | WP8.5 | G8-17–G8-19 | 运行时 senior_reviewer 调用能力 | 可授权 WP8.6 |
| `G8-GATE-8.6` | WP8.6 | G8-16C、G8-20、G8-01–G8-19 最终复跑 | 无 | 可签署 G8 |

阶段通过只表示“技术上具备进入下一阶段条件”，不会自动授权实施者继续。

## 4. 重点判定标准

### 4.1 空、部分和全拒绝

- valid 空 `claims=[]` / `entities=[]`：job `succeeded`，物化 0，metrics 标记 `empty_valid_result=true`。
- 非空且至少一个候选成功：job `succeeded`，只提交成功候选，metrics 记录拒绝计数。
- 非空且所有候选被拒绝：job `dead`（terminal failure），物化 0，metrics 保留原因统计。

### 4.2 事务闭环

- 领域写入与 `finish_knowledge_job` 同事务可见或同事务回滚。
- 预期 materialize RAISE 后必须先 `ROLLBACK TO SAVEPOINT`，再收口 attempt。
- lease 失效 `40001` 后不得伪写 metrics 或另行标记 succeeded。
- Worker 对 `ops.job_attempts` 直接 UPDATE 必须仍被拒绝。

### 4.3 provenance

- payload 必须逐字段绑定 analysis_result、model_run、`ai.v1`、result hash 与 extraction anchor。
- bundle 必须覆盖 result JSON 的全部 candidate 和 locator ordinal；不能插入、替换或无理由遗漏。
- 同 idempotency key 不同 payload 必须失败，不能返回旧 job 当作成功。

### 4.4 merge

- 所有写图操作先取得 `pg_advisory_xact_lock(824, 1)`。
- 四节点并发测试后未撤销图无环。
- reverse 行不成边，不改写关系或证据端点。
- 所有登录运行时角色对 merge/reverse `EXECUTE` 均为拒绝；active principal 不能替代 senior_reviewer 授权。

## 5. 阶段结论格式

通过：

```text
G8-GATE-8.x 独立技术验收通过。
起点 SHA：<full sha>
验收 SHA：<full sha>
通过用例：<ids>
无阻断项。
技术上具备进入 WP8.y 的条件；是否放行由项目负责人决定。
```

不通过：

```text
G8-GATE-8.x 独立技术验收不通过。
验收 SHA：<full sha>
阻断项：<ids and evidence>
后续 WP8.y–WP8.6 与 WP9 保持关闭。
```

任何整改都必须针对新 SHA 重新执行该阶段全部 required 用例。

## 6. 最终签署

最终结论只能二选一：

```text
G8 独立技术验收通过；完成规定角色签署后可由项目负责人开启 WP9。
```

或：

```text
G8 独立技术验收不通过；WP9 保持关闭。
```

禁止通过删除反例、降低质量阈值、修改冻结预期或把 no-op 记为成功来取得通过。
