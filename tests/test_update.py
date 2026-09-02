from __future__ import annotations

import json
import tempfile
import unittest
import urllib.error
from datetime import datetime, timezone
from email.message import Message
from pathlib import Path
from unittest.mock import patch

from scripts.update import (
    CrossrefClient,
    authors_need_replacement,
    build,
    fetch_doi_page_abstract,
    merge_articles,
    merge_crossref,
    parse_feed,
    score_article,
)


ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"


def sciencedirect_feed(include_new: bool = False, new_title: str = "New monthly article") -> bytes:
    new_item = f"""
    <item>
      <title>{new_title}</title>
      <description><![CDATA[<p>Publication date: December 2026</p><p><b>Source:</b> Test Journal</p><p>Author(s): New Author</p>]]></description>
      <link>https://example.org/article/pii/NEW</link>
      <guid>pii-new</guid>
    </item>
    """ if include_new else ""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel>
      <title>ScienceDirect Publication: Test Journal</title>
      <lastBuildDate>Tue, 01 Sep 2026 09:53:19 GMT</lastBuildDate>
      <item>
        <title>Baseline monthly article</title>
        <description><![CDATA[<p>Publication date: September 2026</p><p><b>Source:</b> Test Journal</p><p>Author(s): Base Author</p>]]></description>
        <link>https://example.org/article/pii/BASE</link>
        <guid>pii-base</guid>
      </item>
      {new_item}
    </channel></rss>""".encode("utf-8")


class FeedParserTests(unittest.TestCase):
    def test_parses_rss2_description_metadata(self) -> None:
        journal = {"id": "test", "name": "Test Journal", "publisher": "Elsevier", "feed_url": "https://example.org/rss"}
        articles = parse_feed((FIXTURES / "rss2.xml").read_bytes(), journal, "2026-08-31T00:00:00Z")
        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0]["published"], "2026-08-22")
        self.assertEqual(articles[0]["authors"], ["Mei Lin", "Alex Smith"])
        self.assertEqual(articles[0]["journal"], "Test Journal")

    def test_parses_rdf_rss_and_relative_link(self) -> None:
        journal = {
            "id": "test",
            "name": "Fallback Journal",
            "publisher": "Taylor & Francis",
            "feed_url": "https://www.tandfonline.com/feed/rss/test",
            "site_url": "https://www.tandfonline.com",
        }
        articles = parse_feed((FIXTURES / "rss1.xml").read_bytes(), journal, "2026-08-31T00:00:00Z")
        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0]["doi"], "10.1080/02602938.2026.1234567")
        self.assertEqual(articles[0]["url"], "https://www.tandfonline.com/doi/full/10.1080/02602938.2026.1234567")
        self.assertEqual(articles[0]["authors"], ["Jane Doe"])

    def test_prefers_longest_abstract_field_and_reads_nested_xml(self) -> None:
        journal = {"id": "test", "name": "Test Journal", "feed_url": "https://example.org/rss"}
        payload = b"""<?xml version="1.0"?>
        <rss xmlns:content="http://purl.org/rss/1.0/modules/content/"><channel><item>
          <title>Nested abstract</title>
          <description>Short abstract...</description>
          <content:encoded><![CDATA[<p>Complete abstract text.</p> <p>Second paragraph.</p>]]></content:encoded>
          <pubDate>Sat, 22 Aug 2026 00:00:00 GMT</pubDate>
        </item></channel></rss>"""
        articles = parse_feed(payload, journal, "2026-08-31T00:00:00Z")
        self.assertEqual(articles[0]["abstract"], "Complete abstract text. Second paragraph.")
        self.assertEqual(articles[0]["abstract_source"], "rss")

    def test_metadata_only_description_is_not_saved_as_abstract(self) -> None:
        journal = {"id": "test", "name": "Test Journal", "feed_url": "https://example.org/rss"}
        articles = parse_feed(sciencedirect_feed(), journal, "2026-08-31T00:00:00Z")
        self.assertEqual(articles[0]["abstract"], "")
        self.assertEqual(articles[0]["abstract_source"], "")


class RankingTests(unittest.TestCase):
    def test_weighted_title_and_details_scoring_is_explainable(self) -> None:
        article = {"title": "Feedback in L2 writing", "abstract": "An assessment study", "authors": []}
        settings = {
            "title_multiplier": 2,
            "details_multiplier": 1,
            "keywords": [
                {"term": "L2 writing", "weight": 5},
                {"term": "feedback", "weight": 2},
                {"term": "assessment", "weight": 3},
            ],
        }
        score, matches = score_article(article, settings)
        self.assertEqual(score, 17)
        self.assertEqual({item["keyword"] for item in matches}, {"L2 writing", "feedback", "assessment"})

    def test_merge_preserves_first_seen(self) -> None:
        old = {"id": "old", "title": "Same title", "journal": "J", "first_seen": "2026-08-01T00:00:00Z", "last_seen": "2026-08-01T00:00:00Z"}
        new = {"id": "new", "title": "Same title", "journal": "J", "first_seen": "2026-08-31T00:00:00Z", "last_seen": "2026-08-31T00:00:00Z", "doi": "10.1234/test"}
        merged = merge_articles([old], [new])
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["first_seen"], "2026-08-01T00:00:00Z")
        self.assertEqual(merged[0]["doi"], "10.1234/test")

    def test_detects_biography_in_author_field(self) -> None:
        value = ["Jane Doe Department of Education, Example University. Jane is a professor whose research interests include assessment."]
        self.assertTrue(authors_need_replacement(value))
        self.assertFalse(authors_need_replacement(["Jane Doe", "Li Ming"]))

    def test_crossref_replaces_only_truncated_abstract_with_longer_value(self) -> None:
        article = {"abstract": "Short abstract...", "metadata_source": "rss"}
        self.assertTrue(merge_crossref(article, {"abstract": "A complete Crossref abstract."}))
        self.assertEqual(article["abstract_source"], "crossref")

        complete = {"abstract": "Already complete.", "metadata_source": "rss"}
        self.assertFalse(merge_crossref(complete, {"abstract": "A much longer value."}))
        self.assertEqual(complete["abstract"], "Already complete.")


class DoiPageTests(unittest.TestCase):
    class FakeResponse:
        def __init__(self, payload: bytes) -> None:
            self.payload = payload
            self.headers = Message()
            self.headers["Content-Type"] = "text/html; charset=utf-8"

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

        def read(self, size: int = -1) -> bytes:
            if size < 0:
                value, self.payload = self.payload, b""
                return value
            value, self.payload = self.payload[:size], self.payload[size:]
            return value

    def test_reads_head_abstract_but_ignores_body(self) -> None:
        payload = b"""<html><head>
          <meta name="citation_abstract" content="Public abstract from DOI page.">
          <script type="application/ld+json">{"@type":"ScholarlyArticle","abstract":"JSON abstract."}</script>
        </head><body><article>Full text must not be selected.</article></body></html>"""
        with patch("scripts.update.urllib.request.urlopen", return_value=self.FakeResponse(payload)):
            abstract = fetch_doi_page_abstract("10.1234/example", "test", 5, 10000)
        self.assertEqual(abstract, "Public abstract from DOI page.")


class CrossrefClientTests(unittest.TestCase):
    def test_update_window_uses_crossref_timestamp_format_without_z(self) -> None:
        client = CrossrefClient(contact_email="", user_agent="test")
        start = datetime(2026, 9, 1, tzinfo=timezone.utc)
        end = datetime(2026, 9, 2, tzinfo=timezone.utc)
        with patch.object(client, "_get_json", return_value={"message": {"items": []}}) as get_json:
            client.journal_updates("0260-2938", start, end)
        filters = get_json.call_args.args[1]["filter"]
        self.assertIn("from-update-date:2026-09-01T00:00:00", filters)
        self.assertIn("until-update-date:2026-09-01T23:59:59", filters)
        self.assertNotIn("Z", filters)


class BuildTests(unittest.TestCase):
    def test_guid_diff_baselines_then_processes_only_unseen_items(self) -> None:
        config = {
            "journals": [
                {
                    "id": "test",
                    "name": "Test Journal",
                    "publisher": "Elsevier",
                    "feed_url": "https://example.org/rss",
                    "discovery_mode": "guid_diff",
                }
            ],
            "window": {"enabled": True, "timezone": "Asia/Shanghai"},
            "crossref": {"enabled": False},
            "ranking": {"keywords": [{"term": "monthly", "weight": 4}]},
            "recommendations": {"minimum_score": 1},
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            output = root / "data"

            with patch("scripts.update.request_bytes", return_value=sciencedirect_feed()):
                baseline = build(config_path, output, now=datetime(2026, 9, 2, 1, tzinfo=timezone.utc))
            self.assertTrue(baseline["feeds"][0]["baseline_created"])
            self.assertEqual(baseline["feeds"][0]["known_items"], 1)
            self.assertEqual(baseline["counts"]["items_in_window"], 0)
            self.assertEqual(baseline["counts"]["all_articles"], 0)

            with patch("scripts.update.request_bytes", return_value=sciencedirect_feed(include_new=True)):
                added = build(config_path, output, now=datetime(2026, 9, 3, 1, tzinfo=timezone.utc))
            self.assertFalse(added["feeds"][0]["baseline_created"])
            self.assertEqual(added["feeds"][0]["new_items"], 1)
            self.assertEqual(added["feeds"][0]["imprecise_dates"], 2)
            self.assertEqual(added["counts"]["items_in_window"], 1)
            self.assertEqual(added["counts"]["all_articles"], 1)
            article = json.loads((output / "papers.json").read_text(encoding="utf-8"))["articles"][0]
            self.assertEqual(article["published"], "2026-12")
            self.assertEqual(article["date_precision"], "month")
            self.assertEqual(article["publication_text"], "December 2026")
            self.assertEqual(article["discovered_at"], "2026-09-03T01:00:00Z")
            history = json.loads((output / "history.json").read_text(encoding="utf-8"))
            self.assertEqual(history["days"]["2026-09-02"]["article_ids"], [article["id"]])
            self.assertIn("2026-09-01", history["days"])

            with patch(
                "scripts.update.request_bytes",
                return_value=sciencedirect_feed(include_new=True, new_title="Revised monthly article"),
            ):
                revised = build(config_path, output, now=datetime(2026, 9, 4, 1, tzinfo=timezone.utc))
            self.assertEqual(revised["feeds"][0]["new_items"], 0)
            self.assertEqual(revised["feeds"][0]["updated_items"], 1)
            revised_article = json.loads((output / "papers.json").read_text(encoding="utf-8"))["articles"][0]
            self.assertEqual(revised_article["title"], "Revised monthly article")

            with patch("scripts.update.request_bytes", return_value=sciencedirect_feed()):
                removed = build(config_path, output, now=datetime(2026, 9, 5, 1, tzinfo=timezone.utc))
            self.assertEqual(removed["feeds"][0]["new_items"], 0)
            self.assertEqual(removed["feeds"][0]["removed_items"], 1)
            self.assertEqual(removed["feeds"][0]["known_items"], 2)
            state = json.loads((output / "feed-state.json").read_text(encoding="utf-8"))
            self.assertEqual(len(state["feeds"]["test"]["seen"]), 2)

            saved_state = (output / "feed-state.json").read_text(encoding="utf-8")
            with patch("scripts.update.request_bytes", side_effect=urllib.error.URLError("temporary failure")):
                failed = build(config_path, output, now=datetime(2026, 9, 6, 1, tzinfo=timezone.utc))
            self.assertEqual(failed["feeds"][0]["status"], "error")
            self.assertEqual((output / "feed-state.json").read_text(encoding="utf-8"), saved_state)

    def test_build_writes_history_index_with_the_daily_batch(self) -> None:
        config = {
            "journals": [{"id": "test", "name": "Test Journal", "publisher": "Test", "feed_url": "https://example.org/rss"}],
            "crossref": {"enabled": True, "max_lookups_per_run": 5, "title_match_threshold": 0.8},
            "ranking": {"title_multiplier": 2, "details_multiplier": 1, "keywords": [{"term": "L2 writing", "weight": 4}]},
            "recommendations": {"minimum_score": 1, "limit": 5},
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            with patch("scripts.update.request_bytes", return_value=(FIXTURES / "rss2.xml").read_bytes()), patch(
                "scripts.update.CrossrefClient.lookup",
                return_value={"doi": "10.1234/example", "authors": ["Mei Lin"], "abstract": "Completed abstract"},
            ):
                status = build(config_path, root / "data", now=datetime(2026, 8, 23, 1, tzinfo=timezone.utc))
            self.assertEqual(status["outcome"], "success")
            self.assertEqual(status["counts"]["items_in_window"], 1)
            self.assertEqual(status["counts"]["recommended_today"], 1)
            for name in ("papers.json", "recommendations.json", "status.json", "history.json"):
                self.assertTrue((root / "data" / name).exists())
            history = json.loads((root / "data" / "history.json").read_text(encoding="utf-8"))
            article_id = json.loads((root / "data" / "papers.json").read_text(encoding="utf-8"))["articles"][0]["id"]
            self.assertEqual(history["days"]["2026-08-22"]["article_ids"], [article_id])

    def test_homepage_data_keeps_unrecommended_articles_in_other_articles(self) -> None:
        config = {
            "journals": [{"id": "test", "name": "Test Journal", "publisher": "Test", "feed_url": "https://example.org/rss"}],
            "crossref": {"enabled": False},
            "ranking": {"title_multiplier": 2, "keywords": [{"term": "feedback", "weight": 4}]},
            "recommendations": {"minimum_score": 1, "limit": 5},
        }
        feed = b"""<?xml version="1.0"?>
        <rss><channel>
          <item><title>Feedback study</title><description>Publication date: 22 August 2026</description><creator>Jane Doe</creator><pubDate>Sat, 22 Aug 2026 00:00:00 GMT</pubDate><link>https://example.org/feedback</link></item>
          <item><title>Unrelated study</title><description>Publication date: 22 August 2026</description><creator>John Doe</creator><pubDate>Sat, 22 Aug 2026 00:00:00 GMT</pubDate><link>https://example.org/unrelated</link></item>
        </channel></rss>"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            with patch("scripts.update.request_bytes", return_value=feed):
                status = build(config_path, root / "data", now=datetime(2026, 8, 23, 1, tzinfo=timezone.utc))
            homepage = json.loads((root / "data" / "recommendations.json").read_text(encoding="utf-8"))
            self.assertEqual(status["counts"]["recommended_today"], 1)
            self.assertEqual(status["counts"]["other_today"], 1)
            self.assertEqual(len(homepage["articles"]), 1)
            self.assertEqual(len(homepage["other_articles"]), 1)
            self.assertEqual(homepage["other_articles"][0]["title"], "Unrelated study")

    def test_build_skips_entries_outside_yesterday_window(self) -> None:
        config = {
            "journals": [{"id": "test", "name": "Test Journal", "publisher": "Test", "feed_url": "https://example.org/rss"}],
            "window": {"enabled": True, "timezone": "Asia/Shanghai"},
            "crossref": {"enabled": True},
            "ranking": {"keywords": [{"term": "L2 writing", "weight": 4}]},
            "recommendations": {"minimum_score": 1},
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            with patch("scripts.update.request_bytes", return_value=(FIXTURES / "rss2.xml").read_bytes()), patch("scripts.update.CrossrefClient.lookup") as lookup:
                status = build(config_path, root / "data", now=datetime(2026, 8, 31, 1, tzinfo=timezone.utc))
            self.assertEqual(status["counts"]["items_in_window"], 0)
            self.assertEqual(status["crossref"]["attempted"], 0)
            lookup.assert_not_called()

    def test_truncated_abstract_uses_crossref_then_doi_page(self) -> None:
        config = {
            "journals": [{"id": "test", "name": "Test Journal", "publisher": "Test", "feed_url": "https://example.org/rss"}],
            "crossref": {
                "enabled": True,
                "max_lookups_per_run": 5,
                "doi_page_enabled": True,
                "max_doi_page_lookups_per_run": 5,
            },
            "ranking": {"keywords": [{"term": "complete", "weight": 1}]},
            "recommendations": {"minimum_score": 1},
        }
        feed = b"""<?xml version="1.0"?>
        <rss><channel><item>
          <title>Article with incomplete abstract</title>
          <description>RSS abstract...</description>
          <doi>10.1234/example</doi>
          <creator>Jane Doe</creator>
          <pubDate>Sat, 22 Aug 2026 00:00:00 GMT</pubDate>
          <link>https://example.org/article</link>
        </item></channel></rss>"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            with patch("scripts.update.request_bytes", return_value=feed), patch(
                "scripts.update.CrossrefClient.lookup",
                return_value={"abstract": "Crossref abstract still incomplete..."},
            ) as lookup, patch(
                "scripts.update.fetch_doi_page_abstract",
                return_value="Complete public abstract from DOI page.",
            ) as fetch_page:
                status = build(config_path, root / "data", now=datetime(2026, 8, 23, 1, tzinfo=timezone.utc))
            article = json.loads((root / "data" / "papers.json").read_text(encoding="utf-8"))["articles"][0]
            self.assertEqual(article["abstract"], "Complete public abstract from DOI page.")
            self.assertEqual(article["abstract_source"], "doi-page")
            self.assertEqual(status["abstracts"]["crossref_attempted"], 1)
            self.assertEqual(status["abstracts"]["doi_page_replaced"], 1)
            lookup.assert_called_once()
            fetch_page.assert_called_once()

    def test_uses_crossref_window_fallback_when_rss_is_blocked(self) -> None:
        config = {
            "journals": [
                {
                    "id": "test",
                    "name": "Test Journal",
                    "publisher": "Test",
                    "feed_url": "https://example.org/rss",
                    "crossref_fallback_issn": "1234-5678",
                }
            ],
            "window": {"enabled": True, "timezone": "Asia/Shanghai"},
            "crossref": {"enabled": True},
            "ranking": {"keywords": [{"term": "feedback", "weight": 3}]},
            "recommendations": {"minimum_score": 1},
        }
        crossref_item = {
            "DOI": "10.1234/fallback",
            "URL": "https://doi.org/10.1234/fallback",
            "title": ["Feedback from a fallback"],
            "container-title": ["Test Journal"],
            "author": [{"given": "Jane", "family": "Doe"}],
            "deposited": {"date-time": "2026-08-22T08:00:00Z"},
            "published-online": {"date-parts": [[2026, 8, 22]]},
            "type": "journal-article",
        }
        blocked = urllib.error.HTTPError("https://example.org/rss", 403, "Forbidden", {}, None)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            with patch("scripts.update.request_bytes", side_effect=blocked), patch(
                "scripts.update.CrossrefClient.journal_updates", return_value=[crossref_item]
            ), patch("scripts.update.CrossrefClient.lookup") as lookup:
                status = build(config_path, root / "data", now=datetime(2026, 8, 23, 1, tzinfo=timezone.utc))
            self.assertEqual(status["outcome"], "success")
            self.assertEqual(status["feeds"][0]["status"], "fallback")
            self.assertEqual(status["counts"]["items_in_window"], 1)
            self.assertEqual(status["counts"]["recommended_today"], 1)
            self.assertEqual(status["crossref"]["attempted"], 0)
            lookup.assert_not_called()


if __name__ == "__main__":
    unittest.main()
