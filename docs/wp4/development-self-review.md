# WP4 开发自检报告

- 实现编号：`WP4-IMPL-20260814-01`
- 冻结标准：`G4-FROZEN-20260814-01`
- 状态：开发自检进行中，待独立验收；不构成 G4 通过结论

## 自检范围

- `0005_durable_jobs` 从 G3 head 线性升级成功。
- 幂等入队、原子领取、token 租约、失败分类、死信和 Outbox 函数已建立。
- 普通 Worker 与 Publisher job-type 白名单、数据库函数权限和直接写表限制已建立。
- `platform/tools/wp4_runtime_probe.py` 覆盖真实角色连接、单领取、重试分类、Outbox 幂等和确认。

## 开发自测结果

- 空环境按 `docker compose up --build --detach --wait` 启动成功；数据库迁移至 `0005_durable_jobs`，对象存储五个 bucket ready。
- WP3 兼容探针通过：49 张业务表、108 个外键、孤儿 0，迁移器为 `NOLOGIN/NOINHERIT`，既有对象与权限语义保持通过。
- WP4 运行态探针通过：幂等入队、单次领取、Worker/Publisher 边界、重试分类、租约过期恢复、死信重入队、Outbox 去重与确认，以及同事务回滚。
- R1 修复反例通过：过期 Outbox token 的 ack/failure/release 均返回 40001，Worker 重入队发布死信返回 42501，租约恢复后 `recovery_reason` 保留为 `lease_expired`。
- R2 时钟反例通过：事务开始于租约到期前、实际执行跨越到期点时，`finish_job` 与 `ack_outbox` 均返回 40001；租约领取、截止时间计算及 Outbox 释放/失败路径统一使用实际时钟。
- 迁移链通过：`0001 -> 0002 -> 0003 -> 0004 -> 0005` 顺序升级、重复升级、降级到 WP2 后回升 head 均成功；失败迁移后 migrator 收口验证通过。
- 容器质量检查通过：Ruff、mypy、28/28 pytest，覆盖率 91.86%，WP2 23/23、WP3 9/9、WP4 6/6。
- 证据清单由 `platform/tools/build_wp4_evidence.py` 生成；最终提交后须由独立验收记录绑定 commit、required CI run 和清单 SHA-256。
- required CI 的 Trivy 扫描发现 SeaweedFS 4.41 上游 `weed` 二进制包含 7 个 High 级 Go 漏洞；对象存储镜像现以固定 Go 1.25.13 和 SeaweedFS 4.41 提交 `de34a1a87c02893507f961cda9574172ee5064e9` 重建 `weed`，并继续复用固定的官方 SeaweedFS 运行时层。整改后的最终镜像必须由 required security job 重新证明 High/Critical 为 0。

实现人员不填写最终独立验收结论；本报告只记录开发自测证据。
