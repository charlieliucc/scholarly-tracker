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
  if ((article.matched_keywords || []).length) {
    const keywords = element("div", "keyword-list");
    article.matched_keywords.slice(0, 6).forEach((match) => {
      const tag = element("span", Number(match.contribution) < 0 ? "negative" : "", `${match.keyword} ${scoreLabel(match.contribution)}`);
      tag.title = `命中字段：${(match.fields || []).join("、")}`;
      keywords.append(tag);
    });
    body.append(keywords);
  }
  body.append(actions);
  card.append(rank, body);
  return card;
}

function compactArticleCard(article) {
  const card = element("article", "other-paper-row");
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
  actions.append(element("span", `score ${Number(article.score) < 0 ? "negative" : ""}`, `${scoreLabel(article.score)} 分`));
  body.append(meta, title, authors, actions);
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
    const windowDate = recommendations.window?.start?.slice(0, 10) || recommendations.date;
    $("#today-label").textContent = `DAILY READING · ${formatDate(windowDate)} 日更`;
    $("#hero-count").textContent = articles.length + otherArticles.length;
    $("#new-count").textContent = status.counts?.items_in_window ?? status.counts?.new_today ?? "—";
    $("#recommend-count").textContent = status.counts?.recommended_today ?? articles.length;
    $("#total-count").textContent = status.counts?.all_articles ?? "—";
    $("#updated-at").textContent = formatDate(status.generated_at, true);
    list.replaceChildren();
    if (!articles.length) {
      list.append(emptyState("本次日更没有命中推荐", "精确日期来源的昨日条目和 GUID 来源的本次新增条目中，没有论文达到最低关键词得分。你仍可前往“全部论文”浏览累计记录。"));
    } else {
      articles.forEach((article, index) => list.append(articleCard(article, index)));
    }
    otherList.replaceChildren();
    if (!otherArticles.length) {
      otherList.append(emptyState("没有其他新论文", "本次日更处理的论文都已进入推荐区，或没有新增记录。"));
    } else {
      otherArticles.forEach((article) => otherList.append(compactArticleCard(article)));
    }
  } catch (error) {
    list.replaceChildren(errorState(`请稍后重试。${error.message}`));
    otherList.replaceChildren(errorState(`请稍后重试。${error.message}`));
  }
}

async function initPapers() {
  const list = $("#paper-list");
  let all = [];
  let visible = 50;
  try {
    const payload = await fetchJson("data/papers.json");
    all = payload.articles || [];
    $("#archive-updated").textContent = `更新于 ${formatDate(payload.generated_at, true)}`;
  } catch (error) {
    list.replaceChildren(errorState(`请稍后重试。${error.message}`));
    return;
  }
  [...new Set(all.map((item) => item.journal).filter(Boolean))].sort().forEach((journal) => {
    const option = element("option", "", journal);
    option.value = journal;
    $("#journal-filter").append(option);
  });

  function render(reset = false) {
    if (reset) visible = 50;
    const query = $("#paper-search").value.trim().toLocaleLowerCase();
    const journal = $("#journal-filter").value;
    const sort = $("#sort-filter").value;
    const filtered = all.filter((article) => {
      if (journal && article.journal !== journal) return false;
      if (!query) return true;
      const haystack = [article.title, article.abstract, article.doi, ...(article.authors || []), ...(article.matched_keywords || []).map((item) => item.keyword)].join(" ").toLocaleLowerCase();
      return haystack.includes(query);
    });
    filtered.sort((a, b) => {
      if (sort === "score") return Number(b.score || 0) - Number(a.score || 0) || String(b.published || "").localeCompare(a.published || "");
      if (sort === "seen") return String(b.first_seen || "").localeCompare(a.first_seen || "");
      return String(b.published || "").localeCompare(a.published || "");
    });
    $("#result-count").textContent = `找到 ${filtered.length} 篇论文`;
    list.replaceChildren();
    if (!filtered.length) list.append(emptyState("没有匹配的论文", "请尝试缩短检索词或切换期刊筛选。"));
    filtered.slice(0, visible).forEach((article, index) => list.append(articleCard(article, index)));
    $("#load-more").hidden = filtered.length <= visible;
    $("#load-more").onclick = () => { visible += 50; render(); };
  }
  $("#paper-search").addEventListener("input", () => render(true));
  $("#journal-filter").addEventListener("change", () => render(true));
  $("#sort-filter").addEventListener("change", () => render(true));
  render();
}

