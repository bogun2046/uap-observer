# WP6 开发自检

状态：G6-R1 整改完成；待 R2 required CI 与独立复验。

## 当前范围

- `extract.v1` 输入/输出契约、HTML、PDF、WebVTT、SRT 适配器已实现。
- raw 对象经 `document_version -> artifact_version -> stored_object` 读取，正文写入 derived 对象并追加 `core.extractions`。
- 提取结果与 WP4 `extract_document` 任务的 `finish_job` 在同一数据库事务内提交。
- 数据库写入失败时，新建 derived 对象进入补偿清理路径；固定夹具和运行态探针已加入仓库。
- R1 整改统一剥离 HTML 媒体类型参数，并将非法 HTML 发布日期规范化为 NULL，避免异常元数据阻断任务收口。

## 已完成自测

- Python 3.12.13 环境 Ruff 通过。
- Python 3.12.13 环境 mypy 严格检查通过。
- 全量平台测试、PDF/字幕/持久化新增测试通过，覆盖率满足项目 80% 门槛。
- HTML、PDF、WebVTT、SRT 固定夹具的确定性输出和定位映射有自动回归测试。
- 新增带 `charset` 的 HTML 媒体类型、非法发布日期及真实任务闭环回归覆盖。
- 全新 PostgreSQL、对象存储和真实 `uap_worker` 环境复跑 R2 探针通过：HTML 成功完成且非法日期为 NULL。

## 尚未完成

- 尚未取得包含本轮整改的 R2 Draft PR required CI 全绿结果。
- 因此本报告不构成 G6 通过结论；独立验收单仍保持待验收状态，WP7 继续关闭。
