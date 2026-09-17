# WP7 开发自检记录：G7-R2 本地整改复检

- 整改轮次：`G7-R2`
- 冻结标准：`G7-FROZEN-20260820-01`
- 自检结论：四项新增阻断已完成本地整改和运行态复检；暂不提交、不推送，待授权后独立复验

## 本地与容器质量

- 全量测试：`102 passed`
- 覆盖率：`83.15%`
- Ruff：容器内 `src tests tools alembic` 全部通过
- mypy：容器内 `66 source files` 全部通过
- WP2 策略：`23/23`
- WP3/WP4/WP5/WP6/WP7 静态检查：`9/9`、`6/6`、`7/7`、`10/10`、`8/8`

## 整改覆盖

- 语义幂等键包含文档版本、输入哈希、任务类型、Prompt、Provider、模型和 payload schema；只有成功结果可被重复任务复用，重试失败会再次调用 Provider。
- 401/403 在治理边界强制终止；429、超时和 5xx 进入 WP4 失败分类。
- Provider 输入/输出大小、调用次数、累计费用和调用超时均有上限；预算拒绝使用终止分类，不依赖 attempt 耗尽才收口；成功和失败调用都记录响应 ID、Token、费用、币种及真实开始/结束时间。
- Provider 在可终止的隔离进程中执行硬超时；错误摘要使用固定安全文本，失败原始响应限制为 `64,000` 字节，不透传响应正文；model-io 对象仍保留独立哈希登记和失败补偿路径。
- 新增 `uap_model_governance` 专用登录角色、固定 `search_path` 的 `SECURITY DEFINER` 任务收口函数，并撤销普通 Worker 对模型治理写表权限。
- model-governance 对 `core.stored_objects` 的写入由数据库触发器限制为 `model_io` 域；降级迁移会回收其数据库、Schema 和表权限。

## 真实运行态

- 全新 PostgreSQL/对象存储环境，迁移链 `0001 → 0009`、幂等升级、降级回升及迁移器关闭通过。
- 按 required 顺序 `WP3 → WP4 → WP5 → WP6 → WP7` 执行，全部通过。
- WP7 探针覆盖合法成功、Schema 失败、语义重复、Prompt 版本追加、401/403、429 重试、5xx、超时、费用预算、输出大小、调用次数、错误租约回滚、Worker 写入拒绝、model-governance 跨域写入拒绝和 public reader 隔离。
- 调用预算场景实测 Provider 调用 `3` 次、model run `4` 条，第 4 次在 Provider 前被拒绝；所有测试任务均结束当前 attempt。
- 真实迁移链降级后，model-governance 的直接数据库授权、`core/ops` Schema 使用权及 `core.stored_objects`/`core.extractions` 表权限均已回收。

本记录是开发自检证据，不替代数据库负责人、AI 负责人和 QA 的独立 G7-R2 验收，也不代表 required CI 已通过。
