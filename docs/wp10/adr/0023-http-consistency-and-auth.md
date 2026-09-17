# ADR-0023：HTTP OIDC、事务、游标与一致性

- 状态：Proposed for `G10-FROZEN-20260828-01`
- 前置：WP1 OpenAPI草案、ADR-0014、ADR-0020

## 背景

WP1 OpenAPI是草案，case/entity/locator/claim枚举已与数据库不同；其Idempotency-Key允许任意字符串，而WP9 `uap.request_id`要求UUID。当前仓库只有WP2 `/healthz`，没有业务HTTP/OIDC。

## 决策

WP10以0019数据库枚举为权威，不建平行状态机。写请求的规范UUID Idempotency-Key就是request_id；handler在一个事务SET LOCAL principal/request、调用恰好一个公共写函数、读响应并commit。失败全rollback。数据库operation+request_id与canonical payload hash继续唯一幂等权威。

Admin只接受OIDC bearer；严格校验签名和标准claims，并将issuer/sub解析为已存在active person。token role不能替代role_bindings。WP10不接受cookie认证。

Public list/search使用HMAC签名keyset cursor；单页read-only repeatable-read，跨页为当前projection语义而非历史snapshot。公开不可见和不存在统一404。Public API只持有public reader连接。

## API兼容性

保留WP1的document/claim/entity/search核心路径，修正DTO枚举；EntityDetail删除会误导的relations字段。Admin扩展WP9已交付的全部包装函数。所有写成功统一200，异步projection状态在响应中单独报告。

## 不采用

- 接受旧枚举并静默映射；
- 随机生成缺失Idempotency-Key；
- principal作为函数参数；
- offset pagination；
- core查询兜底public 404；
- cookie session但无CSRF契约。
