from __future__ import annotations

import json
import tempfile
import unittest
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from scripts.update import CrossrefClient, authors_need_replacement, build, merge_articles, parse_feed, score_article


ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"


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
    def test_build_writes_all_three_data_files(self) -> None:
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
            for name in ("papers.json", "recommendations.json", "status.json"):
                self.assertTrue((root / "data" / name).exists())

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
