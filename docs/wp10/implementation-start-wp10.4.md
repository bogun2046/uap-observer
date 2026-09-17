# WP10.4 正式启动记录

```text
G10-GATE-10.3
Signed SHA: 10773f135ef5fca236db2eebee75fd6b15f3cd0f
```

- 已签门禁：`G10-GATE-10.3`
- 签署 SHA：`10773f135ef5fca236db2eebee75fd6b15f3cd0f`
- 实施分支：`codex/wp10.4`
- 起点要求：从签署 SHA 第一父线性追加
- migration：`0023_wp10_api_read_indexes`
- 范围：公开匿名只读 HTTP、keyset cursor、PostgreSQL search 查询与最小读取索引，G10-16–G10-20

本阶段不实现 Admin/OIDC/JWT、写 HTTP、relation 成功路径、外部搜索、WP10.5/10.6、
WP11、Makefile、Docker/compose、workflow 或 CI 总接线。完成 G10-16–G10-20 后只创建
一个普通追加提交，立即 STOP；不签署 `G10-GATE-10.4`，不进入 WP10.5，不 push、
不创建 PR、不启动 CI。
