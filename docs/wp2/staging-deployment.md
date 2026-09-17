# WP2 staging 部署说明

## 1. 前置条件

- Linux 主机安装 Docker Engine、Docker Compose v2 和 Git。
- 目标目录检出同一固定代码版本；主机只允许部署账号读取 staging env 文件。
- 防火墙不公开 PostgreSQL；SeaweedFS S3 API 和健康端口默认绑定 `127.0.0.1`，需要外部访问时由受控反向代理或隧道提供。
- CI 的 `Platform CI / gate` 已成功，并在仓库分支保护中设为 required check。

## 2. 密钥文件

从 `platform/.env.example` 复制变量名称到主机外部的绝对路径，例如 `/etc/uap-platform/staging.env`。必须设置非空且独立生成的：

- `UAP_POSTGRES_PASSWORD`
- `UAP_S3_ACCESS_KEY`
- `UAP_S3_SECRET_KEY`

同时将 `UAP_COMPOSE_PROJECT_NAME` 设为 staging 专用名称、`UAP_APP_ENV` 设为 `staging`，并根据端口分配填写非敏感配置。不得把文件放入仓库、构建上下文或 CI artifact；权限必须为 0600。

## 3. 部署

```bash
chmod 600 /etc/uap-platform/staging.env
platform/scripts/deploy-staging.sh /etc/uap-platform/staging.env
```

脚本按以下顺序执行：

1. 验证 env 绝对路径、0600 权限和 Compose 配置。
2. 构建并启动或复用加固 PostgreSQL 与 SeaweedFS volume。
3. 由平台幂等初始化任务建立或复用四个私有 bucket。
4. 使用 runtime 镜像运行唯一 Alembic 链 `upgrade head`。
5. 原地构建并更新服务，等待健康检查，输出容器状态。

重复执行相同命令是标准更新方式。脚本不执行 `down --volumes`、数据库重建或 bucket 删除。

## 4. 验证与回滚

- `GET /healthz` 必须返回 `status=ready`，并列出 PostgreSQL 与四个 bucket。
- `docker compose ps` 中 PostgreSQL、SeaweedFS 和 app 必须 healthy，`object-store-init` 必须成功退出。
- 日志只能包含 `safe_summary` 的非敏感字段；禁止使用 `docker compose config` 输出作为可公开 artifact，因为渲染配置会包含运行时 env 值。
- 应用回滚使用上一固定代码版本和同一 env 文件重新执行部署脚本；数据库迁移回退从 WP3 起按每个迁移的回滚策略执行，不删除持久 volume。

## 5. 版本一致性

本地、CI 和 staging 均加载 `platform/.env.versions`。上游镜像同时固定可读版本标签和多架构摘要；Dockerfile 以相同 Python/uv 值构建。PostgreSQL 实际运行镜像与最终 app 镜像必须在 CI 中构建后扫描。任何升级先更新版本源和 `uv.lock`，通过 `tools/validate_platform.py` 与 CI 后再部署。
