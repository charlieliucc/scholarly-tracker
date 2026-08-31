# Scholarly Tracker

一个完全由 GitHub Actions 驱动、发布在 GitHub Pages 上的期刊追踪系统。它每天读取 RSS，但只处理上海时区“昨天 00:00（含）至今天 00:00（不含）”更新或上线的条目；随后在需要时通过 Crossref 补全 DOI 和书目信息，再按可配置的关键词权重生成：

- 今日推荐：昨日窗口内更新且达到最低得分的论文；
- 全部论文：可检索、筛选和排序的累积档案；
- 运行状态：每个 RSS 源、Crossref 补全和收录数量的运行记录。

当前预置三个来源：Journal of Second Language Writing（Elsevier）、Language Teaching Research（SAGE）、Assessment & Evaluation in Higher Education（Taylor & Francis）。

## 启用 GitHub Pages

1. 把此目录推送到 GitHub 仓库的 `main` 分支。
2. 打开仓库 **Settings → Pages**。
3. 在 **Build and deployment → Source** 中选择 **GitHub Actions**。
4. 打开 **Actions → Update scholarly tracker → Run workflow**，执行首次更新。

部署完成后，站点地址会显示在该次 Actions 运行的 `deploy` 任务中。GitHub 官方也说明，自定义 Pages 工作流需要 `pages: write`、`id-token: write` 权限，并通过 Pages artifact 部署；本项目已经配置好这些内容：[Using custom workflows with GitHub Pages](https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages)。

## 自动运行与数据保留

- 每天上海时间 06:17 自动抓取并部署；避开整点可降低 Actions 高负载时的排队概率。
- Feed 必须给出精确到“日”或更细的条目时间才能进入处理窗口；只有年份或月份的条目会跳过，不会猜测日期。
- 每月 1 日把最新 `docs/data/` 提交回默认分支。
- 每次运行会先从已部署站点恢复上次数据，因此两次月度快照之间仍能保留每日首次发现时间和累积论文。
- 手动运行时，可勾选 `persist_data`，立即把该次数据也提交到仓库。
- GitHub 会在公共仓库 60 天没有仓库活动后停用定时工作流；月度数据提交用于持续产生仓库活动：[GitHub scheduled workflow policy](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/disable-and-enable-workflows)。

## 自定义期刊和筛选权重

编辑 [`config/journals.json`](config/journals.json)：

- `journals`：增加、禁用或修改 RSS feed；解析器兼容 RSS 2.0、RSS 1.0/RDF 和常见 Atom 字段。
- `window.timezone`：计算昨日窗口的 IANA 时区，默认 `Asia/Shanghai`。
- `ranking.keywords`：正数提高推荐得分，负数降低得分。
- `title_multiplier`：关键词出现在标题中的权重倍率。
- `recommendations.minimum_score`：进入今日推荐的最低分。
- `recommendations.limit`：今日推荐最多显示的篇数。
- `crossref.max_lookups_per_run`：单次运行最多发出的 Crossref 查询数。

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

- RSS 记录优先保留；Crossref 只补全缺失字段。
- 无 DOI 时，Crossref 标题候选必须达到配置的相似度门槛才会合并。
- 单个 feed 或 Crossref 暂时失败不会清空历史数据，错误会显示在运行状态页。
- 页面只发布公开论文元数据，不存储密钥；如仓库或页面中存在敏感数据，请勿启用公开 Pages。
