# WP10.1 整改启动证据

- 记录日期：2026-08-28（Asia/Shanghai）
- 设计冻结号：`G10-FROZEN-20260828-01`
- `G10_DESIGN_SHA`: `e9a5587e4a87da22eeef2f3512938db4871db6a8`
- `WP10_DESIGN_ROOT_SHA`: `e9a5587e4a87da22eeef2f3512938db4871db6a8`
- 实施分支：`codex/wp10.1`
- 实施起点：`e9a5587e4a87da22eeef2f3512938db4871db6a8`
- 本轮普通追加提交：不得 amend、rebase、force-push 或覆盖设计起点。

## 授权记录

项目负责人在本任务中明确给出启动口令：**“按以上要求，开工”**。本记录将该口令与冻结号、完整设计 SHA、WP10.1 范围绑定，作为本轮整改的启动证据；它不是 `G10-GATE-10.1` 签署。

## 允许范围

本轮仅处理审核退回的 WP10.1 阻断问题与验收证据：

1. claim 发布依赖与 document grant 的并发锁定；
2. legacy quarantine 的追加式解决记录；
3. manifest canonical payload 的 timezone 独立性；
4. downgrade 恢复 0019 的 structured claim changes；
5. downgrade 恢复 0019 的 `uap_api` function EXECUTE 权限；
6. 静态 validator、真实角色 probe、隔离 migration probe 与文档证据。

不得添加 Publisher、public HTTP、relation 成功路径、WP10.2 及后续阶段实现。

## 停止线

- 本轮不签署 `G10-GATE-10.1`。
- 本轮不 push、不创建 PR、不启动 CI。
- 完成整改和本地证据复跑后立即 STOP，等待项目负责人明确写出“通过”或等价授权；在此之前不得进入 WP10.2。
