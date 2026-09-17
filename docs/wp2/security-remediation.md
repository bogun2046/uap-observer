# G2-D01 安全整改说明

## 选定基线

| 组件 | 整改后基线 | 处理 |
|---|---|---|
| Python | 3.12.13 Alpine 3.23，固定多架构摘要 | 替换 3.12.11 Debian 基线 |
| PostgreSQL | 16.14 Alpine，固定上游摘要 | 构建实际运行镜像，移除仅 root 降权使用且带脆弱 Go 标准库的 `gosu`，固定为 `postgres` 用户 |
| 对象存储 | SeaweedFS 4.41，固定多架构摘要 | 替换停止维护且受已知 High 漏洞影响的 MinIO |
| 镜像扫描 | Trivy 0.73.0，固定多架构摘要 | 扫描最终 app、PostgreSQL 和对象存储镜像；High/Critical 非零即失败 |

SeaweedFS 的 `mini` 模式提供 8333 S3 API；bucket 初始化由平台自己的
幂等 Python 任务执行，不依赖服务商专有 CLI。运行时仍使用非空随机访问
密钥，未提供密钥时不得启动 Compose。

## 本地预检

使用官方 Trivy 0.73.0 macOS ARM64 发布资产，先按官方 checksum 验证
二进制，再以 2026-08-12 当日漏洞库扫描固定摘要：

- `python:3.12.13-alpine3.23`：0 High/Critical。
- `chrislusf/seaweedfs:4.41`：0 High/Critical。
- 上游 `postgres:16.14-alpine`：发现 15 项 High/Critical，均位于
  `/usr/local/bin/gosu` 的 Go 标准库；因此 CI 只认可移除该工具后的实际
  PostgreSQL 运行镜像，不能直接放行上游镜像。

上述预检不是最终 G2 证据。最终结果必须来自 Docker 构建后的服务镜像
扫描、空 volume 启动、运行日志与镜像历史检查、GitHub required gate
以及两次 staging 部署。

## 失败关闭实现

`platform/scripts/scan-images.sh` 对三个部署产物逐一运行 Trivy，明确设置
`--scanners vuln --severity HIGH,CRITICAL --exit-code 1`，没有
`--ignore-unfixed`。任何镜像失败将终止 `security` job，汇总 `gate` 通过
`needs` 结果继续失败。workflow 的 `failure_injection` 输入用于独立复验
真实失败传播。

## 官方来源

- Python 3.12.13：https://www.python.org/downloads/release/python-31213/
- PostgreSQL 16.14：https://www.postgresql.org/docs/release/16.14/
- SeaweedFS releases：https://github.com/seaweedfs/seaweedfs/releases
- Trivy v0.73.0：https://github.com/aquasecurity/trivy/releases/tag/v0.73.0
