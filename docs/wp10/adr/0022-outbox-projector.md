# ADR-0022：Publisher 原子投影、失败与重建

- 状态：Proposed for `G10-FROZEN-20260828-01`
- 前置：ADR-0004、ADR-0006、ADR-0016、ADR-0021

## 背景

WP4 generic `claim_outbox`与`ack_outbox`允许先提交claim、另事务写public、再ack；进程崩溃会重放，若代码非幂等可能重复。更严重的是单独ack可造成event已发布而projection未写。

## 决策

专用claim只领v2 publication event。`ops.apply_publication_event`在有效lease下锁event，验证event/grant/manifest/hash，幂等写当前projection，并在同一事务ack。Publisher没有publication事件的独立ack能力。

deterministic坏数据进入terminal且不标published；短暂错误指数退避，12次后terminal。data_operator只能经带review request_id的`audit.requeue_publication_event`重放。stale revision/event是验证后的no-op success，绝不降级当前projection。

Document withdraw隐藏dependent claims/search；republish从active v2 manifests重建。Claim始终与evidence原子；entity/claim事件共同reconcile document_entities。search与document/claim变化同事务。

完整rebuild只在维护窗由owner/migrator执行，读active v2 manifests，staging校验后原子替换；不ack pending event。

## 后果

系统获得at-least-once领取与exactly-once-visible-effect。terminal积压可审计且不会忙循环。代价是projection函数较复杂，必须按subject阶段拆分和做大量failure injection。

## 不采用

- Python事务外多次裸DML+ack；
- terminal也设置published_at；
- 永久无限重试坏hash；
- public row作为唯一identity/history；
- relation event no-op ack。
