"use strict";

const $ = (selector) => document.querySelector(selector);

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function formatDate(value, includeTime = false) {
  if (!value) return "未知";
  const date = new Date(value.length === 10 ? `${value}T00:00:00Z` : value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", includeTime
    ? { year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false }
    : { year: "numeric", month: "short", day: "numeric" }
  ).format(date);
}

function formatPublicationDate(article) {
  const precision = article.date_precision;
  const value = String(article.feed_timestamp || article.published || "");
  if (precision === "month" && /^\d{4}-\d{2}$/.test(value)) {
    const [year, month] = value.split("-");
    return `${year}年${Number(month)}月`;
  }
  if (precision === "year" && /^\d{4}$/.test(value)) return `${value}年`;
  return formatDate(article.published || value);
}

async function fetchJson(path) {
  const response = await fetch(path, { cache: "no-store" });
  if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
  return response.json();
}

// First successful local run, recorded in the initial repository snapshot.
const SITE_STARTED_ON = "2026-08-31";

function updateFooterRuntime(now = new Date()) {
  const parts = new Intl.DateTimeFormat("en", {
    timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit",
  }).formatToParts(now);
  const values = Object.fromEntries(parts.map(({ type, value }) => [type, value]));
  const today = Date.UTC(Number(values.year), Number(values.month) - 1, Number(values.day));
  const elapsedDays = Math.max(0, Math.floor((today - Date.parse(`${SITE_STARTED_ON}T00:00:00Z`)) / 86400000));
  $("#site-copyright").textContent = `© ${values.year === "2026" ? "2026" : `2026–${values.year}`} Charlieliucc`;
  $("#site-runtime").textContent = `自 2026-08-31 · 已运行 ${elapsedDays} 天`;
}

async function initFooter() {
  updateFooterRuntime();
  // Refresh an open page across Beijing midnight, including after device sleep.
  setInterval(updateFooterRuntime, 60000);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) updateFooterRuntime();
  });
  const count = $("#site-paper-count");
  try {
    const stats = await fetchJson("data/site-stats.json");
    const identified = stats.totals?.identified;
    const recommended = stats.totals?.recommended;
    if (![identified, recommended].every((value) => Number.isInteger(value) && value >= 0)) {
      throw new Error("Missing article counts");
    }
    count.textContent = `共识别 ${identified.toLocaleString("zh-CN")} 篇次 · 推荐 ${recommended.toLocaleString("zh-CN")} 篇次`;
    if (stats.generated_at) {
      const updated = new Intl.DateTimeFormat("zh-CN", {
        timeZone: "Asia/Shanghai", dateStyle: "medium", timeStyle: "short", hour12: false,
      }).format(new Date(stats.generated_at));
      count.title = `累计所有运行记录，包含同一天重跑。统计更新于 ${updated}（北京时间）`;
    }
  } catch {
    count.textContent = "累计统计暂不可用";
  }
}

function externalLink(href, label, className = "") {
  const link = element("a", className, label);
  link.href = href;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  return link;
}

function scoreLabel(score) {
  const value = Number(score || 0);
  return `${value > 0 ? "+" : ""}${Number.isInteger(value) ? value : value.toFixed(1)}`;
}

function hasCompleteAbstract(article) {
  const abstract = String(article.abstract || "").trim();
  return Boolean(abstract) && !abstract.endsWith("...") && !abstract.endsWith("…");
}

function doiLink(article) {
  if (!article.doi) return null;
  return externalLink(article.doi_url || `https://doi.org/${article.doi}`, `DOI ${article.doi}`, "doi-link");
}

function articleTags(article) {
  const matches = article.matched_keywords || [];
  if (!matches.length) return null;
  const tags = element("div", "tag-list");
  tags.setAttribute("aria-label", "标签");
  matches.slice(0, 6).forEach((match) => {
    const tag = element("span", Number(match.contribution) < 0 ? "negative" : "", `${match.keyword} ${scoreLabel(match.contribution)}`);
    tag.title = `命中字段：${(match.fields || []).join("、")}`;
    tags.append(tag);
  });
  return tags;
}

