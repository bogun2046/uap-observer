# G9 设计 R2 整改矩阵

- 冻结标准：`G9-FROZEN-20260825-02`
- 父设计 SHA：`984da00b52b39bb4f22c365a8792c3d607724733`
- 范围：仅 `docs/wp9/**`；无迁移、无运行时、无 push

| ID | 阻断 | 闭环 |
|---|---|---|
| P0-1 | revise 时 `superseded` 仍占用 `WHERE withdrawn_at IS NULL` 唯一键 | ADR-0016：WP9.3 将 document/claim/entity 索引改为 `WHERE grant_status='active'`；superseded 存在则拒绝 downgrade；G9-27、G9-28 |
| P0-2 | 最后一条 evidence 与 ADR-0009 AI 触发器冲突 | ADR-0019：不改 `require_ai_claim_supports`；新增仅手工 Claim 的 `require_manual_claim_supports`；retire 仅手工+reject/withdraw；G9-23、G9-33 |
| P0-3 | 公开 bind/retire 无 case/decision 绑定 | ADR-0015/0019：删除公开函数；`record_review_decision` 原子调用无登录 EXECUTE 的私有函数；G9-24、G9-32 |
| P1 | 声明幂等但无契约 | ADR-0014 §2.4：冻结 event_key、payload_sha256、重放与冲突；G9-29–G9-31 |
