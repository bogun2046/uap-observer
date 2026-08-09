# GitHub Pages 部署验收

## 手动触发

1. 将本地仓库推送到 GitHub，并确认默认分支包含 `.github/workflows/daily-uap.yml`。
2. 在仓库 `Settings → Pages` 中将构建来源设为 **GitHub Actions**。
3. 在 `Actions → Daily UAP Observer` 选择 **Run workflow**。
4. 如需 AI 分析，在 Actions secrets 添加 `DEEPSEEK_API_KEY` 或 `OPENAI_API_KEY`；模型和 reasoning effort 可通过 repository variables 覆盖。选择 DeepSeek 时，工作流会在批量分析前运行一次 `/models` 健康检查。

## 工作流门禁

工作流会先安装开发依赖并运行完整 pytest。采集和生成页面只有在测试通过后才会继续。选择 DeepSeek 时，健康检查会验证 Key、API 连接和配置模型；401/403 或其他致命的提供商访问错误会以非零退出码立即终止本次运行，避免继续批量调用或发布不完整结果。生成完成后会检查 Markdown 页面、构建后的 `index.html`/`search.html` 和合法的 `search.json`；检查失败时不会上传 Pages artifact。普通的单个来源失败会记录在 Actions 摘要中，但不会阻止其他来源继续处理；若数据库 checkpoint 推送失败，Pages 部署仍会继续，同时在摘要中提示下次补偿。

## 验收清单

- 首页显示最近更新时间或空队列提示
- `search.md` 能加载 `search.json` 并完成关键词过滤
- 新闻分类、事件时间线、人物、机构和关系页面链接有效
- AARO 采集失败时，Actions 日志显示失败原因，其他步骤继续运行
- `source-status` 输出每个来源的最近抓取、成功时间和错误信息
