# 目标架构、C4 与数据流

## 1. 系统上下文

```mermaid
flowchart LR
    visitor["公众访问者"]
    reviewer["内容审核人员"]
    operator["平台运维与数据人员"]
    sources["外部公开来源<br/>RSS、网页、X、YouTube"]
    models["外部模型服务<br/>OpenAI、DeepSeek"]
    system["UAP Platform<br/>采集、溯源、分析、审核和公开发布"]

    visitor -->|"浏览与搜索已发布内容"| system
    reviewer -->|"审核候选、证据、实体和关系"| system
    operator -->|"配置来源、监控任务、恢复与发布"| system
    system -->|"条件请求与限速采集"| sources
    system -->|"最小必要正文、结构化请求"| models
```

系统职责边界：保存来源证据、形成可复核派生结果、执行人工审核并只发布通过的数据。系统不对来源中每项主张自动背书，也不把模型输出视为事实。

## 2. 容器图

```mermaid
flowchart TB
    public_user["公众浏览器"]
    reviewer_user["审核人员浏览器"]
    operator["运维人员"]
    ext_sources["外部来源"]
    model_api["模型 Provider API"]

    subgraph edge["公开边界"]
        web["公开站点<br/>SSR/静态资源"]
        public_api["公开 API<br/>只读、限速、OpenAPI"]
    end

    subgraph private["受信应用边界"]
        admin["审核后台<br/>OIDC + RBAC"]
        app["模块化单体 API<br/>事务与领域规则"]
        scheduler["调度器<br/>创建持久化任务"]
        worker["异步 Worker<br/>采集、提取、AI"]
        publisher["Publisher 进程<br/>公开投影、撤回、缓存失效"]
    end

    subgraph data["数据边界"]
        postgres[("PostgreSQL<br/>ingest/core/ops/audit/public")]
        objects[("S3 兼容对象存储<br/>不可变原始与派生对象")]
    end

    public_user --> web
    web --> public_api
    public_api -->|"public_reader"| postgres
    reviewer_user --> admin
    admin --> app
    operator --> app
    app -->|"领域事务"| postgres
    app -->|"签名对象 URL"| objects
    scheduler -->|"创建 jobs"| postgres
    worker -->|"租约、结果、Outbox"| postgres
    worker -->|"按哈希写入/读取"| objects
    worker --> ext_sources
    worker --> model_api
    app -->|"审核通过后请求发布"| postgres
    publisher -->|"uap_publisher：领取发布任务并写 public"| postgres
    publisher -->|"写不可变 public-assets release"| objects
```

容器约束：

- 公开 API 不连接 `ingest`、`core`、`ops` 或 `audit`；其数据库角色只拥有 `public` Schema 的 `SELECT`。
- 审核后台不直接访问数据库或对象存储，所有操作经模块化单体 API 执行并记录审计。
- Worker 不开放公网入站端口；它通过数据库任务租约取工作。
- Publisher 使用独立 `uap_publisher` 凭据，只领取发布/撤回/缓存失效任务；普通 Worker 既不调用 Publisher handler，也没有 `public` 权限。
- 调度器只创建任务，不执行采集或业务写入。
- 第一阶段搜索采用 PostgreSQL 全文索引，达到扩展阈值前不引入独立搜索集群。

## 3. 模块化单体组件图

```mermaid
flowchart LR
    api["HTTP/API 适配层"]
    commands["命令与事务编排"]
    identity["身份与权限模块"]
    source["来源注册模块"]
    ingest["采集与原始制品模块"]
    object_registry["统一对象登记模块"]
    documents["文档与版本模块"]
    knowledge["Claim、证据、实体与关系模块"]
    review["审核与修订模块"]
    publish["公开投影模块"]
    jobs["任务与 Outbox 模块"]
    model["模型治理模块"]
    audit["审计模块"]
    adapters["数据库、对象存储、HTTP、模型适配器"]

    api --> commands
    commands --> identity
    commands --> source
    commands --> ingest
    commands --> object_registry
    commands --> documents
    commands --> knowledge
    commands --> review
    commands --> publish
    commands --> jobs
    commands --> model
    commands --> audit

    source --> jobs
    ingest --> documents
    ingest --> object_registry
    documents --> object_registry
    model --> object_registry
    model --> documents
    model --> knowledge
    review --> knowledge
    review --> publish
    publish --> jobs

    identity --> adapters
    source --> adapters
    ingest --> adapters
    object_registry --> adapters
    documents --> adapters
    knowledge --> adapters
    review --> adapters
    publish --> adapters
    jobs --> adapters
    model --> adapters
    audit --> adapters
```

