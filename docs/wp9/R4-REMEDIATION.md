# G9 设计 R4 整改矩阵

- 冻结标准：`G9-FROZEN-20260825-04`
- 父设计 SHA：`4677597404241264c0d11b2346970c4521dfb4c0`
- 范围：仅 `docs/wp9/**`；无迁移、无运行时、无 push

| ID | 阻断 | 闭环 |
|---|---|---|
| P0 | 旧 `event_key` 嵌入 subject/assignee/entity/fingerprint 等，改这些参数会换键并绕过 `review_idempotency_payload_conflict` | ADR-0014 §2.4：`event_key` 仅为 `操作类型:{request_id}`。全部权威业务输入进入 canonical `payload_sha256`。同 operation+request_id 且摘要不同必须 `23505`。不同 operation 允许复用同一 request_id。G9-30、G9-34–G9-38 各自覆盖“只改 reason/attribution”和“改旧键业务参数”。R3 并发 revise 串行双成功不变。 |
