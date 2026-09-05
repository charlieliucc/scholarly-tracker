#!/usr/bin/env python3
"""Read and parse journal alert mail from Gmail over read-only IMAP.

The module deliberately keeps only normalized article metadata in memory.  Raw
messages are never written to disk by the tracker.
"""

from __future__ import annotations

import email
import email.header
import email.message
import email.policy
import hashlib
import html
import imaplib
import re
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any, Callable, Iterable, Optional


DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)
SPACE_RE = re.compile(r"\s+")
BOILERPLATE = re.compile(
    r"(?:manage (?:(?:your|my) )?alerts|unsubscribe|privacy policy|terms and conditions|view in browser|read now|click here to read)",
    re.IGNORECASE,
)


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    value = html.unescape(str(value)).replace("\xa0", " ")
    value = re.sub(r"<\s*br\s*/?>", "\n", value, flags=re.IGNORECASE)
    value = re.sub(r"<[^>]+>", " ", value)
    return SPACE_RE.sub(" ", value).strip()


def normalize_doi(value: str) -> str:
    match = DOI_RE.search(urllib.parse.unquote(value or ""))
    return match.group(0).rstrip(".,;)]}").lower() if match else ""


def normalize_title(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", clean_text(value).casefold())


def extract_publication_date(value: str) -> tuple[str, str]:
    text = clean_text(value)
    patterns = (
        (r"\b(\d{1,2}\s+[A-Za-z]{3,9}\s+(?:19|20)\d{2})\b", "%d %B %Y"),
        (r"\b([A-Za-z]{3,9}\s+\d{1,2},\s+(?:19|20)\d{2})\b", "%B %d, %Y"),
    )
    for pattern, fmt in patterns:
        match = re.search(pattern, text)
        if not match:
            continue
        raw = match.group(1)
        for candidate_fmt in (fmt, fmt.replace("%B", "%b")):
            try:
                return datetime.strptime(raw, candidate_fmt).date().isoformat(), raw
            except ValueError:
                pass
    return "", ""


def decode_header(value: str) -> str:
    parts = []
    for fragment, charset in email.header.decode_header(value or ""):
        if isinstance(fragment, bytes):
            parts.append(fragment.decode(charset or "utf-8", errors="replace"))
        else:
            parts.append(fragment)
    return "".join(parts).strip()


def sender_address(value: str) -> str:
    match = re.search(r"<([^>]+)>", value or "")
    return (match.group(1) if match else value or "").strip().casefold()


@dataclass
class MailMessage:
    identity: str
    folder: str
    received_at: datetime
    sender: str
    subject: str
    html_body: str
    text_body: str


def _body_parts(message: email.message.Message) -> tuple[str, str]:
    html_body = ""
    text_body = ""
    if message.is_multipart():
        for part in message.walk():
            if part.is_multipart() or part.get_content_disposition() == "attachment":
                continue
            content_type = part.get_content_type().casefold()
            payload = part.get_payload(decode=True)
            if payload is None:
                raw = part.get_payload()
                payload = raw.encode("utf-8", errors="replace") if isinstance(raw, str) else b""
            charset = part.get_content_charset() or "utf-8"
            value = payload.decode(charset, errors="replace")
            if content_type == "text/html" and len(value) > len(html_body):
                html_body = value
            elif content_type == "text/plain" and len(value) > len(text_body):
                text_body = value
    else:
        payload = message.get_payload(decode=True) or b""
        value = payload.decode(message.get_content_charset() or "utf-8", errors="replace")
        if message.get_content_type().casefold() == "text/html":
            html_body = value
        else:
            text_body = value
    return html_body, text_body


def parse_rfc822(raw: bytes, folder: str, received_at: datetime, identity: str = "") -> MailMessage:
    message = BytesParser(policy=email.policy.default).parsebytes(raw)
    html_body, text_body = _body_parts(message)
    header_identity = decode_header(message.get("Message-ID", ""))
    identity = identity or header_identity or hashlib.sha256(raw).hexdigest()
    return MailMessage(
        identity=identity,
        folder=folder,
        received_at=received_at.astimezone(timezone.utc),
        sender=sender_address(decode_header(message.get("From", ""))),
        subject=decode_header(message.get("Subject", "")),
        html_body=html_body,
        text_body=text_body,
    )


def _imap_date(value: datetime) -> str:
    return value.strftime("%d-%b-%Y")


def _parse_internaldate(value: bytes) -> Optional[datetime]:
    match = re.search(rb'INTERNALDATE\s+"([^"]+)"', value, flags=re.IGNORECASE)
    if not match:
        return None
    try:
        parsed = parsedate_to_datetime(match.group(1).decode("ascii", errors="replace"))
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _folder_name(raw: bytes) -> tuple[str, str]:
    text = raw.decode("utf-8", errors="replace")
    flags = text.partition(")")[0].casefold()
    quoted = re.findall(r'"((?:[^"\\]|\\.)*)"', text)
    name = quoted[-1].replace('\\"', '"') if quoted else text.rsplit(" ", 1)[-1]
    try:
        name = imaplib.IMAP4._mode_utf7.decode(name)
    except Exception:
        pass
    return name, flags


def discover_folders(mail: Any) -> list[str]:
    response, rows = mail.list()
    if response != "OK":
        raise RuntimeError("Gmail LIST failed")
    folders = ["INBOX"]
    for row in rows or []:
        if not isinstance(row, bytes):
            continue
        name, flags = _folder_name(row)
        if "\\junk" in flags and name not in folders:
            folders.append(name)
    return folders


def _fetch_uid(mail: Any, uid: bytes, folder: str) -> Optional[MailMessage]:
    response, data = mail.uid("fetch", uid, "(INTERNALDATE BODY.PEEK[] X-GM-MSGID)")
    if response != "OK":
        return None
    header_bytes = b""
    body_bytes = b""
    for item in data or []:
        if isinstance(item, tuple):
            if isinstance(item[0], bytes):
                header_bytes += item[0]
            if isinstance(item[1], bytes):
                body_bytes += item[1]
    received_at = _parse_internaldate(header_bytes)
    if not received_at or not body_bytes:
        return None
    gm_match = re.search(rb"X-GM-MSGID\s+(\d+)", header_bytes)
    identity = gm_match.group(1).decode("ascii") if gm_match else ""
    return parse_rfc822(body_bytes, folder, received_at, identity)


def fetch_messages(
    username: str,
    app_password: str,
    start: datetime,
    end: datetime,
    host: str = "imap.gmail.com",
    port: int = 993,
    max_messages: int = 500,
    client_factory: Callable[..., Any] = imaplib.IMAP4_SSL,
) -> tuple[list[MailMessage], dict[str, Any]]:
    """Fetch a bounded date range using read-only IMAP selects."""
    if not username or not app_password:
        raise RuntimeError("Gmail credentials are missing")
    mail = client_factory(host, port)
    try:
        mail.login(username, app_password.replace(" ", ""))
        folders = discover_folders(mail)
        messages: list[MailMessage] = []
        stats = {"folders": [], "candidate_count": 0, "duplicate_count": 0}
        seen: set[str] = set()
        for folder in folders:
            response, _ = mail.select(folder, readonly=True)
            if response != "OK":
                stats["folders"].append({"name": folder, "status": "error", "error": "SELECT failed"})
                continue
            # Expand the server-side date search by a day, then use INTERNALDATE
            # for the exact half-open Asia/Shanghai window in the caller.
            response, data = mail.uid(
                "search", None,
                f'(SINCE "{_imap_date(start - timedelta(days=1))}" '
                f'BEFORE "{_imap_date(end + timedelta(days=1))}")',
            )
            uids = (data[0].split() if response == "OK" and data else [])[:max_messages]
            folder_stat = {"name": folder, "status": "ok", "candidates": len(uids), "in_window": 0}
            stats["candidate_count"] += len(uids)
            for uid in uids:
                item = _fetch_uid(mail, uid, folder)
                if not item or not (start <= item.received_at < end):
                    continue
                folder_stat["in_window"] += 1
                if item.identity in seen:
                    stats["duplicate_count"] += 1
                    continue
                seen.add(item.identity)
                messages.append(item)
            stats["folders"].append(folder_stat)
        return messages, stats
    finally:
        try:
            mail.logout()
        except Exception:
            pass


class AnchorParser(HTMLParser):
    """Keep visible line boundaries and each link's position in the message."""
    BLOCKS = {"p", "div", "tr", "td", "li", "br", "h1", "h2", "h3", "h4", "section", "article"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.anchors = []
        self.parts = []
        self.active = None
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"head", "script", "style"}:
            self.skip += 1
        if self.skip:
            return
        if tag in self.BLOCKS:
            self.parts.append("\n")
        if tag == "a":
            self.active = (dict(attrs).get("href", "") or "", len(self.parts))

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag in {"head", "script", "style"}:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        if tag == "a" and self.active:
            url, start = self.active
            self.anchors.append((url, clean_text("".join(self.parts[start:])), start, len(self.parts)))
            self.active = None
        if tag in self.BLOCKS:
            self.parts.append("\n")


def _author_line(value: str) -> bool:
    value = re.sub(r"^(?:by|authors?)\s*:?\s+", "", value, flags=re.I).strip(" ,;")
    if not value or len(value) > 300 or re.search(r"https?://|\d{4}|[!?…:]", value):
        return False
    words = re.findall(r"[^\W\d_]+(?:[’'-][^\W\d_]+)*", value, flags=re.UNICODE)
    particles = {"and", "de", "del", "van", "von", "da", "di", "la", "et", "al"}
    return len(words) >= 2 and all(w[0].isupper() or w in particles for w in words)


def _block_metadata(publisher: str, block: str) -> tuple[str, str]:
    lines = [clean_text(line) for line in block.splitlines() if clean_text(line)]
    if publisher == "Elsevier":
        for index, line in enumerate(lines):
            if re.search(r"Available Online", line, re.I):
                lines = lines[index + 1:]
                break
    authors = []
    abstract = []
    for line in lines:
        if re.search(r"^(?:Read article|New Articles in Press|First Published|Version of Record|Online Version|Manage|Unsubscribe|You are receiving|To update|Open Access|Research article|Full Article|Original Article|\||e\d{4,})\b", line, re.I):
            break
        if _author_line(line) and (not abstract or publisher == "Nature"):
            if publisher == "Nature" and authors:
                break
            authors.append(re.sub(r"^(?:by|authors?)\s*:?\s+", "", line, flags=re.I).strip(" ,;"))
        elif publisher in {"Taylor & Francis", "Nature"} and len(line) >= 60:
            abstract.append(re.sub(r"^Abstract\s*:?\s*", "", line, flags=re.I))
        elif line.casefold().startswith("abstract"):
            abstract.append(re.sub(r"^Abstract\s*:?\s*", "", line, flags=re.I))
        elif authors:
            break
    return ", ".join(authors), " ".join(abstract)


def _article(journal: str, publisher: str, title: str, url: str, authors: str, snippet: str, received: datetime) -> dict[str, Any]:
    title = clean_text(title)
    url = html.unescape(url).strip()
    doi = normalize_doi(f"{url} {title}")
    stable = doi or url or f"{journal}|{title}"
    published, publication_text = extract_publication_date(snippet)
    return {
        "id": hashlib.sha256(stable.encode("utf-8")).hexdigest()[:20],
        "title": title,
        "url": url,
        "guid": doi or url,
        "doi": doi,
        "authors": [clean_text(part) for part in re.split(r",|\s+and\s+|\s*&\s*", authors) if clean_text(part)],
        "abstract": clean_text(snippet),
        "abstract_source": "email" if clean_text(snippet) else "",
        "published": published,
        "publication_text": publication_text,
        "feed_timestamp": received.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "date_precision": "time",
        "journal": journal or publisher,
        "journal_id": re.sub(r"[^a-z0-9]+", "-", (journal or publisher).casefold()).strip("-") or "email",
        "publisher": publisher,
        "volume": "",
        "issue": "",
        "pages": "",
        "type": "journal-article",
        "first_seen": received.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "last_seen": received.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "metadata_source": "email",
    }


def _subject_journal(subject: str, publisher: str) -> str:
    if publisher == "Elsevier":
        return re.split(r"\s*:\s*(?:Alert|Volume)\b", subject, maxsplit=1, flags=re.IGNORECASE)[0].strip()
    if publisher == "SAGE":
        match = re.search(r"\bfor\s+(.+)$", subject, re.IGNORECASE)
        return match.group(1).strip() if match else ""
    if publisher == "Taylor & Francis":
        match = re.search(r"\bfor\s+(.+?)(?:\s+are now available|\s+New articles)\b", subject, re.IGNORECASE)
        return match.group(1).strip() if match else "Taylor & Francis"
    if publisher == "Wiley":
        match = re.search(r"(?:Alert|Articles Alert):\s*(.+)$", subject, re.IGNORECASE)
        return re.sub(r",\s*Vol(?:ume)?\..*$", "", match.group(1)).strip() if match else ""
    return "Nature (Work)" if "nature" in subject.casefold() else ""


def _publisher(subject: str, sender: str, body: str) -> str:
    sender = sender.casefold()
    subject_low = subject.casefold()
    if "sciencedirect@notification.elsevier.com" in sender and ("alert" in subject_low or "volume" in subject_low):
        return "Elsevier"
    if sender.endswith("@sagepub.com") and ("onlinefirst" in subject_low or "online first" in subject_low):
        return "SAGE"
    if sender.endswith("@tandfonline.com") and "article" in subject_low:
        return "Taylor & Francis"
    if sender.endswith("@email.taylorandfrancis.com") and ("ready to read" in subject_low or "article" in subject_low):
        return "Taylor & Francis"
    if "@wiley.com" in sender or sender.endswith("@email2.wiley.com"):
        if "alert" in subject_low or "new article" in subject_low or "early view" in subject_low:
            return "Wiley"
    if ("nature" in sender or "springernature.com" in sender) and "alert" in subject_low:
        return "Nature"
    return ""


def _is_usable_anchor(url: str, title: str, publisher: str) -> bool:
    if not url.startswith(("https://", "http://")) or len(title) < 12:
        return False
    low = title.casefold()
    if BOILERPLATE.search(low) or low in {"read article", "read issue", "view latest articles", "editorial board", "elsevier b.v."} or low.startswith(("new articles in press", "http://", "https://")) or re.match(r"^(?:volume|issue)\s+\d", low) or any(term in low for term in ("safe senders", "forward to", "browse journals", "search all", "publish with", "view books", "add to your", "view these articles")):
        return False
    host = (urllib.parse.urlparse(url).hostname or "").casefold()
    domains = {
        "Elsevier": ("elsevier.com", "sciencedirect.com"),
        "SAGE": ("sagepub.com",),
        "Taylor & Francis": ("tandfonline.com", "taylorandfrancis.com"),
        "Wiley": ("wiley.com",),
        "Nature": ("nature.com", "springernature.com"),
    }.get(publisher, ())
    return any(host == domain or host.endswith("." + domain) for domain in domains)


def parse_message(message: MailMessage) -> tuple[list[dict[str, Any]], str]:
    body = message.html_body or message.text_body
    publisher = _publisher(message.subject, message.sender, body)
    if not publisher:
        return [], "unrecognized"
    journal = _subject_journal(message.subject, publisher)
    parser = AnchorParser()
    if message.html_body:
        try:
            parser.feed(message.html_body)
            parser.close()
        except Exception:
            parser.anchors = []
    articles: list[dict[str, Any]] = []
    if not journal and publisher == "Wiley":
        visible = [clean_text(line) for line in "".join(parser.parts).splitlines() if clean_text(line)]
        for index, line in enumerate(visible):
            if index and (line == "Early View" or re.match(r"Volume \d", line)):
                journal = visible[index - 1]
                break
    section = ""
    for index, (url, title, start, end) in enumerate(parser.anchors):
        if not _is_usable_anchor(url, title, publisher) or normalize_title(title) == normalize_title(journal):
            continue
        before = "".join(parser.parts[:start])
        if publisher == "Nature":
            headings = re.findall(r"(?:^|\n)\s*(Work|Career|News|News in Focus|Research|Research Highlights|Comment|Books|Editorial|World View|New Online)\s*(?:\n|$)", before)
            section = headings[-1] if headings else ""
            if section != "Work":
                continue
        next_start = parser.anchors[index + 1][2] if index + 1 < len(parser.anchors) else len(parser.parts)
        block = "".join(parser.parts[end:next_start])
        authors, snippet = _block_metadata(publisher, block)
        article = _article(journal, publisher, title, url, authors, snippet, message.received_at)
        article["published"], article["publication_text"] = extract_publication_date(block)
        articles.append(article)
    if publisher == "SAGE" and not articles:
        plain = message.text_body or "".join(parser.parts)
        lines = [clean_text(line) for line in plain.splitlines() if clean_text(line)]
        for index, line in enumerate(lines):
            if line.casefold() != "article":
                continue
            block = []
            for following in lines[index + 1:]:
                if following.startswith(("https://", "http://")):
                    if len(block) >= 2:
                        date = block.pop() if re.search(r"OnlineFirst|Online First", block[-1], re.I) else ""
                        authors = block.pop()
                        title = " ".join(block)
                        if _is_usable_anchor(following, title, publisher):
                            article = _article(journal, publisher, title, following, authors, "", message.received_at)
                            article["published"], article["publication_text"] = extract_publication_date(date)
                            articles.append(article)
                    break
                if following.casefold() == "article" or following.startswith("---"):
                    break
                block.append(following)
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for article in articles:
        key = normalize_doi(article["doi"]) or normalize_title(article["title"])
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(article)
    return unique, ("ok" if unique else "no_articles")


def clean_legacy_email_articles(articles: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove definite navigation records produced by the old anchor parser."""
    result = []
    for original in articles:
        article = dict(original)
        if str(article.get("metadata_source", "")).startswith("email"):
            title = clean_text(article.get("title", ""))
            if (normalize_title(title) == normalize_title(article.get("journal", ""))
                    or not _is_usable_anchor(article.get("url", ""), title, article.get("publisher", ""))):
                continue
            abstract = article.get("abstract", "")
            if article.get("abstract_source") == "email" and (
                    re.fullmatch(r"(?:Research|Review|Original|Full) Article|Comment|Open Access", abstract, re.I)
                    or re.search(r"@media|\.responsive[-{]|\.mobile[-{]", abstract)):
                article["abstract"] = ""
                article["abstract_source"] = ""
        result.append(article)
    return result


def parse_messages(messages: Iterable[MailMessage]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    articles: list[dict[str, Any]] = []
    stats = {"messages": 0, "recognized": 0, "unrecognized": 0, "empty": 0, "errors": 0, "parsers": {}}
    for message in messages:
        stats["messages"] += 1
        try:
            current, result = parse_message(message)
        except Exception:
            stats["errors"] += 1
            continue
        if result == "unrecognized":
            stats["unrecognized"] += 1
        elif result == "no_articles":
            stats["empty"] += 1
        else:
            stats["recognized"] += 1
            publisher = current[0]["publisher"] if current else "unknown"
            stats["parsers"].setdefault(publisher, 0)
            stats["parsers"][publisher] += len(current)
        articles.extend(current)
    return articles, stats
