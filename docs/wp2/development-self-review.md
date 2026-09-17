# WP2 开发自检记录

> 本文记录首轮提交时的开发自检，已被 `G2-REJECT-20260812-01` 的安全
> 结论否决，不作为整改后的通过证据。整改结果另行生成新编号报告。

- 自检编号：`WP2-DEV-20260812-01`
- 冻结标准：`G2-FROZEN-20260812-01`
- Python：CPython 3.12.11
- uv：0.12.3
- 自检结论：实现侧可执行检查通过；容器和真实 staging 项等待独立环境验收
- G2 门禁：未通过、未开启 WP3

## 用例自检

| 用例 | 实现侧结果 | 待独立验证 |
|---|---|---|
| G2-01 | 根 `make dev`、随机 env、Compose 健康依赖和 `/healthz` 已实现；配置/单元检查通过 | 本机无 Docker，尚未实际启动容器 |
| G2-02 | PostgreSQL 空库、四 bucket 幂等初始化、空 Alembic `upgrade head` 已配置 | 需在空 volume 中执行并查询 |
| G2-03 | PR workflow 含 lock、Ruff、mypy、pytest、Alembic、pip-audit、Bandit、Gitleaks及失败关闭 `gate` | 需推送固定版本并观察 GitHub Actions，配置 required check |
| G2-04 | env 模板无值；本地随机生成并 chmod 600；SecretStr 脱敏；构建上下文排除 env/密钥；静态扫描、Bandit、pip-audit 通过 | Gitleaks 容器和镜像历史需在 Docker 环境复核 |
| G2-05 | Python 3.12/PostgreSQL 16/MinIO/uv 共用版本源，镜像标签和摘要固定，uv 锁定 65 个包 | 需比较实际容器版本输出 |
| G2-06 | staging 脚本验证 0600、执行幂等 bucket/迁移/up，不含 volume 删除 | 需在 staging 主机连续执行两次 |

## 自动检查结果

- `uv lock --check --offline`：通过。
- Ruff：通过。
- mypy strict：10 个源文件，无问题。
- pytest：9/9 通过，语句/分支综合覆盖率 95%。
- WP2 策略检查：18/18 通过。
- Bandit：零告警；容器内健康端点监听行为以单项 `B104` 说明豁免。
- pip-audit：对 uv 导出的带哈希完整依赖清单检查，未发现已知漏洞。
- Shell 语法：两个脚本均通过 `sh -n`。
- 官方镜像核对：Python、PostgreSQL、MinIO server/client 和 Gitleaks 的标签及多架构摘要均由 Docker Hub API 确认存在。

## 环境限制

开发机当前没有 Docker CLI/daemon，因此未声称 G2-01、G2-02、G2-03 的容器部分或 G2-06 的真实部署已通过。上述事项必须由开发负责人和 DevOps 在独立验收环境完成并留存日志。
