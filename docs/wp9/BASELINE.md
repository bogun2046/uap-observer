# WP9 冻结设计基线

- 日期：2026-08-25（Asia/Tokyo）
- 仓库：`bogun2046/uap-observer`
- 父基线：G8-GATE-8.6 已签署 `8550b8fe2d3322428fc9487e91aeb830425b0ed1`
- 前置 PR：<https://github.com/bogun2046/uap-observer/pull/36>
- 前置 CI：<https://github.com/bogun2046/uap-observer/actions/runs/32834875538>
- 冻结标准：`G9-FROZEN-20260825-05`
- 实施编号：`WP9-IMPL-20260825-01`
- 验收编号：`WP9-ACCEPT-20260825-01`
- 文档状态：**R5 待 Codex 冻结；冻结前不得实施**
- 编码门禁：**关闭，直至项目负责人发出 WP9.1 启动口令**

## 冻结提交与起点

设计链上有两个固定身份，均不得回写“当前 HEAD”到本文件（避免 Git 自引用）：

```text
G9_DESIGN_ROOT_SHA = 984da00b52b39bb4f22c365a8792c3d607724733
G9_DESIGN_SHA       = 本冻结编号最终签署的 docs-only HEAD
```

`G9_DESIGN_ROOT_SHA` 是 WP9 设计根（R1 首份 docs-only 提交）。`G9_DESIGN_SHA` 是其后经 R2–R5 普通追加得到的最终冻结 HEAD，只能在该提交形成后由审核方解析。

WP9.1 的唯一合法起点是项目负责人启动口令中明确列出的 `G9_DESIGN_SHA`。它必须满足：

1. `G9_DESIGN_ROOT_SHA^` = `8550b8fe2d3322428fc9487e91aeb830425b0ed1`；
2. `G9_DESIGN_SHA` 是 `G9_DESIGN_ROOT_SHA` 的 **第一父链后代**（`git merge-base --is-ancestor ROOT SHA` 为真，且从 SHA 沿 `^` 能回到 ROOT）；
3. `8550b8fe..G9_DESIGN_SHA` 全链 **不得有 merge**（每个提交恰好一个父提交）；
4. `git diff --name-only 8550b8fe..G9_DESIGN_SHA` 只能包含 `docs/wp9/**`；
5. `SHA256SUMS` 对除自身外的冻结文档逐项通过；
6. 项目负责人将完整 SHA、阶段编号、任务书和验收用例一并发送给实施者。

**不再要求** `G9_DESIGN_SHA^` 等于 G8 SHA。R2 及之后的整改必须普通追加在设计根之后，禁止 amend / rebase / force 去把最终 HEAD 的第一父改回 G8。

禁止 WP9.1 再从 WP8.6 之前的 SHA 分叉，也禁止从 `G9_DESIGN_ROOT_SHA` 而不是最终 `G9_DESIGN_SHA` 开工。WP9.2–WP9.6 只能从上一阶段的已验收 SHA 继续。

## 本提交范围

仅包含架构与验收文档：

- 没有业务代码；
- 没有 Alembic 迁移；
- 没有 CI、Makefile 或覆盖率配置变更；
- 没有生产数据或对象存储变更；
- 不修改 `docs/wp8/**`、ADR 0001–0013、migration 0001–0013。

## 角色与放行

- Codex 负责架构冻结与独立技术审核。
- Grok 负责按冻结设计实施当前获准阶段，不得改 ADR 或验收口径。
- 项目负责人拥有唯一阶段放行权。
- Codex 的“技术上具备进入下一阶段条件”不会自动授权 Grok 继续；必须由项目负责人给出明确启动口令。

在启动口令前，Grok 只能保持停止状态。每个 WP9.x 交付固定 SHA 后必须停止，等待项目负责人转交 Codex 审核。

## 权威顺序

发生冲突时按以下顺序解释：

1. 本冻结标准下的 ADR 0014–0019；
2. `implementation-ticket.md`；
3. `acceptance-ticket.md` 与 `acceptance-cases.md`；
4. 已签署 G8 SHA 中的 WP1–WP8 约束（含 ADR 0001–0013 与 0002 表结构）；
5. WP1 `openapi.yaml` 仅为合同草案，数据库枚举与表结构优先；
6. 实施者说明、自测报告或对话推断。

任何无法按该顺序消解的冲突均关闭编码门禁，由 Codex 出具设计勘误或新冻结编号。
