# WP2 / G2 冻结验收用例

冻结编号：`G2-FROZEN-20260812-01`  
冻结时间：2026-08-12（Asia/Shanghai）  
状态：实现前已冻结；变更标准必须新建版本并说明原因。

> 安全整改补充：MinIO 专有名称已由 `G2-FROZEN-20260812-01-A1`
> 以“受维护的 S3 兼容对象存储”替代；其余功能预期不变。原文保留用于
> 证明冻结历史，具体见 `acceptance-amendment-01.md`。

## G2-01 一个命令启动开发环境

- 前置：仅安装 Git、Make、Docker Engine 和 Docker Compose v2；工作区不存在 `platform/.env`。
- 操作：在仓库根目录执行 `make dev`。
- 预期：命令自动生成仅限本地的随机密钥；PostgreSQL、MinIO 和平台健康服务启动；`docker compose ps` 全部为 healthy 或一次性初始化成功；`GET /healthz` 返回数据库和对象存储均 ready。日志不得打印密码或访问密钥。

## G2-02 空环境自动创建数据库和对象桶

- 前置：使用全新的 Compose project name 和空 volume。
- 操作：启动开发环境，查询 PostgreSQL 当前数据库并列出 MinIO bucket。
- 预期：自动创建配置的数据库和 `raw`、`derived`、`model-io`、`public-assets` 四个 bucket；Alembic 可从空环境执行到 head；此阶段不存在业务数据迁移。

## G2-03 CI 失败关闭

- 操作：检查 PR 工作流，并在隔离副本分别制造测试失败、类型/格式失败、迁移命令失败、依赖审计失败或密钥扫描命中。
- 预期：对应 job 非零退出，汇总 `gate` 不运行成功；只有锁文件校验、lint、类型、测试、迁移冒烟和全部安全扫描成功时 `gate` 才成功。仓库保护规则将 `Platform CI / gate` 配置为 required check。

## G2-04 密钥不进入 Git、日志和构建产物

- 操作：检查 Git 跟踪文件、`.gitignore`、`.dockerignore`、镜像配置/历史、Compose 渲染结果和运行日志；运行 Gitleaks 与配置脱敏测试。
- 预期：只提交空模板和变量名称；实际值只存在于忽略且权限受限的 env 文件或 CI/staging secret provider；配置对象和日志不回显 SecretStr；构建上下文排除 env、数据库、证据和 Git 历史。

## G2-05 本地、CI 与 staging 主版本一致

- 操作：运行版本策略检查并交叉核对 Python、PostgreSQL、MinIO、uv 和锁文件入口。
- 预期：三类环境共同读取 `platform/.env.versions`；Python 限定 3.12，PostgreSQL 限定 16；Dockerfile、Compose、CI 和 staging 不出现冲突主版本；安装均使用 `uv sync --frozen`。

## G2-06 staging 可重复部署

- 前置：准备权限为 0600 的 staging env 文件和一台安装 Docker Compose v2 的目标机。
- 操作：连续两次执行 `platform/scripts/deploy-staging.sh /path/to/staging.env`。
- 预期：两次均成功；第二次不创建重复数据库、bucket 或配置；先执行迁移再原地更新服务；健康检查通过；脚本不删除 volume、不回显密钥，失败时返回非零。

## G2 验收责任

- 开发负责人：G2-01、G2-02、G2-03、G2-05。
- DevOps：G2-01、G2-03、G2-04、G2-05、G2-06。
- 实现人员提交开发自检，不替代独立验收。