组件只通过公开应用服务或领域事件协作，不跨模块导入 repository 实现，不直接更新其他模块拥有的表。

## 4. 端到端数据流

```mermaid
sequenceDiagram
    participant S as 调度器
    participant J as ops.jobs
    participant W as Worker
    participant X as 外部来源
    participant O as 对象存储
    participant G as 统一对象登记
    participant I as ingest
    participant C as core
    participant OP as ops.model_runs
    participant M as 模型服务
    participant R as 审核人员
    participant A as audit
    participant P as Publisher
    participant U as public

    S->>J: 以 source + 时间窗幂等键创建 fetch 任务
    W->>J: 原子领取租约并创建 attempt
    W->>X: 条件请求、限速和冷却
    X-->>W: 响应或状态码
    W->>O: 按 SHA-256 保存不可变原始对象
    W->>G: 按 storage domain + SHA-256 登记或复用对象
    W->>I: 写 source_run、artifact、artifact_version
    W->>C: 追加 document_version
    W->>J: 完成任务并通过 Outbox 创建提取任务
    W->>C: 追加提取结果和证据定位
    W->>M: 发送最小必要文档版本
    M-->>W: 结构化候选结果
    W->>OP: 追加 model_run、Token、费用和内部响应引用
    W->>C: 追加 analysis_result、候选 Claim/实体/关系
    R->>A: 记录审核决策、理由和修订
    A->>J: 通过 Outbox 创建发布任务
    P->>J: 使用 uap_publisher 原子领取发布任务
    P->>U: 事务性重建或撤回公开投影
    P->>O: 写入不可变 public-assets release
    U-->>R: 返回发布版本和可追溯证据链接
```

## 5. 数据分层与写入权

| 层 | Schema/存储 | 内容 | 唯一写入者 | 公开可见 |
|---|---|---|---|---|
| 原始层 | `ingest` + 对象存储 `raw/` | 来源响应、HTTP 元数据、采集批次、原文件版本 | 采集模块 | 否 |
| 派生层 | `core` + 对象存储 | 统一对象登记、文档版本、提取文本、Claim、证据、实体、关系、AI 多版本结果 | 对象登记/文档/知识/模型模块 | 否 |
| 运行层 | `ops` | jobs、attempts、dead letters、outbox、Prompt、model runs、Token/费用和运行指标 | 任务/模型治理模块 | 否 |
| 审核层 | `audit` | 审核决定、修订、撤回、操作审计 | 审核/审计模块 | 否 |
| 公开层 | `public` | 已通过内容的最小化投影、搜索文档 | 发布模块 | 是，只读 |

## 6. 关键不变量

1. 对象以内容 SHA-256 寻址，`core.stored_objects(storage_domain, content_sha256)` 在同域内唯一；原始版本、提取结果和模型 I/O 只引用该登记，不各自拥有对象 key。
2. 每个文档版本必须指向一个原始 artifact version；提取失败不能替换上一个成功版本。
3. 每条 Claim 证据必须指向具体 `document_version`，并保留字符、页码或时间码定位。
4. 每条 Relation 的两端都是 `core.entities` 外键；禁止多态裸 ID。
5. 每个任务状态变化都必须有 attempt 或系统恢复原因；业务表不承担任务状态。
6. 模型运行和分析结果只追加；选择“当前候选”通过单独的 selection 记录完成。
7. 文档、Claim、实体和关系分别持有与同一 review case/decision/subject 复合绑定的有效授权后才可进入 `public`；`related_entities` 只能来自有授权依据的 `public.document_entities`。
8. 公开投影可全部重建，不作为内部事实的唯一副本。

## 7. 部署与故障边界

- 应用 API、Worker、调度器、Publisher 使用同一代码版本和模块包，但作为四类独立进程部署、使用独立凭据并独立扩缩容。
- PostgreSQL 是事务边界；Outbox 保证“领域写入”和“后续任务/发布事件”不丢失。
- 对象存储故障只阻塞依赖对象的任务，不允许写入缺少对象哈希的成功记录。
- 单一来源、单一模型 Provider 或单个 Worker 故障不应阻断其他来源和公开只读服务。
- Publisher 故障只积压发布任务；公开站点继续提供上一个成功 release，普通 Worker 不接管 Publisher 权限。
- 公开站点和 API 可以在内部采集/AI 暂停时继续提供上一个已发布版本。