function articleCard(article, index) {
  const card = element("article", "paper-card");
  const rank = element("div", "paper-rank", String(index + 1).padStart(2, "0"));
  const body = element("div", "paper-body");
  const meta = element("div", "paper-meta");
  meta.append(element("span", "journal-label", article.journal || "未知期刊"));
  if (article.published || article.feed_timestamp) {
    const published = element("span", "published", formatPublicationDate(article));
    if (article.publication_text) published.title = `来源标注：${article.publication_text}`;
    meta.append(published);
  }
  const title = element("h3");
  const articleUrl = article.url || article.doi_url;
  title.append(articleUrl ? externalLink(articleUrl, article.title) : document.createTextNode(article.title));
  const rawAuthors = (article.authors || []).join(" · ") || "作者信息待补全";
  const authorText = rawAuthors.length > 260 ? `${rawAuthors.slice(0, 257).trim()}…` : rawAuthors;
  const authors = element("p", "authors", authorText);
  if (authorText !== rawAuthors) authors.title = rawAuthors;
  const actions = element("div", "paper-actions");
  const doi = doiLink(article);
  if (doi) actions.append(doi);
  const score = element("span", `score ${Number(article.score) < 0 ? "negative" : ""}`, `${scoreLabel(article.score)} 分`);
  actions.append(score);

  body.append(meta, title, authors);
  if (hasCompleteAbstract(article)) {
    const details = element("details", "abstract");
    details.append(element("summary", "", "查看摘要"), element("p", "", article.abstract));
    body.append(details);
  }
  const tags = articleTags(article);
  if (tags) body.append(tags);
  body.append(actions);
  card.append(rank, body);
  return card;
}

function updateMeta(article) {
  if (article.feed_timestamp) {
    return { label: "更新于", value: formatDate(article.feed_timestamp, true), dateTime: article.feed_timestamp };
  }
  if (article.first_seen) {
    return { label: "首次发现", value: formatDate(article.first_seen, true), dateTime: article.first_seen };
  }
  return { label: "本批次更新", value: "时间未知", dateTime: "" };
}

function todayPaperMeta(article) {
  const meta = element("div", "today-paper-meta");
  meta.append(element("span", "journal-label", article.journal || "未知期刊"));
  const update = updateMeta(article);
  const time = element("time", "update-time", `${update.label} ${update.value}`);
  if (update.dateTime) time.dateTime = update.dateTime;
  meta.append(time);
  return meta;
}

function todayPaperInfo(article) {
  const info = element("div", "today-paper-info");
  info.append(element("span", "today-authors", `作者：${(article.authors || []).join(" · ") || "作者信息待补全"}`));
  if (article.published) info.append(element("span", "today-publication", `发表：${formatPublicationDate(article)}`));
  if (article.is_open_access === true) info.append(element("span", "today-open-access", "Open access"));
  return info;
}

function todayDetails(article) {
  if (!hasCompleteAbstract(article)) return null;
  const details = element("details", "today-details");
  details.append(element("summary", "", "查看摘要"));
  const content = element("div", "today-details-content");
  content.append(element("p", "today-abstract", article.abstract));
  details.append(content);
  return details;
}

function todayArticleCard(article, index) {
  const card = element("article", "today-paper-card");
  const rank = element("div", "paper-rank", String(index + 1).padStart(2, "0"));
  const body = element("div", "today-paper-body");
  const header = element("div", "today-paper-top");
  header.append(todayPaperMeta(article));
  header.append(element("span", "recommendation-badge", `推荐 · ${scoreLabel(article.score)} 分`));
  const title = element("h3");
  const articleUrl = article.url || article.doi_url;
  title.append(articleUrl ? externalLink(articleUrl, article.title) : document.createTextNode(article.title));
  const tags = articleTags(article);
  body.append(header, title, todayPaperInfo(article));
  if (tags) body.append(tags);
  const doi = doiLink(article);
  if (doi) {
    const actions = element("div", "paper-actions");
    actions.append(doi);
    body.append(actions);
  }
  const details = todayDetails(article);
  if (details) body.append(details);
  card.append(rank, body);
  return card;
}

