# ADR-0001：模块化单体与独立 Worker/Publisher 进程

- 状态：Accepted for G1
- 日期：2026-08-11

## 背景

当前系统规模不足以证明微服务带来的部署和数据一致性成本合理，但采集、提取、AI 和发布需要独立重试、超时恢复、权限和扩缩容；发布凭据不能授予普通 Worker。

## 决策

使用一个模块化单体代码库和领域边界；HTTP API、调度器、普通 Worker 和 Publisher 以四类独立进程部署。Publisher 只运行 publishing handler 并使用 `uap_publisher`，普通 Worker 不加载该 handler 且没有 public 权限。所有持久事实进入同一 PostgreSQL，异步工作通过持久化 jobs 和 Outbox 串联。

## 后果

- 领域事务可以在单数据库事务内完成，部署和本地开发较简单。
- Worker 与 Publisher 可独立扩缩容和失败恢复，但使用不同凭据与 handler 白名单，并遵守模块应用服务接口。
- 模块边界通过导入规则、repository 所有权和契约测试强制；未来只有在容量或组织边界得到证据时拆服务。

## 未采用方案

- 全同步单进程：无法可靠处理外部服务延迟和失败。
- 立即拆微服务：增加分布式事务、版本契约和运维成本，当前无必要证据。
