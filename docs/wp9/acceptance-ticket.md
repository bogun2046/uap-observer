# WP9 独立验收单

- 验收编号：`WP9-ACCEPT-20260825-01`
- 冻结标准：`G9-FROZEN-20260825-05`
- 父基线：`8550b8fe2d3322428fc9487e91aeb830425b0ed1`
- 设计根：`G9_DESIGN_ROOT_SHA` = `984da00b52b39bb4f22c365a8792c3d607724733`
- 实施起点：最终冻结 HEAD `G9_DESIGN_SHA`
- 状态：设计待冻结；实施与验收尚未开始
- 技术审核：Codex
- 阶段放行：项目负责人
- 后置门禁：WP10 关闭，直至 G9 最终签署

## 1. 独立性要求

- Codex 在隔离 clone 中审核固定 SHA，不在审核工作区修改实现。
- Grok 的自测不替代 Codex 审核；整改必须形成新提交和新审核包。
- 每次审核先确认起点、HEAD、远端分支、PR HEAD 和 CI run 指向同一提交。
- 使用全新 PostgreSQL 16 与 S3 兼容对象存储，按 `WP3 -> … -> WP8 -> WP9` 顺序执行探针。
- 权限测试使用真实 `uap_worker`、`uap_api`、`uap_scheduler`、`uap_publisher`、`uap_model_governance`、`uap_public_reader`。
- owner/migrator 只允许用于迁移、历史异常夹具，以及验证核心 merge 默认关闭。

## 2. 固定证据

每阶段必须保存：启动口令、冻结编号、起点/最终/父 SHA、`git status --short`、migration head 往返、每条用例的 SQLSTATE 与稳定 error code、CI 四项 required job、内外层 SHA-256。

## 3. WP9.x 独立验收门禁

| 门禁 ID | 阶段 | 必须通过 | 明确不要求 | 通过后 |
|---|---|---|---|---|
| `G9-GATE-9.1` | WP9.1 | G9-01–G9-05 | case/decision/grant/merge 包装 | 可授权 WP9.2 |
| `G9-GATE-9.2` | WP9.2 | G9-06–G9-09、G9-31、G9-34 | grant、selection、merge 包装 | 可授权 WP9.3 |
| `G9-GATE-9.3` | WP9.3 | G9-10–G9-16、G9-27–G9-30、G9-35 | candidate 晋升、merge 包装、手工 Claim | 可授权 WP9.4 |
| `G9-GATE-9.4` | WP9.4 | G9-17–G9-19、G9-36 | merge 包装、手工 Claim、CI 全接线 | 可授权 WP9.5 |
| `G9-GATE-9.5` | WP9.5 | G9-20–G9-22、G9-37 | 手工 Claim、orchestrator | 可授权 WP9.6 |
| `G9-GATE-9.6` | WP9.6 | G9-23–G9-26、G9-32–G9-33、G9-38 及全量复跑 | 公开投影 | 可签署 G9 |

阶段通过只表示“技术上具备进入下一阶段条件”，不会自动授权实施者继续。

## 4. 重点判定标准

1. 伪造 principal 参数或缺少 GUC / request_id 不得成功写入。
2. `uap_worker` 不得打开 case 或记录决定。
3. 任一成功 approve 必须有 grant 与 outbox，且无 `public` 行、无 `publish_*` job。
4. revise 后同一 subject 至多一行 `grant_status='active'`；superseded 行 `withdrawn_at IS NULL`。并发不同 request_id 的 revise 为串行双成功，revision 严格递增。
5. `core.merge_entities` 对 `uap_api` 仍是 `42501`。
6. 私有 `_apply_*` / `_retire_*` 对 `uap_api` 无 EXECUTE；跨事务改绑失败。
7. G8-16C 在 WP9 全阶段保持 `dead` / `terminal_failure` / `knowledge_relation_task_not_in_wp8`。

## 5. 结论模板

通过：

```text
G9 独立技术验收通过；完成规定角色签署后可由项目负责人开启 WP10。
```

不通过：

```text
G9 独立技术验收不通过；WP10 保持关闭。
```
