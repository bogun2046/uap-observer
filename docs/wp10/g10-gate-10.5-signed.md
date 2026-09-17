# G10-GATE-10.5 正式签署记录

本文件是门禁签署记录，不是独立审核报告，也不是候选 SHA 的自动冒充。

```
G10-GATE-10.5: SIGNED
Signed SHA: 4e15bdd8cdb92d4406cc46b38f8cef92320a1881
Authorized by: Bogun Dau
Date: 2026-09-01 (Asia/Shanghai)
```

## 授权

项目负责人在 2026-09-01 发出与 G10-GATE-10.4 同级的正式 pass-and-sign 授权：点名候选 SHA、独立审核报告路径、独立审核结论 PASS，并要求核验 11 项后由有权签署者正式记录签署。

## 签署对象

- 工作树：`/private/tmp/uap-wp10.5-impl`
- 分支：`codex/wp10.5`（无 upstream）
- 候选 / 签署 SHA：`4e15bdd8cdb92d4406cc46b38f8cef92320a1881`
- 父 SHA：`1ea9ae046b2f636c17574dff805044b9f2382948`
- 已签上一门禁：`G10-GATE-10.4` / `19ee4f436179740c002f05f597e456d42b1a17db`
- 第一父链：`10773f1` → `ec06ab8` → `19932ae` → `15b85ed` → `19ee4f4` → `b5ad271` → `1ea9ae0` → `4e15bdd`

## 独立审核

- 报告：`/tmp/uap-wp10.5-codex-review.md`
- 报告 SHA256：`c6b385086a23d0d304e220b797b98e5e6e810237900f0129100d676358b63a70`
- 结论：PASS（第三轮最终；无 P0/P1/P2 阻断）
- 报告自身声明：独立审核结论不是门禁签署

## 核验清单（签署前独立复核）

1. 候选分支为 `codex/wp10.5`。通过。
2. 工作树干净，无 upstream。通过。
3. HEAD 为 `4e15bdd8cdb92d4406cc46b38f8cef92320a1881`。通过。
4. WP10.5 两轮完整 runtime probe 均通过。通过。独立证据 `/tmp/uap-wp10.5-review-r3/runtime-1.json` 与 `runtime-2.json` 均为 `status=passed`，各 90 条 checks、66 个 matrix IDs。
5. G10-21–G10-24 均通过。通过。两轮均为 G10-21=12、G10-22=15、G10-23=7、G10-24=12，failed=0。inactive principal 实测 `[403, "api_principal_not_provisioned"]`。
6. W01–W11 共 66 个实例均有真实 before/after。通过。两轮 matrix `66/66 passed`，unique IDs=`G10-W01-S`…`G10-W11-T`，missing before/after=0。
7. W04 两个并发请求均 replay，且 grant/manifest/outbox 零增量。通过。两轮均为 thread0 revision 3、thread1 revision 2；两个 replay HTTP 200；`resource_id` 与原响应相同；grant/manifest/outbox before=after。
8. 全量测试、定向测试、validators、migration、Ruff、compileall、SHA256 全部通过。通过。独立报告：pytest 295、WP10.5 定向 24、ruff check/format、compileall、validate_wp9 与 validate_wp10_1..5、migration `0023→0024→0023→0024` on PostgreSQL 16.14、SHA256SUMS 38 OK（签署前在候选树复跑 SHA256SUMS 仍 38 OK）。
9. 没有临时数据库、sidecar 或私钥残留。通过。`pg_database` 仅 `postgres`/`template0`/`template1`/`uap_platform`/`uap_wp10_gate`/`uap_wp10_public_gate`；无 `uap_wp10_5_%`；无 `uap-wp10-5-*` 容器；HEAD 无私钥 PEM，`private_exponent` 仅为运行时生成。签署前删除了工作树外遗留 `/tmp/wp10_5_rsa_constants.py`。允许的实施 helper 仍在 git 外 `/tmp/uap-wp10.5-sidecar/`，未纳入本签署提交。
10. 主工作树未变化。通过。`/Users/shonchun/Documents/UAP平台` 仍为 `codex/entity-normalization` / `4f09b0aad0b13c94b6435e4205fa02c046de46e5`；tracked index `0279ee036f8bf5893d446d542d602f22f1b6c0e320478e0b84814e5df68d5ec1`；tracked diff `e651e4f4816ddc9cf725db41fe2c5af88d36a2229bfddbffa7647c89215b9b4c`；untracked paths `9f20514f26da889bab489adab9474da3c285e08861ca7ff3fb5346f691da0b67`；预存在脏集未触碰。
11. WP10.6 尚未开始。通过。HEAD 无 wp10.6 路径；无 `/private/tmp` WP10.6 工作树。本签署不授权启动 WP10.6。

## 停止线

- 已签署 `G10-GATE-10.5`。
- 不进入 WP10.6，除非项目负责人另发同级正式授权并点名本 Signed SHA。
- 不 push、不创建 PR、不启动 CI。
- 不 amend / rebase / merge / squash / force-push。
