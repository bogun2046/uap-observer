# G2 第二轮整改提交锚点

- 整改编号：`WP2-REMEDIATION-20260812-02`
- 对应驳回：`G2-REJECT-20260812-02`
- 当前结论：实现侧整改完成，G2 仍不通过，WP3 门禁关闭
- 验证结果：22/22 策略检查通过，44/44 文件 SHA-256 复核通过
- 验证报告：`validation.json`
- 验证报告 SHA-256：`863c342d47c7f48b7e7d031f65444f0b597b6ef9ce462069179801af25c4514d`
- 源文件清单：`MANIFEST.sha256`
- 源文件清单 SHA-256：`027be33f0e1a8d81b916c672d8cff5ffb7b368076936f261b9d6d6d376f2e110`
- 容器 quality：lock、Ruff、mypy、12/12 pytest、策略检查全部通过；覆盖率 92.75%
- 依赖审计：修正后的 workflow 原命令通过，未发现已知漏洞
- 镜像扫描：最终 app、PostgreSQL、SeaweedFS 均为 0 High/Critical
- 待完成：Git 固定提交、真实 pull request required gate、预期失败注入与恢复后的绿色 gate
- 日期：2026-08-12（Asia/Shanghai）

本锚点记录提交前的本地可复现证据，不构成 G2 通过结论。GitHub 运行态结果须在推送后追加独立证据。