function todayOtherArticleRow(article) {
  const card = element("article", "other-paper-row");
  const body = element("div", "paper-body");
  const header = element("div", "other-paper-head");
  header.append(todayPaperMeta(article));
  header.append(element("span", "not-recommended", "未推荐"));
  const title = element("h3");
  const articleUrl = article.url || article.doi_url;
  title.append(articleUrl ? externalLink(articleUrl, article.title) : document.createTextNode(article.title));
  const tags = articleTags(article);
  body.append(header, title, todayPaperInfo(article));
  if (tags) body.append(tags);
  const doi = doiLink(article);
  if (doi) {
    const actions = element("div", "paper-actions");
    actions.append(doi);
    body.append(actions);
  }
  card.append(body);
  return card;
}

function errorState(message) {
  const box = element("div", "empty-state");
  box.append(element("strong", "", "数据暂时无法读取"), element("p", "", message));
  return box;
}

function emptyState(title, message) {
  const box = element("div", "empty-state");
  box.append(element("span", "empty-mark", "∅"), element("strong", "", title), element("p", "", message));
  return box;
}

function formatUpdatedAt(value) {
  if (!value || Number.isNaN(Date.parse(value))) return "未知";
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai", year: "numeric", month: "short", day: "numeric",
    hour: "2-digit", minute: "2-digit", hour12: false,
  }).format(new Date(value));
}

function updateHealth(status, contentUpdatedAt, now = new Date()) {
  const email = status.email || {};
  const counts = status.counts || {};
  const identified = counts.items_in_window ?? counts.processed_this_run ?? 0;
  const recommended = counts.recommended_today ?? 0;
  const offline = email.status === "offline";
  const failed = !offline && (email.status === "error" || ["error", "stale"].includes(status.outcome));
  const failedFolders = (email.folders || []).filter((folder) => ["error", "partial"].includes(folder.status));
  const incomplete = email.status === "partial" || failedFolders.length > 0
    || Number(email.read_errors || 0) > 0 || Number(email.parser_errors || 0) > 0;
  const pending = !status.generated_at || status.outcome === "pending";
  const configuredHours = Number(status.freshness_max_age_hours);
  const maxAgeHours = configuredHours > 0 && Number.isFinite(configuredHours) ? configuredHours : 36;
  const age = now.getTime() - Date.parse(contentUpdatedAt);
  const delayed = Number.isFinite(age) && age > maxAgeHours * 3600000;
  let label = "更新已完成";
  let state = "success";
  let description = identified === 0 ? "本次没有发现文献。"
    : recommended === 0 ? `本次识别 ${identified} 篇文献，没有文章达到推荐门槛。`
      : `本次识别 ${identified} 篇文献，生成 ${recommended} 篇推荐。`;
  let notice = "";
  if (failed) {
    label = "本次更新失败";
    state = "error";
    description = "邮件读取失败，本次没有更新文献。";
    notice = contentUpdatedAt ? `正在展示 ${formatUpdatedAt(contentUpdatedAt)} 的内容，请查看 Actions 运行记录。`
      : "尚无可用的文献内容，请查看 Actions 运行记录。";
  } else if (offline) {
    label = "暂未更新";
    state = "stale";
    description = "本次为离线运行，未读取邮件，已有内容保留。";
    notice = "正在展示上次内容，请查看运行状态。";
  } else if (pending) {
    label = "等待首次更新";
    state = "pending";
    description = "尚未发布运行记录。";
  } else if (incomplete) {
    label = "更新不完整";
    state = "partial";
    const reasons = [];
    if (email.status === "partial" || failedFolders.length || email.read_errors) reasons.push("部分邮件或目录读取失败");
    if (email.parser_errors) reasons.push(`${email.parser_errors} 封邮件解析失败`);
    description = `${reasons.join("；")}，部分文献可能未收录。`;
    notice = `${description} 请查看运行状态中的邮件名称和目录。`;
  }
  if (delayed && !failed) {
    if (!incomplete && !offline && !pending) {
      label = "尚未更新";
      state = "stale";
      description = `最近一次更新识别 ${identified} 篇文献，生成 ${recommended} 篇推荐。`;
    }
    notice += `${notice ? " " : ""}最近内容更新已超过 ${maxAgeHours} 小时，请查看 Actions 运行记录。`;
  }
  return { label, state, description, notice, failed, offline, pending, incomplete, delayed, failedFolders, identified, recommended };
}

