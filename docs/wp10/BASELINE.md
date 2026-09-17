# WP10 设计基线

- 核验日期：2026-08-28（Asia/Shanghai）
- 仓库：`bogun2046/uap-observer`
- 唯一产品起点：`8a956f32f6462a37043ac0ab6fffec46a5d830db`
- 产品 tree：`54ddb6c84d7f989a13c49bfc9996e3fd12ae447a`
- 指定 origin/main merge：`81645073ad63cf920298eddb0dc8bb8b773de300`
- 建议冻结号：`G10-FROZEN-20260828-01`
- 分支：`codex/wp10-design`

## 1. 开工前核验记录

获取 `origin/main` 后的只读核验结果：

```text
product  8a956f32f6462a37043ac0ab6fffec46a5d830db
tree     54ddb6c84d7f989a13c49bfc9996e3fd12ae447a
parent   86157af50bc425fc66d6366883566cdd8ae9920f

merge    81645073ad63cf920298eddb0dc8bb8b773de300
tree     54ddb6c84d7f989a13c49bfc9996e3fd12ae447a
parents  d0f8f66d24691f3fc55cb36a53d50734f4da07ae
         8a956f32f6462a37043ac0ab6fffec46a5d830db
```

结论：指定 merge 的第二父是产品 SHA；merge 与产品 SHA tree 完全相同；`git diff --stat 8a956f3 8164507` 为空；产品 SHA 是 merge 的祖先。设计分支明确从产品 SHA 创建，未以 merge commit 为内容基线。

核验时主工作树位于既存 `codex/entity-normalization` 且含用户未提交改动；为避免携带或覆盖这些改动，本分支在隔离 worktree 中从产品 SHA 创建。核验时本地 `main` 引用仍是旧值 `d0f8f66d...`，因此权威比较对象是按任务指定并已 fetch 的 `origin/main=8164507...`；未移动本地 `main`。

## 2. 设计提交身份

本目录第一次 docs-only 普通提交记为：

```text
WP10_DESIGN_ROOT_SHA = 提交形成后解析的首个 docs/wp10-only 提交
G10_DESIGN_SHA       = 独立设计冻结最终签署的普通追加 HEAD
```

不得把未知的当前 HEAD 回写到本文，避免 Git 自引用。若独立审核要求整改，必须在 `WP10_DESIGN_ROOT_SHA` 后普通追加；禁止 amend、rebase、force-push 或重建设计根。

最终设计链必须同时满足：

1. `WP10_DESIGN_ROOT_SHA^ = 8a956f32f6462a37043ac0ab6fffec46a5d830db`；
2. `G10_DESIGN_SHA` 是 design root 的第一父链后代；
3. `8a956f3..G10_DESIGN_SHA` 无 merge；
4. 相对产品 SHA 只新增或修改 `docs/wp10/**`；
5. `SHA256SUMS` 对除自身外全部冻结文件通过；
6. 设计链不得包含 migration、Python、Makefile、Docker/compose、lock、workflow 或 WP1–WP9 文档改动。

## 3. WP10.1 合法起点

WP10.1 的唯一合法起点是项目负责人启动口令明确给出的、已经独立签署的 `G10_DESIGN_SHA`，而不是本候选分支名、产品 SHA、merge SHA 或未签署 design root。

启动口令至少包含：冻结号、完整 SHA、WP10.1 范围、G10-GATE-10.1 用例和允许修改的文件范围。未满足时 STOP。

WP10.2–WP10.6 只能从上一阶段独立签署 SHA 线性继续。技术验收通过不自动授权下一阶段。

## 4. 本轮内容边界

本设计候选只新增 `docs/wp10/**`。它没有实施 WP10.1，没有更改既有 schema/角色/函数，没有解除 G8-16C，也没有启动 WP11。
