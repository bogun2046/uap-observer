# WP10.3 正式启动记录

- 已签门禁：`G10-GATE-10.2`
- 签署 SHA：`2718ba2dc8f6773e088f5cfb8cfe90d5070aa7ae`
- 实施分支：`codex/wp10.3`
- 起点要求：从签署 SHA 第一父线性追加
- migration：`0022_wp10_claim_search_projection`
- 范围：claim/evidence、document_entities、search、受控 rebuild，G10-11–G10-15

本阶段不实现 relation 成功路径、HTTP/API/OIDC、0023/WP10.4、CI/Makefile/workflow
总接线或 WP11。完成 G10-11–G10-15 后只创建一个普通追加提交，立即 STOP；不签署
`G10-GATE-10.3`，不进入 WP10.4，不 push、不创建 PR、不启动 CI。
