# G2 第二轮补充整改报告

- 整改编号：`WP2-REMEDIATION-20260812-02`
- 对应驳回：`G2-REJECT-20260812-02`
- 状态：实现侧整改和本地复核完成，待 GitHub 运行态证据及独立复验
- G2 状态：未通过
- WP3 门禁：关闭
- 日期：2026-08-12（Asia/Shanghai）

## G2-R2-D01：quality 容器上下文

quality job 仍使用固定开发镜像提供 Python 3.12.13、uv 和锁定工具链，但将完整 GitHub checkout 只读挂载到 `/repo`，以 `/repo/platform` 为工作目录。这样测试和策略验证器同时读取本次提交的 `platform/.env.versions`、仓库 workflow 和 `docs/wp2`，不会错误读取镜像构建时的旧副本。Ruff、mypy、coverage 和 uv 的可变输出均写入 `/tmp`，只读源码树不会产生缓存或证据污染。

测试中的 `time.sleep` 改用 mypy 可识别的字符串补丁路径。整改后按 workflow 命令在容器内复跑：

- `uv lock --check`：通过，65 包锁定集合保持一致。
- Ruff：通过。
- mypy strict：12 个源文件，无问题。
- pytest：12/12 通过，总覆盖率 92.75%，门槛 80%。
- WP2 策略检查：23/23 通过，44 个待验文件。

## G2-R2-D02：pip-audit 缓存权限

security job 为非 root `uap` 用户设置 `XDG_CACHE_HOME=/tmp/.cache`，并给 pip-audit 传入 `--cache-dir /tmp/pip-audit`。按 workflow 原命令联网复跑退出 0，锁定依赖未发现已知漏洞；不再访问不可写的 `/home/uap/.cache`。

## 镜像门禁复核

基于固定 Trivy 0.73.0 和 2026-08-12 漏洞库，重新构建并扫描本轮最终产物：

| 产物 | OS | High/Critical |
|---|---|---:|
| 最终 app runtime | Alpine 3.23.5 | 0 |
| 加固 PostgreSQL 16.14 | Alpine 3.24.1 | 0 |
| SeaweedFS 4.41 固定摘要 | Alpine 3.24.1 | 0 |

扫描脚本逐个使用 `--severity HIGH,CRITICAL --exit-code 1`，没有忽略未修复漏洞。

## G2-R2-D03：固定版本与 GitHub 证据

本轮将重建验证报告和 SHA-256 清单，并将 WP0、已通过的 WP1、WP2 实现、workflow 及相应历史证据提交为固定版本。旧 `artifacts/wp2-engineering-20260812` 永久保留首轮提交时的 35 文件证据，不用于证明本轮文件一致性。

以下事项只能在提交推送后完成，因此本文不提前声称通过：

1. 在真实 pull request 上观察 `Platform CI / gate`。
2. 将该 check 配置或确认成 `main` 的 required status check。
3. 提交一次可识别的预期失败注入，确认 security 和汇总 gate 均失败。
4. 移除注入并确认同一 PR 的 quality、security、integration、gate 全部通过。

完成上述证据后仍须申请独立复验；开发侧无权把 G2 改为通过。

## Actions 运行时补充整改

首次真实 CI 全绿后，GitHub runner 对 `actions/checkout@v4` 给出 Node.js 20 弃用告警。最终 workflow 已升级到官方 `v7.0.1` 对应的不可变提交 `3d3c42e5aac5ba805825da76410c181273ba90b1`，避免可变版本标签和已弃用 Action 运行时进入待验基线。
