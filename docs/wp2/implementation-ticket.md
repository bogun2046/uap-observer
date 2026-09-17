# WP2 实现单

- 实现编号：`WP2-IMPL-20260812-01`
- 输入：G1 已验收架构、模块边界、权限模型和服务目标
- 状态：首轮独立验收不通过；G2-D01 安全整改实施中
- 实现责任：开发负责人
- 对应验收单：`WP2-ACCEPT-20260812-01`

## 范围

1. 建立隔离的 `platform/` Python 3.12 工程骨架和依赖锁。
2. 建立一个命令启动的 PostgreSQL、S3 兼容对象存储和平台健康服务。
3. 建立空 Alembic 权威迁移框架，为 WP3 提供唯一迁移入口。
4. 建立 PR CI：锁文件、lint、类型、测试、迁移、依赖审计、代码安全和密钥扫描。
5. 建立本地随机密钥生成、环境模板和 staging 幂等部署入口。
6. 完成开发自测并提交证据，不自行宣布 G2 通过。

## 首轮实现结果（已被 G2-D01 否决）

- 新工程：`platform/`，Python 3.12.11，uv 锁定 65 个包。
- 本地入口：根目录 `make dev`；随机密钥由 `platform/scripts/bootstrap-env.sh` 生成。
- 基础设施：PostgreSQL 16.10、MinIO、四个私有 bucket、空 Alembic 权威迁移链和健康服务。
- CI：`quality`、`security`、`integration` 三个失败关闭 job，汇总为 required `gate`。
- staging：同一 Compose/版本源的幂等部署脚本，不包含删除 volume 的路径。
- 证据：`artifacts/wp2-engineering-20260812/`。

## G2-D01 整改范围

- Python 升级为 3.12.13 Alpine；PostgreSQL 升级为 16.14，并扫描去除
  脆弱启动辅助工具后的实际运行镜像。
- MinIO server/client 服务替换为受维护的 SeaweedFS 4.41；应用继续通过
  S3 兼容接口访问，bucket 初始化改为平台自有幂等任务。
- CI 增加最终 app、PostgreSQL 和对象存储镜像扫描，对任意 High/Critical
  失败关闭；增加可审计的失败注入入口。
- 完成 Docker 空环境、密钥/日志/镜像历史、GitHub required gate 和两次
  staging 部署实测后，重新生成整改证据和完整清单，再申请独立复验。

## 明确禁止

- 不改动或迁移现有 SQLite 数据。
- 不在 `src/uap_observer/migrations`、根 `migrations` 或新包内增加目标 PostgreSQL DDL。
- 不提交 `.env`、私钥、Token、密码或云凭据。
- 不以本机缺少 Docker 为由伪造容器运行结果。