async function initHistory() {
  const list = $("#history-list");
  const dateSelect = $("#history-date");
  try {
    const [history, papers] = await Promise.all([fetchJson("data/history.json"), fetchJson("data/papers.json")]);
    const all = papers.articles || [];
    const byId = new Map(all.map((article) => [String(article.id), article]));
    const days = history.days || {};
    const dates = Object.keys(days).sort().reverse();
    if (!dates.length) {
      list.replaceChildren(emptyState("还没有历史记录", "首次成功完成日更后，这里会保留按日期查看的记录。"));
      $("#history-count").textContent = "0 篇论文";
      return;
    }
    dates.forEach((date) => dateSelect.append(element("option", "", formatDate(date))));
    dates.forEach((date, index) => { dateSelect.options[index].value = date; });

    function render() {
      const date = dateSelect.value;
      const day = days[date] || {};
      const articles = (day.article_ids || []).map((id) => byId.get(String(id))).filter(Boolean);
      $("#history-count").textContent = `${articles.length} 篇论文 · ${formatDate(date)}`;
      $("#history-updated").textContent = day.generated_at ? `记录生成于 ${formatDate(day.generated_at, true)}` : "";
      list.replaceChildren();
      if (!articles.length) {
        list.append(emptyState("这一天没有收录记录", "当日数据可能没有命中，或来源暂时没有成功返回。"));
        return;
      }
      articles.forEach((article, index) => list.append(articleCard(article, index)));
    }

    dateSelect.addEventListener("change", render);
    render();
  } catch (error) {
    list.replaceChildren(errorState(`请稍后重试。${error.message}`));
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
      ? `${formatDate(status.window.start, true)} → ${formatDate(status.window.end, true)} · ${status.window.timezone}；GUID 源按首次出现`
      : "精确日期源按昨日窗口；GUID 源按首次出现";
    $("#status-summary").append(
      metric("本次处理", counts.processed_this_run ?? counts.items_in_window ?? counts.fetched_this_run, windowLabel),
      metric("首次收录", counts.new_today, "篇新增记录"),
      metric("累计论文", counts.all_articles, "篇可检索记录"),
      metric("生成时间", formatDate(status.generated_at, true), `上次完全成功：${formatDate(status.last_success_at, true)}`)
    );
    const feedGrid = $("#feed-status");
    feedGrid.replaceChildren();
    (status.feeds || []).forEach((feed) => {
      const card = element("article", `feed-card ${feed.status}`);
      const head = element("div", "feed-head");
      head.append(element("span", "status-dot"), element("strong", "", feed.name));
      const feedMessage = feed.status === "ok"
        ? feed.discovery_mode === "crossref_online_first"
          ? `Crossref published-online · 窗口内 ${feed.items || 0} 篇 OnlineFirst · 查询 ${feed.received || 0} 条 · 已有卷期跳过 ${feed.with_issue || 0} 条`
          : feed.discovery_mode === "guid_diff"
          ? feed.baseline_created
            ? `已建立基线 ${feed.known_items || 0} 篇 · 本次不补录历史文章 · 日期不精确 ${feed.imprecise_dates || 0} 篇`
            : `本次新发现 ${feed.new_items || 0} 篇 · Feed 共 ${feed.received ?? 0} 篇 · 累计识别 ${feed.known_items || 0} 篇 · 日期不精确 ${feed.imprecise_dates || 0} 篇（按 GUID 处理） · 元数据变化 ${feed.updated_items || 0} 篇 · 移出 Feed ${feed.removed_items || 0} 篇`
          : `窗口内 ${feed.items} 篇 · Feed 共 ${feed.received ?? feed.items} 篇 · 日期不精确跳过 ${feed.missing_precise_date || 0} 篇 · 窗口外 ${feed.outside_window || 0} 篇`
        : feed.status === "fallback"
          ? `RSS 被来源站拒绝，已用 Crossref 同窗口回退 · 获取 ${feed.items} 篇`
          : (feed.error || "抓取失败");
      card.append(head, element("p", "", feedMessage));
      const buildTime = feed.last_build_date ? ` · Feed 更新 ${formatDate(feed.last_build_date, true)}` : "";
      const timing = element("small", "", `${feed.duration_ms ?? 0} ms${buildTime} · `);
      timing.append(externalLink(feed.url, feed.source === "crossref" ? "查看 Crossref" : "查看 RSS"));
      card.append(timing);
      feedGrid.append(card);
    });
    const crossref = status.crossref || {};
    $("#crossref-status").append(
      metric("尝试查询", crossref.attempted, "条记录"),
      metric("成功匹配", crossref.matched, "条记录"),
      metric("未找到", crossref.not_found, "条记录"),
      metric("请求错误", crossref.errors, `另有 ${crossref.skipped || 0} 条因单次上限跳过`)
    );
  } catch (error) {
    $("#status-summary").append(errorState(`请稍后重试。${error.message}`));
  }
}

const page = document.body.dataset.page;
if (page === "today") initToday();
if (page === "history") initHistory();
if (page === "papers") initPapers();
if (page === "status") initStatus();
