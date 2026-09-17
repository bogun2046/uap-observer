# ADR-0021：Publication grant 同事务冻结 typed manifest

- 状态：Proposed for `G10-FROZEN-20260828-01`
- 前置：ADR-0015、ADR-0016、0019

## 背景

WP9 grant 的 `publication_payload_sha256` 只覆盖 grant身份/subject/revision，不覆盖 title、summary、category、fact status、claim/evidence或entity内容。`select_analysis_result` 又明确只支持claim/entity extraction。若Publisher在消费时取“最新valid”或当前core行，审核后内容会漂移。

## 决策

approve/revise在同一case锁事务创建grant、不可变typed manifest、manifest hash和outbox v2。Document的公开字段由reviewer在`structured_changes.publication`明确提交；source字段从同事务内部行快照。Claim在应用subject/evidence修订后捕获，Entity从active canonical行捕获。

manifest canonical hash必须与grant和outbox三方相等。Publisher只读manifest，不从latest selection推断。legacy v1 grant/event quarantine；通过新revise产生v2，不自动回填。

public identity与内部ID分离并在withdraw后保留；document identity锚定logical `core.documents.id`，从而不同document version仍使用同一公开ID/slug。

## 兼容性

WP9 operation+request_id和payload_sha256幂等不变；document publication object本来未在WP9交付，是前向新增。Claim/entity现有决定的最终事务语义不弱化。legacy withdraw仍可执行。

## 不采用

- latest valid/selection：grant后漂移且summary/classification selection尚未授权。
- 把internal UUID直接公开：违反WP1独立公开ID。
- 仅在Outbox放正文：Outbox不是typed长期授权快照且扩大敏感面。
- 自动从legacy SQLite category/fact status回填：属于WP11且可能误认审核。
