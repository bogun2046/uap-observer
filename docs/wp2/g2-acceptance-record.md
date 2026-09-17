# G2 独立验收通过记录

- 验收编号：`G2-ACCEPT-20260812-01`
- 冻结标准：`G2-FROZEN-20260812-01`
- 对应实现：`WP2-REMEDIATION-20260812-02`
- 已验技术版本：`0c89a97dc44fda991949d4aa70653b45bcde8bba`
- Draft PR：`https://github.com/bogun2046/uap-observer/pull/25`
- 测试环境：GitHub Actions `ubuntu-latest`、Docker Desktop 独立复验环境、独立 staging Compose 项目
- 前置数据：空 PostgreSQL volume、空对象存储 volume、随机 0600 环境文件
- 验收日期：2026-08-12（Asia/Shanghai）
- 验收结论：通过，无新增阻断项
- 门禁：WP3 开启

## 用例结论

| 用例 | 结论 |
|---|---|
| G2-01 单命令启动 | 通过；空环境执行 `make dev`，自动生成 0600 env，服务全部 healthy |
| G2-02 空环境初始化 | 通过；数据库、Alembic、四个 bucket 及幂等 `created=[]` 成立 |
| G2-03 CI 失败关闭 | 通过；最终四个 job 全绿，失败注入正确传递到 required gate |
| G2-04 密钥边界 | 通过；按已登记的 Docker daemon 可信运维边界执行 |
| G2-05 版本与安全 | 通过；最终 app、PostgreSQL、SeaweedFS 均为 High=0、Critical=0 |
| G2-06 staging 重复部署 | 通过；连续部署、健康检查、volume 复用和幂等补跑成立 |

## 关键证据

- 最终运行 `31577937844`：quality、security、integration、gate 全部成功。
- 失败注入运行 `31576957127`：quality/integration 成功，security/gate 失败，PR 被 required gate 阻断。
- 恢复运行 `31577241272`：四个 job 全部恢复成功。
- main 保护：GitHub Actions `gate`（App ID 15368）为 required check，strict 模式开启。
- 容器复跑：Ruff、mypy、12/12 pytest、92.75% 覆盖率、23/23 策略检查通过。
- 44/44 文件哈希通过；清单 SHA-256 为 `4b803947818923788e425bb8c3f4a423528f080cacf18ab5ba09155d4d9858cd`。
- pip-audit 无已知漏洞；Gitleaks 扫描 129 个提交无泄漏。
- `actions/checkout` 固定提交与官方 `v7.0.1` tag 一致。
- PR 未改动 `data/uap.db`，未纳入 WP0 数据归档或无关设计文件。

## 缺陷处置

- `G2-D01`：关闭。
- `G2-R2-D01`：关闭。
- `G2-R2-D02`：关闭。
- `G2-R2-D03`：关闭。

首轮和第二轮不通过记录继续永久保留。本记录依据独立复验结论登记；验收登记后的新 head 必须重新通过 required `gate` 后方可合并。