function actionsLink(status) {
  const fallback = "https://github.com/charlieliucc/scholarly-tracker/actions/workflows/update.yml";
  const url = status.run_url;
  const currentRun = typeof url === "string" && /^https:\/\/github\.com\/charlieliucc\/scholarly-tracker\/actions\/runs\/\d+$/.test(url);
  return externalLink(currentRun ? url : fallback, currentRun ? "查看本次 Actions 日志" : "查看 Actions 运行记录");
}

function folderName(value) {
  if (value === "INBOX") return "收件箱";
  if (["[Gmail]/&V4NXPpCuTvY-", "[Gmail]/Spam"].includes(value)) return "垃圾邮件";
  return value || "未知目录";
}

function updateStage(title, state, result, description) {
  const card = element("article", `update-stage ${state}`);
  const head = element("div", "update-stage-head");
  head.append(element("h3", "", title), element("span", "stage-result", result));
  card.append(head, element("p", "", description));
  return card;
}

function refreshHomeHealth(status, generatedAt) {
  const health = updateHealth(status, generatedAt);
  const notice = $("#update-notice");
  notice.hidden = !health.notice;
  notice.className = `update-notice ${health.state}`;
  notice.replaceChildren();
  if (health.notice) {
    const link = element("a", "", "查看运行状态");
    link.href = "status.html";
    notice.append(element("span", "", health.notice), link);
  }
}

async function initToday() {
  const list = $("#today-list");
  const otherList = $("#other-list");
  try {
    const [recommendations, status] = await Promise.all([fetchJson("data/recommendations.json"), fetchJson("data/status.json")]);
    const articles = recommendations.articles || [];
    const otherArticles = recommendations.other_articles || [];
    const generatedAt = recommendations.generated_at || status.content_updated_at
      || (!["error", "stale", "pending"].includes(status.outcome) ? status.generated_at : "");
    $("#today-label").textContent = generatedAt ? formatUpdatedAt(generatedAt) : "时间待更新";
    $("#content-updated").textContent = "最近内容更新（北京时间）";
    refreshHomeHealth(status, generatedAt);
    setInterval(() => refreshHomeHealth(status, generatedAt), 60000);
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) refreshHomeHealth(status, generatedAt);
    });
    const sameBatch = generatedAt === status.generated_at;
    $("#new-count").textContent = sameBatch ? status.counts?.items_in_window ?? articles.length + otherArticles.length : articles.length + otherArticles.length;
    $("#recommend-count").textContent = articles.length;
    $("#seven-day-count").textContent = status.counts?.all_articles ?? "—";
    list.replaceChildren();
    if (!articles.length) {
      const health = updateHealth(status, generatedAt);
      list.append(emptyState(articles.length + otherArticles.length ? "本次更新没有推荐文章" : "本次没有文献记录",
        articles.length + otherArticles.length ? "没有文章达到当前标签推荐门槛。"
          : health.failed || health.incomplete || health.pending || health.offline ? "请结合运行状态确认本次读取和解析情况。"
            : "本次读取和解析已完成，没有发现文献。"));
    } else {
      articles.forEach((article, index) => list.append(todayArticleCard(article, index)));
    }
    otherList.replaceChildren();
    if (!otherArticles.length) {
      otherList.append(emptyState("没有其余更新", articles.length ? "本次更新的文章都已进入推荐区。" : "本批次没有其他文献记录。"));
    } else {
      otherArticles.forEach((article) => otherList.append(todayOtherArticleRow(article)));
    }
  } catch (error) {
    list.replaceChildren(errorState(`请稍后重试。${error.message}`));
    otherList.replaceChildren(errorState(`请稍后重试。${error.message}`));
  }
}

