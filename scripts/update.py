#!/usr/bin/env python3
"""Read Gmail journal alerts, optionally enrich high-score records from DOI pages, and build site data."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, time as datetime_time, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from difflib import SequenceMatcher
from typing import Any, Iterable, Optional
from zoneinfo import ZoneInfo

try:
    from .email_source import fetch_messages, parse_messages, clean_legacy_email_articles
except ImportError:  # python scripts/update.py
    from email_source import fetch_messages, parse_messages, clean_legacy_email_articles


DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)
SPACE_RE = re.compile(r"\s+")
MONTHS = {
    name.lower(): number
    for number, name in enumerate(
        (
            "January", "February", "March", "April", "May", "June",
            "July", "August", "September", "October", "November", "December",
        ),
        1,
    )
}


class TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def clean_html(value: Any) -> str:
    if value is None:
        return ""
    parser = TextExtractor()
    try:
        parser.feed(html.unescape(str(value)))
        text = " ".join(parser.parts)
    except Exception:
        text = str(value)
    return SPACE_RE.sub(" ", html.unescape(text)).strip()


def element_text(element: ET.Element) -> str:
    return "".join(element.itertext()).strip()


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def child_text(element: ET.Element, *names: str) -> str:
    wanted = {name.lower() for name in names}
    for child in element:
        if local_name(child.tag) in wanted:
            value = element_text(child)
            if value:
                return value
    return ""


def all_child_text(element: ET.Element, *names: str) -> list[str]:
    wanted = {name.lower() for name in names}
    values = []
    for child in element:
        if local_name(child.tag) in wanted:
            value = element_text(child)
            if value:
                values.append(value)
    return values


def normalize_doi(value: str) -> str:
    match = DOI_RE.search(urllib.parse.unquote(value or ""))
    return match.group(0).rstrip(".,;)]}").lower() if match else ""


def normalized_text(value: str) -> str:
    value = unicodedata.normalize("NFKD", clean_html(value)).casefold()
    value = "".join(char for char in value if char.isalnum() or char.isspace())
    return SPACE_RE.sub(" ", value).strip()


def parse_date(value: str) -> str:
    value = clean_html(value)
    if not value:
        return ""
    try:
        parsed = parsedate_to_datetime(value)
        return parsed.date().isoformat()
    except (TypeError, ValueError, OverflowError):
        pass
    iso_match = re.search(r"\b(19|20)\d{2}-\d{2}-\d{2}\b", value)
    if iso_match:
        return iso_match.group(0)
    long_match = re.search(
        r"\b(?:(\d{1,2})\s+)?(" + "|".join(MONTHS) + r")\s+((?:19|20)\d{2})\b",
        value,
        re.IGNORECASE,
    )
    if long_match:
        day = int(long_match.group(1) or 1)
        month = MONTHS[long_match.group(2).lower()]
        return date(int(long_match.group(3)), month, day).isoformat()
    year_match = re.search(r"\b(19|20)\d{2}\b", value)
    return f"{year_match.group(0)}-01-01" if year_match else ""


def parse_feed_timestamp(value: str) -> tuple[str, str]:
    """Return a normalized timestamp plus its precision (time/day/month/year)."""
    value = clean_html(value)
    if not value:
        return "", ""
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        normalized = parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        return normalized, "time"
    except (TypeError, ValueError, OverflowError):
        pass
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if "T" in value or " " in value:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            normalized = parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            return normalized, "time"
    except ValueError:
        pass
    exact_day = re.search(r"\b((?:19|20)\d{2}-\d{2}-\d{2})\b", value)
    if exact_day:
        return exact_day.group(1), "day"
    long_day = re.search(
        r"\b(\d{1,2})\s+(" + "|".join(MONTHS) + r")\s+((?:19|20)\d{2})\b",
        value,
        re.IGNORECASE,
    )
    if long_day:
        parsed_day = date(int(long_day.group(3)), MONTHS[long_day.group(2).lower()], int(long_day.group(1)))
        return parsed_day.isoformat(), "day"
    month_year = re.search(r"\b(" + "|".join(MONTHS) + r")\s+((?:19|20)\d{2})\b", value, re.IGNORECASE)
    if month_year:
        return f"{month_year.group(2)}-{MONTHS[month_year.group(1).lower()]:02d}", "month"
    year = re.search(r"\b((?:19|20)\d{2})\b", value)
    return (year.group(1), "year") if year else ("", "")


def extract_description_metadata(description: str) -> dict[str, Any]:
    text = clean_html(description)
    result: dict[str, Any] = {"abstract": text}
    author_match = re.search(r"Author\(s\):\s*(.+)$", text, re.IGNORECASE)
    if author_match:
        result["authors"] = [part.strip() for part in author_match.group(1).split(",") if part.strip()]
        result["abstract"] = text[: author_match.start()].strip(" ;")
    publication_match = re.search(r"Publication date:\s*(.+?)(?=\s+Source:|$)", text, re.IGNORECASE)
    if publication_match:
        result["publication_text"] = publication_match.group(1)
    result["abstract"] = re.sub(
        r"^\s*Publication date:\s*.*?(?=\s+Source:|$)",
        "",
        result.get("abstract", ""),
        flags=re.IGNORECASE,
    )
    result["abstract"] = re.sub(r"^\s*Source:\s*.*$", "", result.get("abstract", ""), flags=re.IGNORECASE)
    result["abstract"] = result.get("abstract", "").strip(" ;")
    return result


def abstract_candidates(element: ET.Element) -> list[str]:
    values = all_child_text(element, "description", "summary", "abstract", "encoded")
    cleaned = []
    for value in values:
        text = clean_html(value)
        if text:
            cleaned.append(text)
    return cleaned


def select_abstract(element: ET.Element) -> dict[str, Any]:
    candidates = abstract_candidates(element)
    if not candidates:
        return {"abstract": ""}
    parsed = [extract_description_metadata(value) for value in candidates]
    selected = max(parsed, key=lambda value: len(value.get("abstract", "")))
    selected["abstract"] = clean_html(selected.get("abstract", ""))
    return selected


def abstract_needs_fallback(value: str) -> bool:
    value = clean_html(value)
    return not value or value.endswith("...") or value.endswith("…")


def abstract_is_longer(candidate: str, current: str) -> bool:
    candidate = clean_html(candidate)
    current = clean_html(current)
    return bool(candidate) and len(candidate) > len(current)


def parse_feed(xml_bytes: bytes, journal: dict[str, Any], fetched_at: str) -> list[dict[str, Any]]:
    root = ET.fromstring(xml_bytes)
    channel = next((node for node in root.iter() if local_name(node.tag) == "channel"), root)
    feed_title = child_text(channel, "title")
    feed_journal = child_text(channel, "publicationname") or journal.get("name") or feed_title
    items = [node for node in root.iter() if local_name(node.tag) in {"item", "entry"}]
    articles: list[dict[str, Any]] = []
    for item in items:
        title = clean_html(child_text(item, "title"))
        if not title:
            continue
        links = all_child_text(item, "link")
        if not links:
            links = [
                child.attrib.get("href", "")
                for child in item
                if local_name(child.tag) == "link" and child.attrib.get("href")
            ]
        link = next((value for value in links if value.startswith("http")), links[0] if links else "")
        if link.startswith("/"):
            link = urllib.parse.urljoin(journal.get("site_url") or journal["feed_url"], link)
        guid = child_text(item, "guid", "identifier")
        desc_meta = select_abstract(item)
        description = " ".join(abstract_candidates(item))
        creators = all_child_text(item, "creator", "author")
        authors = creators or desc_meta.get("authors", [])
        raw_date = child_text(item, "date", "publicationdate", "coverdate", "pubdate", "published", "updated")
        publication_text = raw_date or desc_meta.get("publication_text", "")
        feed_timestamp, date_precision = parse_feed_timestamp(publication_text)
        published = parse_date(publication_text) if date_precision == "time" else feed_timestamp
        doi = normalize_doi(child_text(item, "doi", "identifier") or guid or link or description)
        journal_name = clean_html(child_text(item, "publicationname")) or feed_journal
        stable_source = doi or guid or link or f"{journal_name}|{title}"
        article_id = hashlib.sha256(stable_source.encode("utf-8")).hexdigest()[:20]
        articles.append(
            {
                "id": article_id,
                "title": title,
                "url": link,
                "guid": guid,
                "doi": doi,
                "authors": authors,
                "abstract": desc_meta.get("abstract", ""),
                "abstract_source": "rss" if desc_meta.get("abstract") else "",
                "published": published,
                "publication_text": publication_text,
                "feed_timestamp": feed_timestamp,
                "date_precision": date_precision,
                "journal": journal_name,
                "journal_id": journal["id"],
                "publisher": journal.get("publisher", ""),
                "volume": clean_html(child_text(item, "volume")),
                "issue": clean_html(child_text(item, "number", "issue")),
                "pages": clean_html(child_text(item, "startingpage", "endingpage")),
                "type": "journal-article",
                "first_seen": fetched_at,
                "last_seen": fetched_at,
                "metadata_source": "rss",
            }
        )
    return articles


def feed_channel_metadata(xml_bytes: bytes) -> dict[str, str]:
    root = ET.fromstring(xml_bytes)
    channel = next((node for node in root.iter() if local_name(node.tag) == "channel"), root)
    raw_last_build = child_text(channel, "lastbuilddate", "updated")
    last_build, precision = parse_feed_timestamp(raw_last_build)
    return {
        "last_build_date": last_build,
        "last_build_date_raw": clean_html(raw_last_build),
        "last_build_date_precision": precision,
    }


def article_content_hash(article: dict[str, Any]) -> str:
    tracked = {
        "title": article.get("title", ""),
        "url": article.get("url", ""),
        "guid": article.get("guid", ""),
        "authors": article.get("authors", []),
        "abstract": article.get("abstract", ""),
        "publication_text": article.get("publication_text", ""),
        "journal": article.get("journal", ""),
    }
    encoded = json.dumps(tracked, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:20]


def request_bytes(url: str, user_agent: str, timeout: int = 30) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": user_agent,
            "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


class HeadMetadataParser(HTMLParser):
    ABSTRACT_META_NAMES = {
        "citation_abstract",
        "dc.description",
        "dcterms.abstract",
        "eprints.abstract",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_head = False
        self.head_ended = False
        self.in_jsonld = False
        self.jsonld_parts: list[str] = []
        self.abstracts: list[str] = []
        self.dois: list[str] = []
        self.jsonld_documents: list[Any] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        tag = tag.lower()
        attributes = {key.lower(): value or "" for key, value in attrs}
        if tag == "head":
            self.in_head = True
            return
        if not self.in_head:
            return
        if tag == "meta":
            name = attributes.get("name", "").strip().casefold()
            itemprop = attributes.get("itemprop", "").strip().casefold()
            content = clean_html(attributes.get("content", ""))
            if content and (name in self.ABSTRACT_META_NAMES or itemprop == "abstract"):
                self.abstracts.append(content)
            if content and (name in {"citation_doi", "dc.identifier", "dcterms.identifier"} or itemprop == "doi"):
                doi = normalize_doi(content)
                if doi:
                    self.dois.append(doi)
        elif tag == "script" and attributes.get("type", "").split(";", 1)[0].strip().casefold() == "application/ld+json":
            self.in_jsonld = True
            self.jsonld_parts = []

    def handle_data(self, data: str) -> None:
        if self.in_head and self.in_jsonld:
            self.jsonld_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "script" and self.in_jsonld:
            raw = "".join(self.jsonld_parts).strip()
            if raw:
                try:
                    self.jsonld_documents.append(json.loads(raw))
                except json.JSONDecodeError:
                    pass
            self.in_jsonld = False
            self.jsonld_parts = []
        elif tag == "head":
            self.in_head = False
            self.head_ended = True


def jsonld_abstracts(value: Any) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        abstract = value.get("abstract")
        if isinstance(abstract, str) and clean_html(abstract):
            found.append(clean_html(abstract))
        for child in value.values():
            found.extend(jsonld_abstracts(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(jsonld_abstracts(child))
    return found


def fetch_doi_page_abstract(doi: str, user_agent: str, timeout: int, max_bytes: int) -> str:
    metadata = fetch_page_metadata(
        f"https://doi.org/{urllib.parse.quote(normalize_doi(doi), safe='')}",
        user_agent,
        timeout,
        max_bytes,
    )
    return metadata.get("abstract", "")


def fetch_page_metadata(url: str, user_agent: str, timeout: int, max_bytes: int) -> dict[str, str]:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": user_agent,
            "Accept": "text/html, application/xhtml+xml",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        content_type = response.headers.get_content_type().casefold()
        if content_type not in {"text/html", "application/xhtml+xml"}:
            return {}
        parser = HeadMetadataParser()
        remaining = max(1, int(max_bytes))
        while remaining > 0 and not parser.head_ended:
            chunk = response.read(min(8192, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            parser.feed(chunk.decode(response.headers.get_content_charset() or "utf-8", errors="replace"))
        parser.close()
    candidates = [*parser.abstracts]
    for document in parser.jsonld_documents:
        candidates.extend(jsonld_abstracts(document))
    cleaned = [clean_html(value) for value in candidates]
    return {
        "abstract": max((value for value in cleaned if value), key=len, default=""),
        "doi": next(iter(parser.dois), ""),
    }


def crossref_date_value(message: dict[str, Any], key: str) -> tuple[str, str]:
    value = message.get(key, {})
    parts = value.get("date-parts", []) if isinstance(value, dict) else []
    if not parts or not parts[0]:
        return "", ""
    raw_parts = parts[0]
    values = list(raw_parts) + [1, 1]
    try:
        parsed = date(int(values[0]), int(values[1]), int(values[2])).isoformat()
    except (TypeError, ValueError):
        return "", ""
    precision = {1: "year", 2: "month", 3: "day"}.get(len(raw_parts), "")
    return parsed, precision


def crossref_date(message: dict[str, Any]) -> str:
    for key in ("published-online", "published-print", "published", "issued", "created"):
        parsed, _ = crossref_date_value(message, key)
        if parsed:
            return parsed
    return ""


def crossref_authors(message: dict[str, Any]) -> list[str]:
    authors = []
    for author in message.get("author", []):
        name = " ".join(part for part in (author.get("given", ""), author.get("family", "")) if part).strip()
        if not name:
            name = author.get("name", "")
        if name:
            authors.append(name)
    return authors


def crossref_record(message: dict[str, Any]) -> dict[str, Any]:
    titles = message.get("title") or []
    containers = message.get("container-title") or []
    pages = message.get("page") or message.get("article-number") or ""
    published_online, published_online_precision = crossref_date_value(message, "published-online")
    return {
        "title": clean_html(titles[0]) if titles else "",
        "doi": normalize_doi(message.get("DOI", "")),
        "url": message.get("URL", ""),
        "authors": crossref_authors(message),
        "abstract": clean_html(message.get("abstract", "")),
        "published": crossref_date(message),
        "published_online": published_online,
        "published_online_precision": published_online_precision,
        "journal": clean_html(containers[0]) if containers else "",
        "publisher": clean_html(message.get("publisher", "")),
        "volume": str(message.get("volume", "") or ""),
        "issue": str(message.get("issue", "") or ""),
        "pages": str(pages),
        "type": message.get("type", "journal-article"),
    }


@dataclass
class CrossrefClient:
    contact_email: str
    user_agent: str
    delay_seconds: float = 0.15
    timeout: int = 30
    _last_request: float = 0.0

    def _get_json(self, path: str, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        params = dict(params or {})
        if self.contact_email:
            params["mailto"] = self.contact_email
        query = urllib.parse.urlencode(params)
        url = f"https://api.crossref.org/{path}" + (f"?{query}" if query else "")
        wait = self.delay_seconds - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        payload = request_bytes(url, self.user_agent, self.timeout)
        self._last_request = time.monotonic()
        return json.loads(payload)

    def lookup(self, article: dict[str, Any], threshold: float) -> Optional[dict[str, Any]]:
        doi = normalize_doi(article.get("doi", ""))
        if doi:
            payload = self._get_json(f"works/{urllib.parse.quote(doi, safe='')}")
            return crossref_record(payload.get("message", {}))
        params: dict[str, Any] = {"query.title": article["title"], "rows": 3}
        if article.get("journal"):
            params["query.container-title"] = article["journal"]
        payload = self._get_json("works", params)
        candidates = payload.get("message", {}).get("items", [])
        wanted = normalized_text(article["title"])
        ranked = sorted(
            (
                (SequenceMatcher(None, wanted, normalized_text((item.get("title") or [""])[0])).ratio(), item)
                for item in candidates
            ),
            key=lambda pair: pair[0],
            reverse=True,
        )
        if not ranked or ranked[0][0] < threshold:
            return None
        return crossref_record(ranked[0][1])

    def journal_updates(self, issn: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
        start_utc = start.astimezone(timezone.utc).replace(microsecond=0)
        # Crossref's until filter is inclusive; subtract one second to preserve
        # this project's half-open [start, end) window.
        end_utc = (end.astimezone(timezone.utc) - timedelta(seconds=1)).replace(microsecond=0)
        # Crossref interprets these timestamps as UTC but rejects an explicit
        # trailing Z in date-filter values.
        crossref_time = lambda value: value.replace(tzinfo=None).isoformat()
        filters = ",".join(
            (
                f"issn:{issn}",
                f"from-update-date:{crossref_time(start_utc)}",
                f"until-update-date:{crossref_time(end_utc)}",
                "type:journal-article",
            )
        )
        payload = self._get_json("works", {"filter": filters, "rows": 1000})
        return payload.get("message", {}).get("items", [])

    def journal_online_first(self, issn: str, start_date: date, end_date: date) -> list[dict[str, Any]]:
        filters = ",".join(
            (
                f"from-online-pub-date:{start_date.isoformat()}",
                f"until-online-pub-date:{end_date.isoformat()}",
                "type:journal-article",
            )
        )
        path = f"journals/{urllib.parse.quote(issn, safe='')}/works"
        payload = self._get_json(path, {"filter": filters, "rows": 1000})
        return payload.get("message", {}).get("items", [])


def articles_from_crossref_updates(
    messages: Iterable[dict[str, Any]], journal: dict[str, Any], fetched_at: str
) -> list[dict[str, Any]]:
    articles = []
    for message in messages:
        metadata = crossref_record(message)
        if not metadata.get("title"):
            continue
        doi = metadata.get("doi", "")
        deposited = message.get("deposited", {}).get("date-time", "")
        feed_timestamp, date_precision = parse_feed_timestamp(deposited)
        stable_source = doi or metadata.get("url") or f"{journal['name']}|{metadata['title']}"
        article_id = hashlib.sha256(stable_source.encode("utf-8")).hexdigest()[:20]
        articles.append(
            {
                "id": article_id,
                "title": metadata.get("title", ""),
                "url": metadata.get("url", ""),
                "guid": doi,
                "doi": doi,
                "authors": metadata.get("authors", []),
                "abstract": metadata.get("abstract", ""),
                "abstract_source": "crossref" if metadata.get("abstract") else "",
                "published": metadata.get("published", ""),
                "feed_timestamp": feed_timestamp,
                "date_precision": date_precision,
                "journal": metadata.get("journal") or journal["name"],
                "journal_id": journal["id"],
                "publisher": metadata.get("publisher") or journal.get("publisher", ""),
                "volume": metadata.get("volume", ""),
                "issue": metadata.get("issue", ""),
                "pages": metadata.get("pages", ""),
                "type": metadata.get("type", "journal-article"),
                "first_seen": fetched_at,
                "last_seen": fetched_at,
                "metadata_source": "crossref-fallback",
            }
        )
    return articles


def articles_from_crossref_online_first(
    messages: Iterable[dict[str, Any]], journal: dict[str, Any], fetched_at: str
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    articles = []
    stats = {"with_issue": 0, "missing_precise_date": 0, "missing_doi_or_title": 0}
    for message in messages:
        metadata = crossref_record(message)
        if not metadata.get("title") or not metadata.get("doi"):
            stats["missing_doi_or_title"] += 1
            continue
        if metadata.get("published_online_precision") != "day":
            stats["missing_precise_date"] += 1
            continue
        if metadata.get("volume") or metadata.get("issue"):
            stats["with_issue"] += 1
            continue
        stable_source = metadata["doi"] or metadata.get("url") or f"{journal['name']}|{metadata['title']}"
        article_id = hashlib.sha256(stable_source.encode("utf-8")).hexdigest()[:20]
        online_date = metadata["published_online"]
        articles.append(
            {
                "id": article_id,
                "title": metadata["title"],
                "url": metadata.get("url", ""),
                "guid": metadata["doi"],
                "doi": metadata["doi"],
                "authors": metadata.get("authors", []),
                "abstract": metadata.get("abstract", ""),
                "abstract_source": "crossref" if metadata.get("abstract") else "",
                "published": online_date,
                "published_online": online_date,
                "published_online_source": "crossref",
                "published_online_precision": metadata["published_online_precision"],
                "publication_text": f"Published online: {online_date}",
                "feed_timestamp": online_date,
                "date_precision": metadata["published_online_precision"],
                "journal": metadata.get("journal") or journal["name"],
                "journal_id": journal["id"],
                "publisher": metadata.get("publisher") or journal.get("publisher", ""),
                "volume": metadata.get("volume", ""),
                "issue": metadata.get("issue", ""),
                "pages": metadata.get("pages", ""),
                "type": metadata.get("type", "journal-article"),
                "first_seen": fetched_at,
                "last_seen": fetched_at,
                "discovered_at": fetched_at,
                "metadata_source": "crossref-onlinefirst",
                "online_first_status": "confirmed",
            }
        )
    return articles, stats


def merge_crossref(article: dict[str, Any], metadata: dict[str, Any]) -> bool:
    abstract_replaced = False
    for key in (
        "doi",
        "authors",
        "published",
        "published_online",
        "published_online_precision",
        "journal",
        "publisher",
        "volume",
        "issue",
        "pages",
        "type",
    ):
        if not article.get(key) and metadata.get(key):
            article[key] = metadata[key]
    if abstract_needs_fallback(article.get("abstract", "")) and abstract_is_longer(
        metadata.get("abstract", ""), article.get("abstract", "")
    ):
        article["abstract"] = clean_html(metadata["abstract"])
        article["abstract_source"] = "crossref"
        abstract_replaced = True
    if authors_need_replacement(article.get("authors", [])) and metadata.get("authors"):
        article["authors"] = metadata["authors"]
    if metadata.get("doi"):
        article["doi"] = metadata["doi"]
        article["doi_url"] = f"https://doi.org/{metadata['doi']}"
    if not article.get("url") and metadata.get("url"):
        article["url"] = metadata["url"]
    article["metadata_source"] = "rss+crossref"
    return abstract_replaced


def merge_abstract(article: dict[str, Any], abstract: str, source: str) -> bool:
    current = article.get("abstract", "")
    if not abstract_needs_fallback(current) or not abstract_is_longer(abstract, current):
        return False
    article["abstract"] = clean_html(abstract)
    article["abstract_source"] = source
    return True


def authors_need_replacement(authors: list[str]) -> bool:
    if not authors:
        return False
    combined = " ".join(authors)
    lowered = combined.casefold()
    markers = (" department of ", " university", " research interests", " is a professor", " is an associate professor", " faculty of ")
    return len(combined) > 180 or any(marker in lowered for marker in markers)


def timestamp_in_window(article: dict[str, Any], start: datetime, end: datetime, zone: ZoneInfo) -> bool:
    value = article.get("feed_timestamp", "")
    precision = article.get("date_precision", "")
    if precision == "time":
        try:
            moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return False
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=zone)
        return start <= moment.astimezone(zone) < end
    if precision == "day":
        try:
            moment = datetime.combine(date.fromisoformat(value), datetime_time.min, tzinfo=zone)
        except ValueError:
            return False
        return start <= moment < end
    return False


def history_date_for_batch(batch_date: date) -> str:
    """Return the date used to group records in the daily history."""
    return batch_date.isoformat()


def score_article(article: dict[str, Any], settings: dict[str, Any]) -> tuple[float, list[dict[str, Any]]]:
    title = normalized_text(article.get("title", ""))
    details = normalized_text(" ".join((article.get("abstract", ""), " ".join(article.get("authors", [])))))
    title_multiplier = float(settings.get("title_multiplier", 2.0))
    details_multiplier = float(settings.get("details_multiplier", 1.0))
    matches: list[dict[str, Any]] = []
    total = 0.0
    for entry in settings.get("keywords", []):
        keyword = normalized_text(str(entry.get("term", "")))
        if not keyword:
            continue
        fields = []
        contribution = 0.0
        weight = float(entry.get("weight", 0))
        if keyword in title:
            fields.append("title")
            contribution += weight * title_multiplier
        if keyword in details:
            fields.append("details")
            contribution += weight * details_multiplier
        if fields:
            total += contribution
            matches.append(
                {
                    "keyword": entry["term"],
                    "weight": weight,
                    "fields": fields,
                    "contribution": round(contribution, 2),
                }
            )
    matches.sort(key=lambda item: abs(item["contribution"]), reverse=True)
    return round(total, 2), matches


def identity_keys(article: dict[str, Any]) -> list[str]:
    keys = []
    article_id = str(article.get("id", "")).strip()
    if article_id:
        keys.append(f"id:{article_id}")
    doi = normalize_doi(article.get("doi", ""))
    if doi:
        keys.append(f"doi:{doi}")
    title_key = normalized_text(article.get("title", ""))
    journal_key = normalized_text(article.get("journal", ""))
    if title_key:
        keys.append(f"title:{title_key}|{journal_key}")
        keys.append(f"title:{title_key}")
    return keys


def merge_articles(previous: Iterable[dict[str, Any]], incoming: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = [dict(article) for article in previous]
    index: dict[str, int] = {}
    for position, article in enumerate(records):
        for key in identity_keys(article):
            index.setdefault(key, position)
    for article in incoming:
        position = next((index[key] for key in identity_keys(article) if key in index), None)
        if position is None:
            position = len(records)
            records.append(dict(article))
        else:
            old = records[position]
            first_seen = old.get("first_seen") or article.get("first_seen", "")
            merged = dict(old)
            for key, value in article.items():
                if value not in (None, "", []):
                    merged[key] = value
            merged["first_seen"] = first_seen
            records[position] = merged
        for key in identity_keys(records[position]):
            index[key] = position
    return records


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def display_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _build_legacy(config_path: Path, output_dir: Path, now: Optional[datetime] = None, offline: bool = False) -> dict[str, Any]:
    config = load_json(config_path, None)
    if not isinstance(config, dict):
        raise ValueError(f"Invalid config: {config_path}")
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    run_at = display_timestamp(now)
    window_config = config.get("window", {})
    window_enabled = window_config.get("enabled", True)
    zone = ZoneInfo(window_config.get("timezone", "Asia/Shanghai"))
    local_today = now.astimezone(zone).date()
    window_end = datetime.combine(local_today, datetime_time.min, tzinfo=zone)
    window_start = window_end - timedelta(days=1)
    batch_date = window_start.date() if window_enabled else local_today
    previous_payload = load_json(output_dir / "papers.json", {"articles": []})
    previous = previous_payload.get("articles", []) if isinstance(previous_payload, dict) else []
    previous_history_payload = load_json(output_dir / "history.json", {"version": 1, "days": {}})
    history_days = previous_history_payload.get("days", {}) if isinstance(previous_history_payload, dict) else {}
    if not isinstance(history_days, dict):
        history_days = {}
    feed_state_path = output_dir / "feed-state.json"
    loaded_feed_state = load_json(feed_state_path, {"version": 1, "feeds": {}})
    feed_state_payload = loaded_feed_state if isinstance(loaded_feed_state, dict) else {"version": 1, "feeds": {}}
    if not isinstance(feed_state_payload.get("feeds"), dict):
        feed_state_payload["feeds"] = {}
    feed_state_payload["version"] = 1
    feed_state_changed = False
    user_agent = config.get("user_agent", "ScholarlyTracker/1.0")
    email = config.get("crossref", {}).get("contact_email", "").strip()
    if email and "mailto:" not in user_agent:
        user_agent = f"{user_agent} (mailto:{email})"
    crossref = CrossrefClient(
        contact_email=email,
        user_agent=user_agent,
        delay_seconds=float(config.get("crossref", {}).get("delay_seconds", 0.15)),
        timeout=int(config.get("request_timeout_seconds", 30)),
    )
    source_status = []
    fetched: list[dict[str, Any]] = []
    metadata_updates: list[dict[str, Any]] = []
    primary_crossref_attempted = 0
    previous_article_ids = {str(article.get("id", "")) for article in previous if article.get("id")}
    for journal in config.get("journals", []):
        if not journal.get("enabled", True):
            continue
        started = time.monotonic()
        discovery_mode = journal.get("discovery_mode", "publication_date")
        crossref_online_first = discovery_mode == "crossref_online_first"
        crossref_url = ""
        if journal.get("crossref_issn"):
            crossref_url = f"https://api.crossref.org/journals/{urllib.parse.quote(str(journal['crossref_issn']), safe='')}/works"
        status = {
            "id": journal["id"],
            "name": journal["name"],
            "url": crossref_url if crossref_online_first else journal.get("feed_url", journal.get("site_url", "")),
            "source": "crossref" if crossref_online_first else "rss",
            "discovery_mode": discovery_mode,
            "status": "ok",
            "received": 0,
            "items": 0,
            "outside_window": 0,
            "missing_precise_date": 0,
        }
        if crossref_online_first:
            try:
                if offline:
                    raise RuntimeError("offline mode")
                crossref_issn = str(journal.get("crossref_issn", "")).strip()
                if not crossref_issn:
                    raise ValueError(f"Missing crossref_issn for {journal['id']}")
                primary_crossref_attempted += 1
                messages = crossref.journal_online_first(crossref_issn, window_start.date(), (window_end - timedelta(seconds=1)).date())
                entries, crossref_stats = articles_from_crossref_online_first(messages, journal, run_at)
                for entry in entries:
                    entry["history_date"] = history_date_for_batch(batch_date)
                fetched.extend(entries)
                status["received"] = len(messages)
                status["items"] = len(entries)
                status["crossref_online_first"] = len(entries)
                status["with_issue"] = crossref_stats["with_issue"]
                status["missing_precise_date"] = crossref_stats["missing_precise_date"]
                status["missing_doi_or_title"] = crossref_stats["missing_doi_or_title"]
            except Exception as error:
                status["status"] = "error"
                status["error"] = f"Crossref {type(error).__name__}: {error}"
            status["duration_ms"] = round((time.monotonic() - started) * 1000)
            source_status.append(status)
            continue
        try:
            if offline:
                raise RuntimeError("offline mode")
            payload = request_bytes(journal["feed_url"], user_agent, int(config.get("request_timeout_seconds", 30)))
            entries = parse_feed(payload, journal, run_at)
            status["received"] = len(entries)
            if discovery_mode == "guid_diff":
                status["discovery_mode"] = "guid_diff"
                channel_metadata = feed_channel_metadata(payload)
                status.update(channel_metadata)
                status["imprecise_dates"] = sum(
                    1 for entry in entries if entry.get("date_precision") not in {"time", "day"}
                )
                old_feed_state = feed_state_payload["feeds"].get(journal["id"], {})
                initialized = isinstance(old_feed_state, dict) and isinstance(old_feed_state.get("seen"), dict)
                old_seen = old_feed_state.get("seen", {}) if initialized else {}
                old_present_ids = old_feed_state.get("present_ids", []) if initialized else []
                old_present = set(old_present_ids) if isinstance(old_present_ids, list) else set()
                current_ids = {entry["id"] for entry in entries}
                eligible = []
                changed_entries = []
                updated_seen = dict(old_seen)
                for entry in entries:
                    entry_id = entry["id"]
                    content_hash = article_content_hash(entry)
                    old_record = old_seen.get(entry_id, {}) if isinstance(old_seen.get(entry_id), dict) else {}
                    if initialized and entry_id not in old_seen:
                        entry["discovered_at"] = run_at
                        entry["history_date"] = history_date_for_batch(batch_date)
                        eligible.append(entry)
                    elif initialized and old_record.get("content_hash") != content_hash:
                        changed_entries.append(entry)
                    updated_seen[entry_id] = {
                        "first_seen": old_record.get("first_seen") or run_at,
                        "last_seen": run_at,
                        "content_hash": content_hash,
                    }
                metadata_updates.extend(
                    {
                        key: value
                        for key, value in entry.items()
                        if key
                        in {
                            "id",
                            "title",
                            "url",
                            "guid",
                            "authors",
                            "abstract",
                            "abstract_source",
                            "published",
                            "publication_text",
                            "feed_timestamp",
                            "date_precision",
                            "journal",
                            "journal_id",
                            "publisher",
                            "last_seen",
                        }
                    }
                    for entry in changed_entries
                    if entry.get("id") in previous_article_ids
                )
                initialized_at = old_feed_state.get("initialized_at", "") if initialized else run_at
                feed_state_payload["feeds"][journal["id"]] = {
                    "mode": "guid_diff",
                    "initialized_at": initialized_at or run_at,
                    "last_success_at": run_at,
                    "last_build_date": channel_metadata.get("last_build_date", ""),
                    "present_ids": sorted(current_ids),
                    "seen": updated_seen,
                }
                feed_state_changed = True
                status["baseline_created"] = not initialized
                status["new_items"] = len(eligible)
                status["updated_items"] = len(changed_entries)
                status["removed_items"] = len(old_present - current_ids) if initialized else 0
                status["known_items"] = len(updated_seen)
            elif discovery_mode != "publication_date":
                raise ValueError(f"Unsupported discovery_mode for {journal['id']}: {discovery_mode}")
            elif window_enabled:
                eligible = []
                for entry in entries:
                    if entry.get("date_precision") not in {"time", "day"}:
                        status["missing_precise_date"] += 1
                    elif timestamp_in_window(entry, window_start, window_end, zone):
                        entry["history_date"] = history_date_for_batch(batch_date)
                        eligible.append(entry)
                    else:
                        status["outside_window"] += 1
            else:
                eligible = entries
            fetched.extend(eligible)
            status["items"] = len(eligible)
        except Exception as error:  # Preserve old data and expose the failure on the status page.
            fallback_issn = journal.get("crossref_fallback_issn", "")
            if fallback_issn and not offline:
                try:
                    messages = crossref.journal_updates(fallback_issn, window_start, window_end)
                    entries = articles_from_crossref_updates(messages, journal, run_at)
                    for entry in entries:
                        entry["history_date"] = history_date_for_batch(batch_date)
                    fetched.extend(entries)
                    status["status"] = "fallback"
                    status["fallback"] = "crossref"
                    status["rss_error"] = f"{type(error).__name__}: {error}"
                    status["items"] = len(entries)
                except Exception as fallback_error:
                    status["status"] = "error"
                    status["error"] = (
                        f"RSS {type(error).__name__}: {error}; "
                        f"Crossref fallback {type(fallback_error).__name__}: {fallback_error}"
                    )
            else:
                status["status"] = "error"
                status["error"] = f"{type(error).__name__}: {error}"
        status["duration_ms"] = round((time.monotonic() - started) * 1000)
        source_status.append(status)

    previous_keys = {key for article in previous for key in identity_keys(article)}
    seen_incoming: set[str] = set()
    new_count = 0
    for article in fetched:
        keys = identity_keys(article)
        if not any(key in previous_keys or key in seen_incoming for key in keys):
            new_count += 1
        seen_incoming.update(keys)
    processed_keys = {key for article in fetched for key in identity_keys(article)}
    articles = merge_articles(previous, [*metadata_updates, *fetched])
    processed_articles = [article for article in articles if any(key in processed_keys for key in identity_keys(article))]
    keyword_settings = config.get("ranking", {})
    for article in processed_articles:
        article["score"], article["matched_keywords"] = score_article(article, keyword_settings)

    crossref_status = {
        "attempted": 0,
        "matched": 0,
        "not_found": 0,
        "errors": 0,
        "skipped": 0,
        "primary_attempted": primary_crossref_attempted,
    }
    abstract_status = {
        "crossref_attempted": 0,
        "crossref_replaced": 0,
        "doi_page_attempted": 0,
        "doi_page_replaced": 0,
        "doi_page_unavailable": 0,
        "doi_page_errors": 0,
    }
    crossref_attempted_ids: set[str] = set()
    crossref_config = config.get("crossref", {})
    if crossref_config.get("enabled", True) and not offline:
        limit = int(crossref_config.get("max_lookups_per_run", 60))
        threshold = float(crossref_config.get("title_match_threshold", 0.86))
        abstract_candidates = [
            article
            for article in processed_articles
            if abstract_needs_fallback(article.get("abstract", "")) and article.get("doi")
        ]
        abstract_candidate_ids = {str(article.get("id", "")) for article in abstract_candidates}
        crossref_attempted_ids.update(
            str(article.get("id", ""))
            for article in abstract_candidates
            if article.get("metadata_source") in {"crossref-fallback", "crossref-onlinefirst"}
        )
        # Only enrich records from this window. Missing DOI is highest priority,
        # followed by malformed feed author strings and other incomplete fields.
        candidates = [
            article
            for article in processed_articles
            if article.get("metadata_source") not in {"crossref-fallback", "crossref-onlinefirst"}
            and (
                not article.get("doi")
                or not article.get("abstract")
                or not article.get("authors")
                or authors_need_replacement(article.get("authors", []))
            )
        ]
        candidates.sort(
            key=lambda article: (
                1 if not article.get("doi") else 0,
                1 if authors_need_replacement(article.get("authors", [])) else 0,
                article.get("score", 0),
            ),
            reverse=True,
        )
        for article in candidates:
            if crossref_status["attempted"] >= limit:
                crossref_status["skipped"] += 1
                continue
            crossref_status["attempted"] += 1
            crossref_attempted_ids.add(str(article.get("id", "")))
            if str(article.get("id", "")) in abstract_candidate_ids:
                abstract_status["crossref_attempted"] += 1
            try:
                metadata = crossref.lookup(article, threshold)
                if metadata:
                    if merge_crossref(article, metadata):
                        abstract_status["crossref_replaced"] += 1
                    crossref_status["matched"] += 1
                else:
                    crossref_status["not_found"] += 1
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError, ValueError) as error:
                crossref_status["errors"] += 1
                article["crossref_error"] = f"{type(error).__name__}: {error}"

        for article in abstract_candidates:
            article_id = str(article.get("id", ""))
            if article_id in crossref_attempted_ids:
                continue
            if crossref_status["attempted"] >= limit:
                continue
            crossref_status["attempted"] += 1
            crossref_attempted_ids.add(article_id)
            abstract_status["crossref_attempted"] += 1
            try:
                metadata = crossref.lookup(article, threshold)
                if metadata:
                    crossref_status["matched"] += 1
                    if merge_abstract(article, metadata.get("abstract", ""), "crossref"):
                        abstract_status["crossref_replaced"] += 1
                    else:
                        # A Crossref record can exist without an abstract, or can
                        # still contain a truncated value. Let DOI-page fallback decide next.
                        pass
                else:
                    crossref_status["not_found"] += 1
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError, ValueError) as error:
                crossref_status["errors"] += 1
                article["crossref_error"] = f"{type(error).__name__}: {error}"

        doi_page_enabled = crossref_config.get("doi_page_enabled", False)
        doi_page_limit = int(crossref_config.get("max_doi_page_lookups_per_run", 20))
        doi_page_max_bytes = int(crossref_config.get("doi_page_max_bytes", 524288))
        if doi_page_enabled:
            for article in abstract_candidates:
                if not abstract_needs_fallback(article.get("abstract", "")):
                    continue
                if abstract_status["doi_page_attempted"] >= doi_page_limit:
                    break
                abstract_status["doi_page_attempted"] += 1
                try:
                    page_abstract = fetch_doi_page_abstract(
                        article["doi"], user_agent, int(config.get("request_timeout_seconds", 30)), doi_page_max_bytes
                    )
                    if merge_abstract(article, page_abstract, "doi-page"):
                        abstract_status["doi_page_replaced"] += 1
                    else:
                        abstract_status["doi_page_unavailable"] += 1
                except (urllib.error.URLError, TimeoutError, UnicodeError, ValueError) as error:
                    abstract_status["doi_page_errors"] += 1
                    article["doi_page_error"] = f"{type(error).__name__}: {error}"

    for article in processed_articles:
        article["doi"] = normalize_doi(article.get("doi", ""))
        if article["doi"]:
            article["doi_url"] = f"https://doi.org/{article['doi']}"
        article["score"], article["matched_keywords"] = score_article(article, keyword_settings)
    articles.sort(
        key=lambda article: (
            article.get("published") or "0000-00-00",
            article.get("first_seen") or "",
            article.get("score", 0),
        ),
        reverse=True,
    )
    articles = articles[: int(config.get("max_articles", 2000))]
    recommendation_config = config.get("recommendations", {})
    processed_articles = [article for article in articles if any(key in processed_keys for key in identity_keys(article))]
    recommended = [article for article in processed_articles if article.get("score", 0) >= float(recommendation_config.get("minimum_score", 1))]
    recommended.sort(key=lambda article: (article.get("score", 0), article.get("published", "")), reverse=True)
    recommended = recommended[: int(recommendation_config.get("limit", 12))]
    recommended_ids = {str(article.get("id", "")) for article in recommended}
    other_articles = [
        article for article in processed_articles if str(article.get("id", "")) not in recommended_ids
    ]
    other_articles.sort(key=lambda article: (article.get("score", 0), article.get("published", "")), reverse=True)
    failures = sum(1 for source in source_status if source["status"] == "error")
    if source_status and failures == len(source_status):
        outcome = "error" if not articles else "stale"
    elif failures or crossref_status["errors"]:
        outcome = "partial"
    else:
        outcome = "success"
    previous_status = load_json(output_dir / "status.json", {})
    status_payload = {
        "generated_at": run_at,
        "outcome": outcome,
        "last_success_at": run_at if outcome == "success" else previous_status.get("last_success_at", ""),
        "window": {
            "timezone": str(zone),
            "start": window_start.isoformat(),
            "end": window_end.isoformat(),
        },
        "feeds": source_status,
        "crossref": crossref_status,
        "abstracts": abstract_status,
        "counts": {
            "fetched_this_run": len(fetched),
            "processed_this_run": len(fetched),
            "new_today": new_count,
            "items_in_window": len(fetched),
            "recommended_today": len(recommended),
            "other_today": len(other_articles),
            "all_articles": len(articles),
        },
    }
    write_json(output_dir / "papers.json", {"generated_at": run_at, "articles": articles})
    write_json(
        output_dir / "recommendations.json",
        {
            "generated_at": run_at,
            "date": local_today.isoformat(),
            "window": status_payload["window"],
            "articles": recommended,
            "other_articles": other_articles,
        },
    )
    write_json(output_dir / "status.json", status_payload)
    if outcome in {"success", "partial"}:
        date_key = history_date_for_batch(batch_date)
        old_day = history_days.get(date_key, {})
        old_ids = old_day.get("article_ids", []) if isinstance(old_day, dict) else []
        if not isinstance(old_ids, list):
            old_ids = []
        batch_ids = [str(article.get("id", "")) for article in fetched if article.get("id")]
        history_days[date_key] = {
            "generated_at": run_at,
            "article_ids": sorted(set(old_ids) | set(batch_ids)),
        }
        write_json(
            output_dir / "history.json",
            {"version": 1, "generated_at": run_at, "days": history_days},
        )
    if feed_state_changed or feed_state_path.exists():
        if feed_state_changed:
            feed_state_payload["updated_at"] = run_at
        write_json(feed_state_path, feed_state_payload)
    return status_payload


def _trusted_article_url(url: str, publisher: str) -> bool:
    parsed = urllib.parse.urlparse(url or "")
    if parsed.scheme != "https" or not parsed.hostname:
        return False
    host = parsed.hostname.casefold()
    allowed = {
        "Elsevier": ("elsevier.com", "sciencedirect.com"),
        "SAGE": ("sagepub.com",),
        "Taylor & Francis": ("tandfonline.com",),
        "Wiley": ("wiley.com",),
        "Nature": ("nature.com", "springernature.com"),
    }.get(publisher, ())
    return any(host == domain or host.endswith("." + domain) for domain in allowed)


def lookup_openalex(article: dict[str, Any], user_agent: str, timeout: int, threshold: float = 0.92) -> Optional[dict[str, Any]]:
    doi = normalize_doi(article.get("doi", ""))
    if doi:
        url = "https://api.openalex.org/works/" + urllib.parse.quote("https://doi.org/" + doi, safe="")
    else:
        url = "https://api.openalex.org/works?" + urllib.parse.urlencode({"search": article["title"], "per-page": 5})
    payload = json.loads(request_bytes(url, user_agent, timeout))
    candidates = [payload] if doi else payload.get("results", [])
    wanted = normalized_text(article["title"])
    candidates = sorted(candidates, key=lambda item: SequenceMatcher(None, wanted, normalized_text(item.get("title") or "")).ratio(), reverse=True)
    for item in candidates:
        candidate_doi = normalize_doi(item.get("doi") or "")
        if doi:
            if candidate_doi != doi:
                continue
        elif SequenceMatcher(None, wanted, normalized_text(item.get("title") or "")).ratio() < threshold:
            continue
        source = ((item.get("primary_location") or {}).get("source") or {}).get("display_name", "")
        if not doi and source and article.get("journal") and normalized_text(source) != normalized_text(article["journal"]):
            continue
        positions = {}
        for word, offsets in (item.get("abstract_inverted_index") or {}).items():
            for offset in offsets:
                if isinstance(offset, int) and offset >= 0:
                    positions[offset] = word
        return {
            "doi": candidate_doi,
            "authors": [entry["author"]["display_name"] for entry in item.get("authorships", []) if (entry.get("author") or {}).get("display_name")],
            "abstract": " ".join(positions[index] for index in sorted(positions)),
        }
    return None


def enrich_email_metadata(articles: list[dict[str, Any]], config: dict[str, Any]) -> dict[str, Any]:
    cfg = config.get("metadata_fallback", {})
    status = {"attempted": 0, "matched": 0, "abstracts_replaced": 0, "errors": 0}
    if not cfg.get("enabled", False):
        return status
    user_agent = str(config.get("user_agent", "ScholarlyTracker/2.0"))
    timeout = int(config.get("request_timeout_seconds", 30))
    client = CrossrefClient(contact_email="", user_agent=user_agent, timeout=timeout)
    for article in sorted(articles, key=lambda value: value.get("score", 0), reverse=True):
        if status["attempted"] >= int(cfg.get("max_lookups_per_run", 20)):
            break
        if article.get("authors") and article.get("doi") and not abstract_needs_fallback(article.get("abstract", "")):
            continue
        status["attempted"] += 1
        for source in ("crossref", "openalex"):
            try:
                metadata = client.lookup(article, 0.92) if source == "crossref" else lookup_openalex(article, user_agent, timeout)
                if not metadata:
                    continue
                # Title searches require the same journal when the service supplies it.
                if source == "crossref" and not article.get("doi") and metadata.get("journal") and normalized_text(metadata["journal"]) != normalized_text(article.get("journal", "")):
                    continue
                status["matched"] += 1
                for field in ("doi", "authors", "published", "volume", "issue", "pages"):
                    if not article.get(field) and metadata.get(field):
                        article[field] = metadata[field]
                if merge_abstract(article, metadata.get("abstract", ""), source):
                    status["abstracts_replaced"] += 1
                article["metadata_source"] = article.get("metadata_source", "email") + "+" + source
                if article.get("authors") and not abstract_needs_fallback(article.get("abstract", "")):
                    break
            except (urllib.error.URLError, TimeoutError, UnicodeError, ValueError, OSError):
                status["errors"] += 1
    return status


def _email_build(config_path: Path, output_dir: Path, now: Optional[datetime], offline: bool) -> dict[str, Any]:
    config = load_json(config_path, {})
    if not isinstance(config, dict):
        raise ValueError("Configuration must be a JSON object")
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    run_at = display_timestamp(now)
    window_config = config.get("window", {})
    zone = ZoneInfo(window_config.get("timezone", "Asia/Shanghai"))
    local_today = now.astimezone(zone).date()
    window_end = datetime.combine(local_today, datetime_time.min, tzinfo=zone)
    window_start = window_end - timedelta(days=1)
    batch_date = window_start.date()
    previous_payload = load_json(output_dir / "papers.json", {"articles": []})
    previous = previous_payload.get("articles", []) if isinstance(previous_payload, dict) else []
    previous_history = load_json(output_dir / "history.json", {"version": 1, "days": {}})
    history_days = previous_history.get("days", {}) if isinstance(previous_history, dict) else {}
    if not isinstance(history_days, dict):
        history_days = {}

    mail_cfg = config.get("mail", {}) if isinstance(config.get("mail", {}), dict) else {}
    email_status: dict[str, Any] = {
        "status": "ok",
        "host": str(mail_cfg.get("host", "imap.gmail.com")),
        "folders": [],
        "candidate_count": 0,
        "messages_in_window": 0,
        "duplicates": 0,
        "recognized_alerts": 0,
        "unrecognized": 0,
        "empty_alerts": 0,
        "parser_errors": 0,
    }
    parser_stats: dict[str, Any] = {}
    fetched: list[dict[str, Any]] = []
    fetch_error = ""
    if offline:
        email_status["status"] = "offline"
    else:
        username = os.environ.get("GMAIL_USERNAME", "").strip()
        app_password = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
        if not username or not app_password:
            raise RuntimeError("GMAIL_USERNAME and GMAIL_APP_PASSWORD Actions secrets are required")
        try:
            messages, fetch_stats = fetch_messages(
                username,
                app_password,
                window_start.astimezone(timezone.utc),
                window_end.astimezone(timezone.utc),
                host=str(mail_cfg.get("host", "imap.gmail.com")),
                port=int(mail_cfg.get("port", 993)),
                max_messages=int(mail_cfg.get("max_messages", 500)),
            )
            email_status.update(
                {
                    "folders": fetch_stats.get("folders", []),
                    "candidate_count": fetch_stats.get("candidate_count", 0),
                    "duplicates": fetch_stats.get("duplicate_count", 0),
                    "messages_in_window": len(messages),
                }
            )
            fetched, parsed_stats = parse_messages(messages)
            parser_stats = parsed_stats.get("parsers", {})
            email_status.update(
                {
                    "recognized_alerts": parsed_stats.get("recognized", 0),
                    "unrecognized": parsed_stats.get("unrecognized", 0),
                    "empty_alerts": parsed_stats.get("empty", 0),
                    "parser_errors": parsed_stats.get("errors", 0),
                }
            )
            if any(folder.get("status") == "error" for folder in email_status["folders"]):
                email_status["status"] = "partial"
        except Exception as error:
            fetch_error = f"{type(error).__name__}: {error}"
            email_status["status"] = "error"

    if not offline and not fetch_error:
        previous = clean_legacy_email_articles(previous)

    previous_keys = {key for article in previous for key in identity_keys(article)}
    seen_incoming: set[str] = set()
    new_count = 0
    for article in fetched:
        keys = identity_keys(article)
        if not any(key in previous_keys or key in seen_incoming for key in keys):
            new_count += 1
        seen_incoming.update(keys)
        article["history_date"] = batch_date.isoformat()
    processed_keys = {key for article in fetched for key in identity_keys(article)}
    articles = merge_articles(previous, fetched)
    processed_articles = [article for article in articles if any(key in processed_keys for key in identity_keys(article))]
    ranking = config.get("ranking", {})
    for article in processed_articles:
        article["score"], article["matched_keywords"] = score_article(article, ranking)

    metadata_status = enrich_email_metadata(processed_articles, config) if not offline and not fetch_error else {"attempted": 0, "matched": 0, "abstracts_replaced": 0, "errors": 0}
    for article in processed_articles:
        article["score"], article["matched_keywords"] = score_article(article, ranking)

    abstract_cfg = config.get("doi_page", {}) if isinstance(config.get("doi_page", {}), dict) else {}
    abstract_status = {
        "attempted": 0,
        "replaced": 0,
        "doi_discovered": 0,
        "unavailable": 0,
        "errors": 0,
        "skipped_low_score": sum(1 for article in processed_articles if article.get("score", 0) < float(config.get("recommendations", {}).get("minimum_score", 1))),
    }
    abstract_status["metadata_fallback"] = metadata_status
    if not offline and not fetch_error and abstract_cfg.get("enabled", True):
        limit = int(abstract_cfg.get("max_lookups_per_run", 20))
        max_bytes = int(abstract_cfg.get("max_bytes", 524288))
        minimum_score = float(config.get("recommendations", {}).get("minimum_score", 1))
        for article in sorted(processed_articles, key=lambda value: value.get("score", 0), reverse=True):
            if article.get("score", 0) < minimum_score or abstract_status["attempted"] >= limit:
                continue
            if not abstract_needs_fallback(article.get("abstract", "")):
                continue
            publisher = str(article.get("publisher", ""))
            target = article.get("doi") and f"https://doi.org/{urllib.parse.quote(article['doi'], safe='')}" or article.get("url", "")
            if not article.get("doi") and not _trusted_article_url(target, publisher):
                abstract_status["unavailable"] += 1
                continue
            abstract_status["attempted"] += 1
            try:
                metadata = fetch_page_metadata(target, str(config.get("user_agent", "ScholarlyTracker/1.0")), int(config.get("request_timeout_seconds", 30)), max_bytes)
                if not article.get("doi") and metadata.get("doi"):
                    article["doi"] = metadata["doi"]
                    article["guid"] = metadata["doi"]
                    article["doi_url"] = f"https://doi.org/{metadata['doi']}"
                    abstract_status["doi_discovered"] += 1
                if merge_abstract(article, metadata.get("abstract", ""), "doi-page"):
                    abstract_status["replaced"] += 1
                elif not metadata.get("abstract"):
                    abstract_status["unavailable"] += 1
            except (urllib.error.URLError, TimeoutError, UnicodeError, ValueError, OSError):
                abstract_status["errors"] += 1

    for article in processed_articles:
        article["doi"] = normalize_doi(article.get("doi", ""))
        if article["doi"]:
            article["doi_url"] = f"https://doi.org/{article['doi']}"
        article["score"], article["matched_keywords"] = score_article(article, ranking)
    articles.sort(key=lambda article: (article.get("published") or "", article.get("feed_timestamp") or "", article.get("score", 0)), reverse=True)
    articles = articles[: int(config.get("max_articles", 2000))]
    processed_articles = [article for article in articles if any(key in processed_keys for key in identity_keys(article))]
    recommendation_cfg = config.get("recommendations", {})
    minimum_score = float(recommendation_cfg.get("minimum_score", 1))
    recommended = [article for article in processed_articles if article.get("score", 0) >= minimum_score]
    recommended.sort(key=lambda article: (article.get("score", 0), article.get("feed_timestamp", "")), reverse=True)
    recommended = recommended[: int(recommendation_cfg.get("limit", 12))]
    recommended_ids = {str(article.get("id", "")) for article in recommended}
    other_articles = [article for article in processed_articles if str(article.get("id", "")) not in recommended_ids]
    other_articles.sort(key=lambda article: (article.get("score", 0), article.get("feed_timestamp", "")), reverse=True)

    if fetch_error:
        outcome = "stale" if previous else "error"
    elif email_status["status"] == "partial" or email_status["parser_errors"] or abstract_status["errors"] or metadata_status["errors"]:
        outcome = "partial"
    else:
        outcome = "success" if not offline else "stale"
    previous_status = load_json(output_dir / "status.json", {})
    status_payload = {
        "generated_at": run_at,
        "outcome": outcome,
        "last_success_at": run_at if outcome == "success" else previous_status.get("last_success_at", ""),
        "window": {"timezone": str(zone), "start": window_start.isoformat(), "end": window_end.isoformat()},
        "email": email_status,
        "parsers": [{"name": name, "articles": count} for name, count in sorted(parser_stats.items())],
        "abstracts": abstract_status,
        "counts": {
            "fetched_this_run": len(fetched),
            "processed_this_run": len(fetched),
            "new_today": new_count,
            "items_in_window": len(fetched),
            "recommended_today": len(recommended),
            "other_today": len(other_articles),
            "all_articles": len(articles),
        },
    }
    if fetch_error:
        status_payload["email"]["error"] = fetch_error
        write_json(output_dir / "status.json", status_payload)
        return status_payload
    write_json(output_dir / "papers.json", {"generated_at": run_at, "articles": articles})
    write_json(output_dir / "recommendations.json", {"generated_at": run_at, "date": batch_date.isoformat(), "window": status_payload["window"], "articles": recommended, "other_articles": other_articles})
    write_json(output_dir / "status.json", status_payload)
    if outcome in {"success", "partial", "stale"} and not offline:
        valid_ids = {str(article.get("id", "")) for article in articles}
        for day in history_days.values():
            if isinstance(day, dict) and isinstance(day.get("article_ids"), list):
                day["article_ids"] = [item for item in day["article_ids"] if item in valid_ids]
        old_day = history_days.get(batch_date.isoformat(), {})
        old_ids = old_day.get("article_ids", []) if isinstance(old_day, dict) else []
        batch_ids = [str(article.get("id", "")) for article in fetched if article.get("id")]
        history_days[batch_date.isoformat()] = {"generated_at": run_at, "article_ids": sorted(set(old_ids if isinstance(old_ids, list) else []) | set(batch_ids))}
        write_json(output_dir / "history.json", {"version": 1, "generated_at": run_at, "days": history_days})
    return status_payload


def build(config_path: Path, output_dir: Path, now: Optional[datetime] = None, offline: bool = False) -> dict[str, Any]:
    """Build using Gmail when the config contains ``mail``.

    The legacy branch exists only so old local fixtures remain testable; the
    repository configuration uses the email branch exclusively.
    """
    config = load_json(config_path, {})
    if isinstance(config, dict) and "mail" in config:
        return _email_build(config_path, output_dir, now, offline)
    return _build_legacy(config_path, output_dir, now, offline)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config/journals.json"))
    parser.add_argument("--output", type=Path, default=Path("docs/data"))
    parser.add_argument("--offline", action="store_true", help="Keep existing data and generate a stale status without network access")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        status = build(args.config, args.output, offline=args.offline)
    except Exception as error:
        print(f"Build failed: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print(json.dumps(status, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
