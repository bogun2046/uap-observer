# ADR-0007：优先使用 PostgreSQL 全文搜索

- 状态：Accepted for G1
- 日期：2026-08-11

## 背景

公开产品需要全文搜索，但初始数据量和团队规模不支持无证据地增加独立搜索集群。

## 决策

搜索只索引 `public.search_documents`，初始使用 PostgreSQL `tsvector` + GIN 和结构化 facets。达到容量基线后若连续两周不能满足 p95 目标，再通过新 ADR 评估外部搜索服务。

## 后果

- 公开读模型与搜索结果保持事务一致，备份和权限更简单。
- 需要为中英文分词质量建立黄金查询集。
- 独立搜索迁移必须保留 public revision、撤回和缓存失效语义。
