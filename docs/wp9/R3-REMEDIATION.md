# G9 设计 R3 整改矩阵

- 冻结标准：`G9-FROZEN-20260825-03`
- 父设计 SHA：`60c80dedb75097b2237ed9c82ac0f1462cb556d9`
- 范围：仅 `docs/wp9/**`；无迁移、无运行时、无 push

| ID | 阻断 | 闭环 |
|---|---|---|
| P0 | G9-28 要求 40001/23505，与 READ COMMITTED + case 行锁的串行双成功不符 | ADR-0016 明确选择串行双成功：锁后重读 active grant 与 max(revision_no)；不同 request_id 的并发 revise 都可提交；revision 严格递增；最终一行 active；无丢失更新。G9-28 改写。不采用 expected_active_grant_id。 |
| P1 | 幂等只测了 record_review_decision | ADR-0014 把每个公开写函数映射到 G9-29/30、G9-34–G9-38；按阶段纳入门禁。 |
