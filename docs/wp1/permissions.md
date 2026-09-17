# 权限模型与安全边界

## 1. 身份原则

- 人员身份由企业 OIDC 提供，应用只保存不可逆主体 ID、显示名快照和角色绑定。
- Worker、调度器、发布器、迁移器使用独立服务账号，不共享数据库凭据。
- 应用层 RBAC 与 PostgreSQL Schema 权限同时生效；应用授权不能替代数据库最小权限。
- 对象存储不开放匿名桶；公开证据只通过公开投影中的来源 URL 或受控衍生对象提供。

## 2. 应用角色

| 角色 | 允许能力 | 明确禁止 |
|---|---|---|
| `viewer` | 浏览公开站点/API | 访问任何内部接口 |
| `reviewer` | 查看候选和证据；通过、驳回、争议；提出修订 | 修改来源配置、角色、Prompt、系统设置 |
| `senior_reviewer` | reviewer 能力；撤回已发布内容；批准实体合并/撤销 | 修改基础设施和密钥 |
| `data_operator` | 管理来源启停、重跑/死信恢复、查看净化运行日志 | 审核自己的候选；读取 API Key；直接写 public |
| `model_manager` | 管理 Prompt 版本、模型白名单和费用阈值 | 审核或发布模型输出 |
| `security_admin` | 管理角色绑定、查看安全审计 | 修改内容结论、删除审计事件 |
| `platform_admin` | 受控系统配置和运维 | 绕过审计；读取密钥明文；直接更新审核历史 |
| `audit_reader` | 只读审计与验收报告 | 任何业务写入 |

关键职责分离：产生 AI 候选的服务账号不能审核；普通 reviewer 不能审核自己人工创建的候选；撤回和实体合并要求 senior reviewer；生产迁移和内容发布使用不同服务账号。

## 3. PostgreSQL 角色矩阵

图例：`R` 读、`W` 受模块约束的写、`A` 仅追加、`M` 迁移 DDL、`—` 无权限。

| 数据库角色 | `ingest` | `core` | `ops` | `audit` | `public` |
|---|---:|---:|---:|---:|---:|
| `uap_owner` | M | M | M | M | M |
| `uap_migrator` | M | M | M | M | M |
| `uap_api` | R | R/W | R/W | R/A | R |
| `uap_worker` | R/W | R/W | R/W | A | — |
| `uap_scheduler` | R | — | R/W，仅 jobs/outbox | A | — |
| `uap_publisher` | R | R | R/W，仅发布 jobs | R | R/W |
| `uap_public_reader` | — | — | — | — | R |
| `uap_audit_reader` | R | R | R | R | R |
| `uap_backup` | R | R | R | R | R |

实现要求：

- `uap_public_reader` 的 `search_path` 固定为 `public,pg_catalog`，并显式撤销 `PUBLIC` 对其他 Schema 的权限。
- 公开 API 连接池只能使用 `uap_public_reader`，不能在同一进程复用后台数据库凭据。
- `uap_worker` 对 `public` 没有任何权限；发布 handler 只能运行在使用独立 `uap_publisher` 凭据的 Publisher 进程中。
- 五个 Schema 中只有 `uap_owner`、临时启用的 `uap_migrator` 和常驻 `uap_publisher` 对 `public` 拥有写权限；普通 API、Worker、Scheduler 均不能写公开投影。
- `audit.audit_events` 对业务角色只授予 `INSERT`；更正通过补充事件而不是 UPDATE。
- 迁移器只在部署窗口启用，不作为常驻应用凭据。

## 4. 字段级公开规则

| 数据 | 内部可见 | 公开可见 | 规则 |
|---|---:|---:|---|
| 来源名称、原始链接、发布日期 | 是 | 是 | 必须经发布投影复制 |
| 经审核标题、摘要、分类、事实状态 | 是 | 是 | 带公开 revision |
| 经审核 Claim | 是 | 是 | 至少一条公开 evidence |
| 最小证据摘录、页码/时间码 | 是 | 是 | 只公开审核通过片段 |
| 原始 HTML/PDF/字幕对象 | 是 | 否 | 仅内部对象存储角色 |
| 完整提取正文 | 是 | 否 | 不进入 `public` 或 API DTO |
| Prompt 和输出 Schema | 受限 | 否 | model_manager/audit_reader |
| 模型原始请求/响应 | 受限 | 否 | model_manager/audit_reader，签名 URL 短时有效 |
| Token、费用、Provider 错误 | 受限 | 否 | 运维指标，不公开 |
| API Key/OAuth Token | 密钥管理器 | 否 | 不进入数据库、日志、对象或构建物 |
| 审核人身份、内部理由 | 受限 | 否 | 公开只显示内容状态和修订时间 |
| 内部 job ID、attempt、死信 | 受限 | 否 | 不出现在公开错误响应 |

## 5. 对象存储权限

| 前缀/桶 | 写入者 | 读取者 | 生命周期 |
|---|---|---|---|
| `raw/{sha256}` | collection | documents、audit、backup | 不可变，长期/归档 |
| `derived/{sha256}` | documents/model | knowledge、review、audit、backup | 不可变，长期 |
| `model-io/{sha256}` | model_governance | model_manager、audit | 受限，按保留策略 |
| `public-assets/{release}/{path}` | publisher | CDN/公众 | 可缓存，可由 release 重建 |
| `backups/{backup_id}` | backup service | restore operator | 加密、保留锁 |

- 对象 key 不包含来源标题、人员姓名、URL query 或密钥片段。
- 上传必须携带预期 SHA-256，完成后服务端再次校验。
- 原始和模型对象使用服务端加密；生产读取写审计日志。
- 签名 URL 最长 10 分钟，且不向公开 API 生成 raw/model-io URL。

## 6. API 授权矩阵

| API 组 | viewer | reviewer | senior_reviewer | data_operator | model_manager | security_admin |
|---|---:|---:|---:|---:|---:|---:|
| `/v1/*` 公开只读 | R | R | R | R | R | R |
| `/admin/v1/review-cases` | — | R/W | R/W | R | R | R |
| `/admin/v1/withdrawals` | — | — | W | R | — | R |
| `/admin/v1/entities/merges` | — | 提议 | W | R | — | R |
| `/admin/v1/sources` | — | R | R | R/W | R | R |
| `/admin/v1/jobs` | — | R | R | R/W | R | R |
| `/admin/v1/prompts` | — | R | R | R | R/W | R |
| `/admin/v1/role-bindings` | — | — | — | — | — | R/W |
| `/admin/v1/audit-events` | — | 自身相关 | R | R | R | R |

所有写请求要求 OIDC、CSRF 防护（浏览器会话）、`Idempotency-Key`、操作理由和 request ID；结果写入 `audit.audit_events`。

## 7. G1 安全断言

1. `uap_public_reader` 执行 `SELECT` 访问 `ingest/core/ops/audit` 必须得到 permission denied。
2. OpenAPI 的公开 Schema 不出现 `raw_content`、`extracted_content`、`object_key`、`model_response`、`prompt`、`error`、`token`、`cost`、`reviewer_id`。
3. 未审核、驳回、争议未决或撤回数据不存在于 `public` 当前投影。
4. 管理员操作和服务账号操作同样产生审计事件。
5. 备份角色能读取数据但不能修改生产表或对象。