async function initHistory() {
  const recommendedList = $("#history-recommended-list");
  const otherList = $("#history-other-list");
  const dateSelect = $("#history-date");
  try {
    const [history, papers] = await Promise.all([fetchJson("data/history.json"), fetchJson("data/papers.json")]);
    const all = papers.articles || [];
    const byId = new Map(all.map((article) => [String(article.id), article]));
    const days = history.days || {};
    const dates = Object.keys(days).sort().reverse();
    if (!dates.length) {
      recommendedList.replaceChildren(emptyState("还没有历史记录", "首次成功完成日更后，这里会保留按日期查看的记录。"));
      otherList.replaceChildren(emptyState("还没有历史记录", "首次成功完成日更后，这里会保留按日期查看的记录。"));
      $("#history-count").textContent = "0 篇论文";
      return;
    }
    dates.forEach((date) => dateSelect.append(element("option", "", formatDate(days[date].generated_at))));
    dates.forEach((date, index) => { dateSelect.options[index].value = date; });

    function render() {
      const date = dateSelect.value;
      const day = days[date] || {};
      const articles = (day.article_ids || []).map((id) => byId.get(String(id))).filter(Boolean);
      let recommendedArticles;
      let otherArticles;
      if (Array.isArray(day.recommended_article_ids) && Array.isArray(day.other_article_ids)) {
        recommendedArticles = day.recommended_article_ids.map((id) => byId.get(String(id))).filter(Boolean);
        otherArticles = day.other_article_ids.map((id) => byId.get(String(id))).filter(Boolean);
      } else {
        recommendedArticles = articles.filter((article) => Number(article.score || 0) >= 3)
          .sort((a, b) => Number(b.score || 0) - Number(a.score || 0)).slice(0, 12);
        const recommendedIds = new Set(recommendedArticles.map((article) => String(article.id)));
        otherArticles = articles.filter((article) => !recommendedIds.has(String(article.id)));
      }
      $("#history-count").textContent = `${articles.length} 篇论文 · ${formatDate(day.generated_at)}`;
      $("#history-updated").textContent = day.generated_at ? `更新于 ${formatDate(day.generated_at, true)}` : "";
      recommendedList.replaceChildren();
      otherList.replaceChildren();
      if (!articles.length) {
        recommendedList.append(emptyState("这一天没有收录记录", "当日数据可能没有命中，或来源暂时没有成功返回。"));
        otherList.append(emptyState("这一天没有收录记录", "当日数据可能没有命中，或来源暂时没有成功返回。"));
        return;
      }
      if (recommendedArticles.length) {
        recommendedArticles.forEach((article, index) => recommendedList.append(todayArticleCard(article, index)));
      } else {
        recommendedList.append(emptyState("这一天没有推荐文章", "没有文章达到当日标签推荐门槛。"));
      }
      if (otherArticles.length) {
        otherArticles.forEach((article) => otherList.append(todayOtherArticleRow(article)));
      } else {
        otherList.append(emptyState("没有其他文章", "这一天的文章都已进入推荐区。"));
      }
    }

    dateSelect.addEventListener("change", render);
    render();
  } catch (error) {
    recommendedList.replaceChildren(errorState(`请稍后重试。${error.message}`));
    otherList.replaceChildren(errorState(`请稍后重试。${error.message}`));
  }
}

function metric(label, value, detail) {
  const box = element("div", "status-metric");
  box.append(element("span", "", label), element("strong", "", String(value ?? "—")), element("small", "", detail));
  return box;
}

