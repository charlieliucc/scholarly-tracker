# Scholarly Tracker

一个完全由 GitHub Actions 驱动、发布在 GitHub Pages 上的期刊追踪系统。它每天读取 RSS：有逐条精确日期的来源只处理上海时区“昨天 00:00（含）至今天 00:00（不含）”的条目；没有逐条更新日期的 ScienceDirect 则处理上次成功抓取后首次出现的 GUID。随后在需要时通过 Crossref 补全 DOI 和书目信息，再按可配置的关键词权重生成：

- 今日推荐：本次日更发现且达到最低得分的论文，展示完整卡片；
- 其他新论文：本次日更发现但未进入推荐区的论文，首页保留期刊、日期、标题、作者、DOI 和分数；
- 历史记录：按日查看已经生成过的日更批次，避免错过前一天的推送；
- 全部论文：可检索、筛选和排序的累积档案；
- 运行状态：每个 RSS 源、Crossref 补全和收录数量的运行记录。

当前预置 25 个来源，覆盖 Elsevier、SAGE、Wiley、Taylor & Francis、Cambridge University Press、Oxford University Press 和 Springer Nature。

## 启用 GitHub Pages

1. 把此目录推送到 GitHub 仓库的 `main` 分支。
2. 打开仓库 **Settings → Pages**。
3. 在 **Build and deployment → Source** 中选择 **GitHub Actions**。
4. 打开 **Actions → Update scholarly tracker → Run workflow**，执行首次更新。

部署完成后，站点地址会显示在该次 Actions 运行的 `deploy` 任务中。GitHub 官方也说明，自定义 Pages 工作流需要 `pages: write`、`id-token: write` 权限，并通过 Pages artifact 部署；本项目已经配置好这些内容：[Using custom workflows with GitHub Pages](https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages)。

## 自动运行与数据保留

- 每天上海时间 06:17 自动抓取并部署；避开整点可降低 Actions 高负载时的排队概率。
- 默认情况下，Feed 必须给出精确到“日”或更细的条目时间才能进入处理窗口；只有年份或月份的条目会跳过，不会猜测日期。
- ScienceDirect 使用 GUID 增量发现：首次运行只建立当前条目基线，之后仅处理从未见过的 GUID。月份级卷期日期按原精度展示，不会伪装成每月 1 日，也不会把频道级 `lastBuildDate` 套用到全部文章。
- 每月 1 日把最新 `docs/data/` 提交回默认分支。
- 每次运行会先从已部署站点恢复上次论文、状态和 Feed 身份索引，因此两次月度快照之间仍能保留每日首次发现时间和累积论文。
- `history.json` 只保存日更日期到论文 ID 的轻量索引；历史页面从累计论文档案还原内容，不保存每日完整 RSS 原文。
- 手动运行时，可勾选 `persist_data`，立即把该次数据也提交到仓库。
- GitHub 会在公共仓库 60 天没有仓库活动后停用定时工作流；月度数据提交用于持续产生仓库活动：[GitHub scheduled workflow policy](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/disable-and-enable-workflows)。

## 自定义期刊和筛选权重

编辑 [`config/journals.json`](config/journals.json)：

- `journals`：增加、禁用或修改 RSS feed；解析器兼容 RSS 2.0、RSS 1.0/RDF 和常见 Atom 字段。
- `journals[].discovery_mode`：默认 `publication_date`；对于没有逐条精确更新时间的 ScienceDirect Feed，可设为 `guid_diff`，按 GUID 首次出现判断新增。
- `window.timezone`：计算昨日窗口的 IANA 时区，默认 `Asia/Shanghai`。
- `ranking.keywords`：正数提高推荐得分，负数降低得分。
- `title_multiplier`：关键词出现在标题中的权重倍率。
- `recommendations.minimum_score`：进入今日推荐的最低分。
- `recommendations.limit`：今日推荐最多显示的篇数。
- `recommendations.json` 的 `articles` 保存完整推荐卡片数据，`other_articles` 保存同批次未推荐论文数据。
- `crossref.max_lookups_per_run`：单次运行最多发出的 Crossref 查询数。
- `crossref.doi_page_enabled`：Crossref 摘要缺失或被截断时，是否读取 DOI 页面 `<head>` 中的公开摘要元数据；不读取正文或 PDF。
- `crossref.max_doi_page_lookups_per_run`：单次运行最多读取的 DOI 页面数量。
- `crossref.doi_page_max_bytes`：单个 DOI 页面最多读取的 HTML 字节数。

建议把 `crossref.contact_email` 改为维护者邮箱。Crossref 推荐在自动查询中提供邮箱和明确的 `User-Agent`，以进入 polite pool；配置后脚本会同时设置两者：[Crossref REST API etiquette](https://api.crossref.org/swagger-ui/index.html)。邮箱只随 Crossref 请求发送，不会写入页面数据。

## 本地运行

无需安装第三方 Python 包：

```bash
python3 scripts/update.py --config config/journals.json --output docs/data
python3 -m unittest discover -s tests -v
python3 -m http.server 8000 --directory docs
```

然后访问 `http://localhost:8000/`。在无网络环境下可运行 `python3 scripts/update.py --offline`，它会保留已有论文，并把状态标记为使用旧数据。

## 数据策略

- 摘要按 `RSS → Crossref → DOI 页面公开摘要` 回退；只有当前摘要为空或以省略号结尾，且新摘要更长时才替换。
- DOI 页面只解析 `<head>` 中的公开摘要元数据，不读取正文、全文或 PDF；访问失败时保留原摘要。
- RSS 记录优先保留；Crossref 只补全缺失字段，摘要回退遵守更长才替换的规则。
- 无 DOI 时，Crossref 标题候选必须达到配置的相似度门槛才会合并。
- 单个 feed 或 Crossref 暂时失败不会清空历史数据，错误会显示在运行状态页。
- `feed-state.json` 只保存最小化的条目 ID、首次/最后见到时间和内容指纹；不会保存每日完整 RSS 快照。
- 某些出版商可能拒绝 GitHub Actions 的服务器 IP；可为该期刊配置 `crossref_fallback_issn`，仅在 RSS 失败时按相同时间窗口读取 Crossref 更新记录。
- 页面只发布公开论文元数据，不存储密钥；如仓库或页面中存在敏感数据，请勿启用公开 Pages。
