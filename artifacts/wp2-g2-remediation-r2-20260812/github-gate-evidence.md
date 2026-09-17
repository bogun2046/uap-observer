# GitHub required gate 与失败传播证据

- 仓库：`bogun2046/uap-observer`
- Draft PR：`#25`，`codex/g2-ci-remediation -> main`
- main required check：`gate`，GitHub Actions App ID `15368`，strict 模式
- 证据日期：2026-08-12（Asia/Shanghai）

## 基线成功

运行 `31576514417` 对提交 `650da5014ca9e990815c83419eacc456c2a2d64f` 执行真实 pull request workflow：quality、security、integration 和 gate 全部成功。

## 失败注入

提交 `a3208382e95be22ec127d575fc3337e54b27ae07` 将 workflow 中已有的 `Deliberate security failure injection` 临时强制执行。运行 `31576957127` 的结果为：

- quality：成功；
- integration：成功；
- security：在前置镜像扫描、pip-audit、Bandit、Gitleaks 全部成功后，由明确的注入步骤以退出码 86 失败；
- aggregate gate：失败；
- PR merge state：`BLOCKED`。

## 恢复成功

提交 `25c2bb3a20d84b52a8bd66429f2d1acbc24a1eed` 恢复 `if: ${{ inputs.failure_injection }}`，文件内容回到清单版本。运行 `31577241272` 的 quality、security、integration 和 required gate 全部成功。

首次运行发现 `actions/checkout@v4` 的 Node.js 20 弃用告警。最终待验 workflow 已改为官方 `actions/checkout` v7.0.1 的不可变提交 SHA，并须以其后的最终绿色运行作为复验依据。

本证据证明失败从 security job 传播到 required aggregate gate；不构成 G2 独立验收通过结论。
