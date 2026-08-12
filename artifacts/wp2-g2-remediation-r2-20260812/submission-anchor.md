# G2 第二轮整改提交锚点

- 整改编号：`WP2-REMEDIATION-20260812-02`
- 对应驳回：`G2-REJECT-20260812-02`
- 当前结论：实现侧整改完成，G2 仍不通过，WP3 门禁关闭
- 验证结果：23/23 策略检查通过，44/44 文件 SHA-256 复核通过
- 验证报告：`validation.json`
- 验证报告 SHA-256：`1fe566aee6087f1c52302655ecf48ad16e5629ea8a57647ac27f7650f35af2bc`
- 源文件清单：`MANIFEST.sha256`
- 源文件清单 SHA-256：`4b803947818923788e425bb8c3f4a423528f080cacf18ab5ba09155d4d9858cd`
- 容器 quality：lock、Ruff、mypy、12/12 pytest、策略检查全部通过；覆盖率 92.75%
- 依赖审计：修正后的 workflow 原命令通过，未发现已知漏洞
- 镜像扫描：最终 app、PostgreSQL、SeaweedFS 均为 0 High/Critical
- 已完成：Git 固定提交、真实 pull request required gate、预期失败注入与恢复后的绿色 gate
- 日期：2026-08-12（Asia/Shanghai）

本锚点记录本地可复现证据及 GitHub 运行态证据，不构成 G2 通过结论。
