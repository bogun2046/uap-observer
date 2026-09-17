# G9 设计 R5 整改矩阵

- 冻结标准：`G9-FROZEN-20260825-05`
- 父设计 SHA：`c4b6c52f4265ae75d9cfd3c092fc60d5f7688a42`
- 范围：仅 `docs/wp9/**`；无迁移、无运行时、无 push

| ID | 阻断 | 闭环 |
|---|---|---|
| P0 | BASELINE 要求 `G9_DESIGN_SHA^` = G8，但整改链使最终 HEAD 的第一父为 4677597 | 引入 `G9_DESIGN_ROOT_SHA` = `984da00b52b39bb4f22c365a8792c3d607724733`（`ROOT^` = G8）。`G9_DESIGN_SHA` 为 ROOT 的第一父链后代。G8..SHA 无 merge；`git diff G8..SHA` 仅 `docs/wp9/**`。WP9.1 从最终 SHA 开始。R4 技术契约不变。 |
