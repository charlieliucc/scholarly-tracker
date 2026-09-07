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
  if (article.doi) actions.append(externalLink(article.doi_url || `https://doi.org/${article.doi}`, `DOI ${article.doi}`, "doi-link"));
  const score = element("span", `score ${Number(article.score) < 0 ? "negative" : ""}`, `${scoreLabel(article.score)} 分`);
  actions.append(score);

  body.append(meta, title, authors);
  if (article.abstract) {
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
  return info;
}

function todayDetails(article, summaryText = "查看详细信息") {
  const details = element("details", "today-details");
  details.append(element("summary", "", summaryText));
  const content = element("div", "today-details-content");
  const metadata = element("div", "today-detail-meta");
  if (article.doi) metadata.append(externalLink(article.doi_url || `https://doi.org/${article.doi}`, `DOI ${article.doi}`, "doi-link"));
  if (metadata.childNodes.length) content.append(metadata);
  if (article.abstract) content.append(element("p", "today-abstract", article.abstract));
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
  body.append(todayDetails(article, "查看摘要和 DOI"));
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
  body.append(todayDetails(article));
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

async function initToday() {
  const list = $("#today-list");
  const otherList = $("#other-list");
  try {
    const [recommendations, status] = await Promise.all([fetchJson("data/recommendations.json"), fetchJson("data/status.json")]);
    const articles = recommendations.articles || [];
    const otherArticles = recommendations.other_articles || [];
    const generatedAt = recommendations.generated_at || status.generated_at;
    $("#today-label").textContent = generatedAt ? formatDate(generatedAt, true) : "时间待更新";
    $("#new-count").textContent = status.counts?.items_in_window ?? status.counts?.new_today ?? "—";
    $("#recommend-count").textContent = status.counts?.recommended_today ?? articles.length;
    list.replaceChildren();
    if (!articles.length) {
      list.append(emptyState("本次更新没有推荐文章", "没有文章达到当前标签推荐门槛。"));
    } else {
      articles.forEach((article, index) => list.append(todayArticleCard(article, index)));
    }
    otherList.replaceChildren();
    if (!otherArticles.length) {
      otherList.append(emptyState("没有其余更新", "本次更新的文章都已进入推荐区。"));
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
    dates.forEach((date) => dateSelect.append(element("option", "", formatDate(date))));
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
      $("#history-count").textContent = `${articles.length} 篇论文 · ${formatDate(date)}`;
      $("#history-updated").textContent = day.generated_at ? `记录生成于 ${formatDate(day.generated_at, true)}` : "";
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
    const status = await fetchJson("data/status.json");
    const names = { success: "运行正常", partial: "部分成功", stale: "使用旧数据", error: "运行失败", pending: "等待首次运行" };
    const orb = $("#overall-status");
    orb.className = `status-orb ${status.outcome || "pending"}`;
    orb.querySelector("strong").textContent = names[status.outcome] || status.outcome;
    const counts = status.counts || {};
    const windowLabel = status.window?.start
      ? `${formatDate(status.window.start, true)} → ${formatDate(status.window.end, true)} · ${status.window.timezone}`
      : "昨日邮件接收窗口";
    $("#status-summary").append(
      metric("本次处理", counts.processed_this_run ?? counts.items_in_window ?? counts.fetched_this_run, windowLabel),
      metric("首次收录", counts.new_today, "篇新增记录"),
      metric("近 7 天论文", counts.all_articles, "篇历史记录"),
      metric("生成时间", formatDate(status.generated_at, true), `上次完全成功：${formatDate(status.last_success_at, true)}`)
    );
    const email = status.email || {};
    const feedGrid = $("#feed-status");
    feedGrid.replaceChildren();
    const emailCard = element("article", `feed-card ${email.status || "ok"}`);
    const emailHead = element("div", "feed-head");
    emailHead.append(element("span", "status-dot"), element("strong", "Gmail IMAP"));
    emailCard.append(emailHead, element("p", "", `窗口候选 ${email.candidate_count ?? 0} 封 · 有效提醒 ${email.recognized_alerts ?? 0} 封 · 邮件文章 ${counts.items_in_window ?? 0} 篇 · 未识别 ${email.unrecognized ?? 0} 封 · 无文章 ${email.empty_alerts ?? 0} 封`));
    const folderText = (email.folders || []).map((folder) => `${folder.name}: ${folder.in_window ?? 0} 封`).join(" · ");
    emailCard.append(element("small", "", folderText || (email.error || "没有目录统计")));
    feedGrid.append(emailCard);
    const parserPanel = $("#crossref-status");
    parserPanel.replaceChildren();
    (status.parsers || []).forEach((parser) => parserPanel.append(metric(parser.name, parser.articles, "篇文章")));
    const abstracts = status.abstracts || {};
    parserPanel.append(
      metric("网页摘要请求", abstracts.attempted, "篇高匹配文章"),
      metric("摘要已替换", abstracts.replaced, "篇"),
      metric("发现 DOI", abstracts.doi_discovered, "篇"),
      metric("网页不可用", abstracts.unavailable, "篇"),
      metric("请求错误", abstracts.errors, "次")
    );
  } catch (error) {
    $("#status-summary").append(errorState(`请稍后重试。${error.message}`));
  }
}

const page = document.body.dataset.page;
if (page === "today") initToday();
if (page === "history") initHistory();
if (page === "status") initStatus();
