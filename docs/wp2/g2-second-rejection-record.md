# G2 第二轮独立复验不通过记录

- 验收编号：`G2-REJECT-20260812-02`
- 前次记录：`G2-REJECT-20260812-01`
- 结论：不通过；首轮镜像安全和 staging 阻断已关闭，发现 3 项新阻断
- 门禁：WP3 继续关闭
- 验收日期：2026-08-12（Asia/Shanghai）

## 用例结论

| 用例 | 结论 |
|---|---|
| G2-01 一个命令启动开发环境 | 通过：全新临时环境执行 `make dev`，env 权限 0600，全部服务 healthy |
| G2-02 空环境自动初始化 | 通过：数据库、四个 bucket、Alembic 及幂等补跑成立 |
| G2-03 CI 失败关闭 | 不通过：quality 与 security 原命令失败，required gate 和真实失败传播尚不可验证 |
| G2-04 密钥边界 | 部分通过：历史、镜像配置附加项与日志未泄漏；Docker daemon 检查接口和完整 Compose 渲染可见环境值 |
| G2-05 版本与安全补丁 | 通过：三个实际运行镜像 High/Critical 均为 0 |
| G2-06 staging 重复部署 | 通过：连续部署、volume 复用、健康检查和幂等补跑成立 |

## 阻断缺陷

1. `G2-R2-D01`：quality job 在仅包含 `platform/` 的应用镜像内执行，策略验证器无法读取仓库级版本源和 `docs/wp2`；同时测试中的补丁目标不满足严格 mypy。
2. `G2-R2-D02`：security job 未给非 root 用户提供可写的 pip-audit 缓存目录，原命令会因 `/home/uap/.cache` 权限失败。
3. `G2-R2-D03`：待验 WP2 与 workflow 未形成 Git 固定版本；旧清单已有 22 项不匹配且文件集合不完整，因此 required check 与失败注入没有可验收的提交锚点。

## 密钥边界登记

Docker daemon 及其宿主机管理员属于可信运维边界。具备 Docker 检查权限的主体能够读取容器 `Config.Env`；这是环境变量注入模型的已知边界，不作为应用或普通开发者权限。验收证据只允许使用 `docker compose config --quiet`、脱敏镜像检查结果和脱敏日志，禁止保存或发布非 quiet 的完整 Compose 渲染。若未来 daemon 权限向不受信主体开放，必须先迁移到文件型 secrets 或外部密钥服务。

## 整改与复验要求

- quality 命令应在固定开发镜像中运行，但只读挂载完整 checkout 并以 `platform/` 为工作目录；所有缓存和覆盖率输出写入 `/tmp`。
- 修复严格 mypy 补丁路径，并为 pip-audit 显式配置 `/tmp` 缓存。
- 按 workflow 原命令重跑 quality/security，重建完整验证报告和 SHA-256 清单。
- 提交并推送固定版本，在真实 GitHub PR 上验证 `Platform CI / gate` required check；注入一次预期失败并确认汇总 gate 关闭，再恢复并确认 gate 通过。

本记录永久保留“不通过”结论。仅后续独立复验可改变 G2 当前门禁状态。
