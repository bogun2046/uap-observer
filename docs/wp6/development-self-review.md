# WP6 开发自检

状态：WP6 实现自检完成；尚未申请 G6 独立验收。

## 当前范围

- `extract.v1` 输入/输出契约、HTML、PDF、WebVTT、SRT 适配器已实现。
- raw 对象经 `document_version -> artifact_version -> stored_object` 读取，正文写入 derived 对象并追加 `core.extractions`。
- 提取结果与 WP4 `extract_document` 任务的 `finish_job` 在同一数据库事务内提交。
- 数据库写入失败时，新建 derived 对象进入补偿清理路径；固定夹具和运行态探针已加入仓库。

## 已完成自测

- Python 3.12.13 环境 Ruff 通过。
- Python 3.12.13 环境 mypy 严格检查通过。
- 全量平台测试、PDF/字幕/持久化新增测试通过，覆盖率满足项目 80% 门槛。
- HTML、PDF、WebVTT、SRT 固定夹具的确定性输出和定位映射有自动回归测试。

## 尚未完成

- 已在全新 PostgreSQL、对象存储和真实 `uap_worker` 连接上执行 `wp6_runtime_probe.py`：通过 5 个文档夹具、4 条成功抽取记录，坏 PDF 记录为 `invalid_pdf`，并验证并发幂等、派生对象哈希和任务闭环。
- 尚未取得包含 WP6 探针的 Draft PR required CI 全绿结果。
- 因此本报告不构成 G6 通过结论；独立验收单仍保持待验收状态，WP7 继续关闭。