async function initStatus() {
  try {
    const [status, recommendations] = await Promise.all([
      fetchJson("data/status.json"), fetchJson("data/recommendations.json").catch(() => ({})),
    ]);
    const contentUpdatedAt = status.content_updated_at || recommendations.generated_at
      || (!["error", "stale", "pending"].includes(status.outcome) ? status.generated_at : "");
    function refresh() {
      const health = updateHealth(status, contentUpdatedAt);
      const orb = $("#overall-status");
      orb.className = `status-orb ${health.state}`;
      orb.querySelector("strong").textContent = health.label;
      const notices = $("#status-notices");
      notices.replaceChildren();
      if (health.notice) notices.append(element("p", `update-notice ${health.state}`, health.notice));
    }
    refresh();
    setInterval(refresh, 60000);
    document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
    const health = updateHealth(status, contentUpdatedAt);
    const counts = status.counts || {};
    const windowLabel = status.window?.start
      ? `${formatUpdatedAt(status.window.start)} → ${formatUpdatedAt(status.window.end)}（北京时间）`
      : "昨日邮件接收窗口";
    $("#status-summary").append(
      metric("内容更新时间", formatUpdatedAt(contentUpdatedAt), "北京时间"),
      metric("本次识别", health.identified, "篇文献"),
      metric("本次推荐", health.recommended, "篇文献")
    );
    const email = status.email || {};
    const skipped = health.failed || health.offline || health.pending;
    const readIncomplete = email.status === "partial" || health.failedFolders.length > 0 || email.read_errors > 0;
    const abstracts = status.abstracts || {};
    const metadata = abstracts.metadata_fallback || {};
    const supplementIncomplete = Number(abstracts.errors || 0) + Number(metadata.errors || 0) + Number(abstracts.unavailable || 0) > 0;
    $("#update-stages").replaceChildren(
      updateStage("邮件读取", health.failed ? "error" : skipped ? "pending" : readIncomplete ? "warning" : "success",
        health.failed ? "读取失败" : skipped ? "未执行" : readIncomplete ? "读取不完整" : "已完成",
        health.failed ? "本次未能读取邮件，请检查邮箱连接并查看 Actions 日志。"
          : skipped ? "本次未执行在线邮件读取。"
            : readIncomplete ? "部分邮件或目录读取失败，可能漏收期刊提醒；请核查下方目录。"
              : `已读取接收窗口内 ${email.messages_in_window ?? email.recognized_alerts ?? 0} 封邮件。`),
      updateStage("文献解析", skipped ? "pending" : email.parser_errors ? "warning" : "success",
        skipped ? "未执行" : email.parser_errors ? "部分失败" : "已完成",
        skipped ? "邮件读取完成后才能解析文献。" : email.parser_errors
          ? `${email.parser_errors} 封邮件解析失败，部分文献可能未收录；请核查下方邮件名称。`
          : `已识别 ${health.identified} 篇文献。${email.empty_alerts ? `另有 ${email.empty_alerts} 封提醒未提取到文章，可在技术详情中核查。` : ""}`),
      updateStage("推荐生成", skipped ? "pending" : "success", skipped ? "未执行" : "已完成",
        skipped ? "本次未生成新推荐，已有内容保留。" : `推荐 ${health.recommended} 篇，其余 ${counts.other_today ?? 0} 篇。`),
      updateStage("摘要补全", skipped ? "pending" : supplementIncomplete ? "warning" : "success",
        skipped ? "未执行" : supplementIncomplete ? "部分未补全" : "已完成",
        skipped ? "本次未进行摘要补全。" : supplementIncomplete
          ? "部分摘要或元数据暂未补全。已收录文献可正常阅读，也可通过论文链接查看摘要。"
          : Number(abstracts.attempted || 0) + Number(metadata.attempted || 0) === 0 ? "本次无需额外补全摘要。" : "本次摘要补全已完成。")
    );
    const issues = $("#mail-issue-list");
    const failedMessages = email.failed_messages || [];
    failedMessages.forEach((message) => {
      const item = element("article", "mail-issue");
      item.append(element("strong", "", message.subject || "（无主题）"),
        element("p", "", "解析失败，可能漏收这封提醒中的文献。请核对邮件内容并查看本次日志。"));
      issues.append(item);
    });
    if (email.parser_errors && !failedMessages.length) {
      issues.append(element("p", "mail-issue", "这份旧运行记录未保存失败邮件的主题，请查看 Actions 日志。"));
    }
    health.failedFolders.forEach((folder) => {
      const item = element("article", "mail-issue");
      item.append(element("strong", "", `${folderName(folder.name)}读取失败`), element("p", "",
        `${folder.read_errors ? `${folder.read_errors} 封邮件未能读取。` : "未能完整读取该目录。"}具体邮件主题暂不可用，请检查邮箱连接或目录访问情况。`));
      issues.append(item);
    });
    if (email.read_errors && !health.failedFolders.length) {
      issues.append(element("p", "mail-issue", `${email.read_errors} 封邮件读取失败，具体主题暂不可用。`));
    }
    $("#mail-issues").hidden = !issues.childElementCount;
    $("#status-actions").append(actionsLink(status));
    $("#run-details").textContent = `本次尝试：${formatUpdatedAt(status.generated_at)}（北京时间） · 接收窗口：${windowLabel}`;
    const feedGrid = $("#feed-status");
    feedGrid.replaceChildren();
    const emailCard = element("article", `feed-card ${email.status || "ok"}`);
    const emailHead = element("div", "feed-head");
    emailHead.append(element("span", "status-dot"), element("strong", "Gmail IMAP"));
    emailCard.append(emailHead, element("p", "", `窗口候选 ${email.candidate_count ?? 0} 封 · 有效提醒 ${email.recognized_alerts ?? 0} 封 · 邮件文章 ${counts.items_in_window ?? 0} 篇 · 未识别 ${email.unrecognized ?? 0} 封 · 无文章 ${email.empty_alerts ?? 0} 封`));
    const folderText = (email.folders || []).map((folder) => `${folder.name}: ${folder.in_window ?? 0} 封`).join(" · ");
    emailCard.append(element("small", "", folderText || "没有目录统计"));
    if (email.error) emailCard.append(element("p", "", `读取错误：${email.error}`));
    (email.empty_messages || []).forEach((message) => emailCard.append(element("p", "", `未提取文章：${message.subject || "（无主题）"}。可能为无文章提醒或模板尚未覆盖。`)));
    failedMessages.forEach((message) => emailCard.append(element("p", "", `解析失败：${message.subject || "（无主题）"} · 错误类型：${message.reason || "未知"}`)));
    feedGrid.append(emailCard);
    const parserPanel = $("#crossref-status");
    parserPanel.replaceChildren();
    (status.parsers || []).forEach((parser) => parserPanel.append(metric(parser.name, parser.articles, "篇文章")));
    parserPanel.append(
      metric("网页摘要请求", abstracts.attempted, "篇高匹配文章"),
      metric("摘要已替换", abstracts.replaced, "篇"),
      metric("发现 DOI", abstracts.doi_discovered, "篇"),
      metric("网页不可用", abstracts.unavailable, "篇"),
      metric("网页请求错误", abstracts.errors, "次"),
      metric("元数据补全请求", metadata.attempted, "篇文献"),
      metric("元数据接口匹配", metadata.matched, "次匹配"),
      metric("元数据请求错误", metadata.errors, "次")
    );
  } catch (error) {
    $("#overall-status").className = "status-orb error";
    $("#overall-status strong").textContent = "状态暂不可用";
    $("#update-stages").replaceChildren();
    $("#status-summary").append(errorState(`请稍后重试。${error.message}`));
  }
}

const page = document.body.dataset.page;
initFooter();
if (page === "today") initToday();
if (page === "history") initHistory();
if (page === "status") initStatus();
