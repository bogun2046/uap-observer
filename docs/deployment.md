# GitHub Pages 部署验收

## 手动触发

1. 将本地仓库推送到 GitHub，并确认默认分支包含 `.github/workflows/daily-uap.yml`。
2. 在仓库 `Settings → Pages` 中将构建来源设为 **GitHub Actions**。
3. 在 `Actions → Daily UAP Observer` 选择 **Run workflow**。
4. 如需 AI 分析，先在 Actions secrets 添加 `OPENAI_API_KEY`；模型和 reasoning effort 可通过 repository variables 覆盖。

## 工作流门禁

工作流会先安装开发依赖并运行完整 pytest。采集和生成页面只有在测试通过后才会继续。生成完成后会检查 Markdown 页面、构建后的 `index.html`/`search.html` 和合法的 `search.json`；检查失败时不会上传 Pages artifact。

## 验收清单

- 首页显示最近更新时间或空队列提示
- `search.md` 能加载 `search.json` 并完成关键词过滤
- 新闻分类、事件时间线、人物、机构和关系页面链接有效
- AARO 采集失败时，Actions 日志显示失败原因，其他步骤继续运行
- `source-status` 输出每个来源的最近抓取、成功时间和错误信息
