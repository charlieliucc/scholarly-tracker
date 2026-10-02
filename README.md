# Scholarly Tracker

一个完全由 GitHub Actions 驱动、发布在 GitHub Pages 上的期刊追踪系统。它每天从专用 Gmail 的收件箱和垃圾邮件读取期刊提醒，只处理北京时间昨日 00:00 至今日 00:00 的邮件，随后按可配置的标签权重生成：

- 今日推荐：本次日更发现且达到最低得分的论文，展示完整卡片；
- 其他新论文：本次日更发现但未进入推荐区的论文，首页保留期刊、日期、标题、作者、DOI 和分数；
- 历史记录：按日查看最近 7 天生成的日更批次，更早内容可在邮箱中回溯；
- 运行状态：邮箱目录、邮件解析器、网页摘要补全和收录数量的运行记录。

当前解析器覆盖 Elsevier、SAGE、Wiley、Taylor & Francis、Nature、APA PsycAlert 和 Cambridge Core 的期刊提醒模板。SAGE 纯文本提醒支持 `Article` 和 `Manuscript`；Cambridge 按文章 DOI 提取条目并排除封面与前后附页；APA 保留以分号分隔的“姓, 名”作者格式。收录批次按邮件接收时间确定，条目另行保留实际出版日期。

Wiley 支持 `Table of Contents Alert`、`New Articles Alert` 和 `Early View Alert`。按独立文章容器读取标题、作者、首次出版日期、卷期、页码或文章编号，排除目录入口和 `Issue Information`。开放获取标记只属于所在文章；`Free Access`、`Free to Read` 不直接视为开放获取。邮件未提供摘要或 DOI 时留空，再由现有元数据补全流程处理。

Nature 收录全部文章栏目，包括 This week、News in Focus、Books & Arts、Opinion、Work、Research 和更正／撤稿条目，排除 Research Highlights 汇总入口、Spotlight 广告和 Collections 合集入口。按文章卡片分别读取标题、作者和摘要片段，保留 `section` 栏目；作者列表末尾的 `et al.` 以 `authors_truncated` 标记，不作为姓名。期刊统一为 `Nature`，邮件没有明确的文章出版日期时留空，不用邮件日期或摘要中提及的日期代替。

上述 Wiley／Nature 规则已用 8 封 EML 样本验证；`tests/fixtures/` 仅保存脱敏 HTML，移除了邮件头、收件人地址和真实追踪标识。

## 启用 GitHub Pages

1. 把此目录推送到 GitHub 仓库的 `main` 分支。
2. 打开仓库 **Settings → Pages**。
3. 在 **Build and deployment → Source** 中选择 **GitHub Actions**。
4. 打开 **Actions → Update scholarly tracker → Run workflow**，执行首次更新。

部署完成后，站点地址会显示在该次 Actions 运行的 `deploy` 任务中。GitHub 官方也说明，自定义 Pages 工作流需要 `pages: write`、`id-token: write` 权限，并通过 Pages artifact 部署；本项目已经配置好这些内容：[Using custom workflows with GitHub Pages](https://docs.github.com/en/pages/getting-started-with-github-pages/using-custom-workflows-with-github-pages)。

## 自动运行与数据保留

- 每天北京时间 00:00 自动读取邮件并部署（GitHub Actions 使用 UTC 16:00 cron）。
- 邮件按 Gmail 内部接收时间过滤；邮件正文中的出版日期只有能可靠确认时才写入。
- 每月 1 日把最新 `docs/data/` 提交回默认分支。
- 每次运行会先从已部署站点恢复上次论文和历史索引，因此两次月度快照之间仍能连续保留最近 7 天的数据。
- `history.json` 只保存最近 7 个日更日期到论文 ID 的轻量索引；`papers.json` 同步删除窗口外的论文元数据。历史页面不保存每日完整邮件原文。
- 手动运行时，可勾选 `persist_data`，立即把该次数据也提交到仓库。
- GitHub 会在公共仓库 60 天没有仓库活动后停用定时工作流；月度数据提交用于持续产生仓库活动：[GitHub scheduled workflow policy](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/disable-and-enable-workflows)。

## 邮箱和筛选配置

编辑 [`config/journals.json`](config/journals.json)：

- `mail`：配置 Gmail IMAP 主机、端口和单次读取上限。账号与应用专用密码必须放在 Actions Secrets：`GMAIL_USERNAME`、`GMAIL_APP_PASSWORD`。
- `history_retention_days`：历史记录和论文元数据的滚动保留天数，当前为 7 天。
- `window.timezone`：计算昨日窗口的 IANA 时区，默认 `Asia/Shanghai`。
- `ranking.keywords`：配置用于筛选和展示的标签；正数提高推荐得分，负数降低得分。
- 同一 `group` 的标签在每篇论文中只计分一次；优先取标题命中，避免宽泛 AI 表述重复加分。
- `title_multiplier`：标签出现在标题中的权重倍率。
- `recommendations.minimum_score`：进入今日推荐的最低分。
- `recommendations.limit`：今日推荐最多显示的篇数。
- `recommendations.json` 的 `articles` 保存完整推荐卡片数据，`other_articles` 保存同批次未推荐论文数据。
- `doi_page`：设置只对高匹配文章读取论文页 `<head>` 摘要的上限和最大 HTML 字节数；不读取正文或 PDF。
- `metadata_fallback`：对本次邮件条目按相关度排序，在限额内使用 Crossref、OpenAlex 补齐作者、DOI 和摘要；按 DOI 或高置信度标题及期刊匹配，不修改邮件原始链接。

## 本地运行

无需安装第三方 Python 包：

```bash
python3 scripts/update.py --config config/journals.json --output docs/data
python3 -m unittest discover -s tests -v
python3 -m http.server 8000 --directory docs
```

然后访问 `http://localhost:8000/`。在无网络环境下可运行 `python3 scripts/update.py --offline`，它会保留已有论文，并把状态标记为使用旧数据。

## 数据策略

- 摘要按 `邮件文章块 → Crossref → OpenAlex → 可信论文页 <head>` 回退；只有高匹配文章才访问网页，元数据 API 可补齐其他文章。只用更长的摘要替换缺失或截断摘要。
- DOI 页面只解析 `<head>` 中的公开摘要元数据，不读取正文、全文或 PDF；访问失败时保留原摘要。
- 邮件提醒按可信出版商模板解析；营销、仿冒和无法可靠解析的邮件会跳过并在状态页计数。
- 单个邮箱目录、邮件模板或论文页失败不会清空历史数据，错误会显示在运行状态页。
- 不保存原始邮件、正文、Message-ID 或账号信息；页面只发布公开论文元数据。
- 页面只发布公开论文元数据，不存储密钥；如仓库或页面中存在敏感数据，请勿启用公开 Pages。
