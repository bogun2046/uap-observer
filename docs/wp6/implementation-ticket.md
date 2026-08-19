# WP6 实现单：文档提取与版本化

- 实现编号：`WP6-IMPL-20260819-01`
- 冻结标准：`G6-FROZEN-20260819-01`
- 前置提交：`0efefaa9c1e7b83e179e4b9c0d26b7caca6971c5`
- 前置门禁：G5 通过，WP6 已开启
- 负责人：文档处理负责人 + 后端负责人
- 当前状态：待实现

## 输入与边界

1. 任务 payload 必须包含 `document_version_id`、`extractor_name`、`extractor_version` 和 `payload_schema_version=extract.v1`。
2. Worker 必须通过文档应用服务读取 `core.document_versions` 及其 `artifact_version_id`，再通过对象登记服务读取 raw 对象。
3. Extractor 不得根据 URL 发起网络请求，不得直接拼接对象 key，不得访问 `public`、`audit` 或知识域写表。
4. 所有任务使用 WP4 的 attempt、lease token、重试和 dead-letter 语义；成功、失败和租约失效都必须结束当前 attempt。

## 实现要求

### 1. 稳定提取契约

- 定义版本化的 `extract.v1` 输入/输出 DTO。
- 输出必须包含规范化正文、正文 SHA-256、标题、作者、语言、来源日期、提取器名称/版本和 `location_map`。
- 输出只能引用已登记的 `document_version_id`，不能把提取内容写回 raw artifact 或覆盖文档版本。

### 2. 三类适配器

- HTML：去除脚本、样式、导航和重复模板噪声；保留正文段落顺序和可审计的字符范围。
- PDF：保留页码范围；无法提取文本时返回结构化失败，不伪造空正文成功。
- 字幕：支持 WebVTT/SRT 的时间区间和文本顺序；保留毫秒级 `time_start_ms/time_end_ms`。
- 同一输入、同一 extractor 版本和同一配置必须得到相同的规范化输出及哈希。

### 3. 追加式持久化

- 成功结果写入 `core.extractions`，正文写入 `core.stored_objects(storage_domain='derived')`。
- 失败结果写入 `core.extractions(outcome='failed')`，不得留下未登记 derived 对象。
- 同一 `(document_version_id, extractor_name, extractor_version, output_sha256)` 幂等；并发执行不得产生重复登记或数据库—对象存储不一致。
- 已有成功结果不能被失败重试清空或改写；更换 extractor 版本只能追加新结果。

### 4. 资源与安全边界

- 对输入字节、输出字节、页数、字幕条目数和单任务耗时设置显式上限。
- 解析失败、超限、编码错误和恶意结构必须转换为可审计的错误码及任务结果。
- 日志只记录 ID、哈希、计数、错误码和净化摘要，不记录原文、字幕文本或 PDF 正文。

## 开发自测门槛

- Ruff、mypy、全量既有测试和新增 G6 契约测试通过。
- HTML、PDF、WebVTT、SRT 固定夹具重复执行输出哈希一致。
- 真实 PostgreSQL + 对象存储验证成功、失败、并发幂等、对象补偿和扫描幂等。
- 真实 `uap_worker` 验证 attempt/lease、retry_wait、dead 和 stale lease 路径。
- 迁移链、权限检查、WP2/WP3/WP4/WP5 检查及 42 项以上证据清单全部通过。

## 完成定义

只有 G6-01 至 G6-08 全部通过、独立验收记录已签署、Draft PR 的 quality/security/integration/gate 全绿后，WP7 门禁才可开启。

