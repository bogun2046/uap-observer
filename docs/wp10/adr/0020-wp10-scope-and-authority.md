# ADR-0020：WP10 范围与发布写权威

- 状态：Proposed for `G10-FROZEN-20260828-01`
- 前置：G9-GATE-9.6、ADR-0006、ADR-0014–0019

## 背景

WP8/WP9 明确把 public projection、公开 API、搜索留给 WP10。实际 0019 后仍无业务 HTTP；WP9 只创建 grant/outbox。0003 的早期宽授权还让 Publisher 可裸写 public，不能证明所有公开行都经授权投影。

## 决策

WP10 交付 document/claim/entity manifest→Publisher→public/search→HTTP，并把 WP9 受控函数映射为 Admin API。relation 不进入。

Publisher 公开写入只允许 owner `SECURITY DEFINER` 的 `ops.apply_publication_event`。`uap_publisher` 撤销 public 裸 DML，不能独立 ack publication event。`uap_api` 撤销 bottom core/ops裸 DML，继续只调用 audit包装；bottom merge/reverse保持关闭。

Public/Admin API 分进程、分数据库凭据。Public API只读 public；Admin API不持有Publisher凭据。grant没有独立写API，只是review decision私有副作用。

## 后果

投影和ack可形成数据库可证明的原子边界；HTTP代码无法绕过DB授权。代价是必须追加权限migration和专用Publisher函数，并更新WP4 generic ack的生产使用方式。

## 不采用

- 让Publisher Python裸写public：无法强制grant/hash/ack顺序。
- API直接查core并过滤：容易泄漏未审内容。
- 合并Public/Admin数据库pool：扩大凭据误用半径。
- 在WP10顺带 relation：缺少输入Schema和全链前置。
