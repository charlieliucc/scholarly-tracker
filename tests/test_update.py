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
    articles_from_crossref_online_first,
    fetch_doi_page_abstract,
    merge_articles,
    merge_crossref,
    parse_feed,
    prune_history_window,
    score_article,
    update_site_stats,
)
from scripts.email_source import MailMessage, fetch_messages, parse_message, parse_rfc822, parse_messages


ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"


class SiteStatsTests(unittest.TestCase):
    def test_preserves_history_and_counts_actions_attempts_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "site-stats.json"
            baseline = json.loads((ROOT / "docs/data/site-stats.json").read_text())
            path.write_text(json.dumps(baseline))
            status = {"generated_at": "2026-10-03T20:00:00Z", "outcome": "partial",
                      "counts": {"items_in_window": 5, "recommended_today": 2}}
            with patch.dict("os.environ", {"GITHUB_RUN_ID": "test-run", "GITHUB_RUN_ATTEMPT": "1"}):
                update_site_stats(root, status)
                update_site_stats(root, status)
            result = json.loads(path.read_text())
            self.assertEqual(result["totals"], {
                "identified": baseline["totals"]["identified"] + 5,
                "recommended": baseline["totals"]["recommended"] + 2,
            })
            self.assertEqual(result["started_on"], "2026-08-31")
            self.assertEqual(len(result["runs"]), len(baseline["runs"]) + 1)
            with patch.dict("os.environ", {"GITHUB_RUN_ID": "test-run", "GITHUB_RUN_ATTEMPT": "2"}):
                update_site_stats(root, status)
            self.assertEqual(json.loads(path.read_text())["totals"], {
                "identified": baseline["totals"]["identified"] + 10,
                "recommended": baseline["totals"]["recommended"] + 4,
            })

    def test_failed_and_offline_builds_leave_totals_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "site-stats.json"
            original = (ROOT / "docs/data/site-stats.json").read_text()
            path.write_text(original)
            for outcome, offline in [("success", True), ("stale", False), ("error", False)]:
                update_site_stats(root, {
                    "generated_at": "2026-10-03T20:00:00Z", "outcome": outcome,
                    "counts": {"items_in_window": 5, "recommended_today": 2},
                }, offline)
                self.assertEqual(path.read_text(), original)


class HistoryRetentionTests(unittest.TestCase):
    def test_keeps_seven_days_and_removes_older_paper_metadata(self) -> None:
        history = {
            "2026-08-29": {"article_ids": ["old"]},
            "2026-08-30": {"article_ids": ["boundary", "missing"]},
            "2026-09-05": {"article_ids": ["current"]},
        }
        articles = [
            {"id": "old", "history_date": "2026-08-29"},
            {"id": "boundary", "history_date": "2026-08-30"},
            {"id": "current", "history_date": "2026-09-05"},
            {"id": "undated"},
        ]

        retained_history, retained_articles = prune_history_window(
            history, articles, datetime(2026, 9, 5).date(), 7
        )

        self.assertEqual(list(retained_history), ["2026-08-30", "2026-09-05"])
        self.assertEqual(retained_history["2026-08-30"]["article_ids"], ["boundary"])
        self.assertEqual([article["id"] for article in retained_articles], ["boundary", "current"])


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


class JournalConfigTests(unittest.TestCase):
    def test_crossref_online_first_journals_do_not_have_rss_sources(self) -> None:
        config = json.loads((ROOT / "config" / "journals.json").read_text(encoding="utf-8"))
        self.assertIn("mail", config)
        self.assertNotIn("journals", config)
        self.assertNotIn("crossref", config)


class EmailParserTests(unittest.TestCase):
    RECEIVED = datetime(2026, 9, 3, 0, 0, tzinfo=timezone.utc)

    def message(self, sender: str, subject: str, body: str) -> MailMessage:
        return MailMessage("m1", "INBOX", self.RECEIVED, sender, subject, body, "")

    def test_parses_elsevier_html_alert_and_rejects_footer(self) -> None:
        message = self.message(
            "sciencedirect@notification.elsevier.com",
            "Educational Research Review : Volume 52",
            '<h2><a href="https://click.notification.elsevier.com/article/10.1016%2Fj.edurev.2026.1">Feedback literacy in teachers</a></h2><p>Jane Doe</p><a href="https://example.invalid">Manage my alerts</a>',
        )
        articles, result = parse_message(message)
        self.assertEqual(result, "ok")
        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0]["publisher"], "Elsevier")
        self.assertEqual(articles[0]["doi"], "10.1016/j.edurev.2026.1")
        self.assertEqual(articles[0]["metadata_source"], "email")

    def test_parses_plain_text_sage_alert(self) -> None:
        message = MailMessage(
            "m2", "INBOX", self.RECEIVED, "noreply@sagepub.com",
            "New OnlineFirst articles available for Language Teaching Research", "",
            "Article\nTeacher emotion and feedback\nJane Doe\nhttps://journals.sagepub.com/doi/10.1177/1\n",
        )
        articles, result = parse_message(message)
        self.assertEqual(result, "ok")
        self.assertEqual(articles[0]["journal"], "Language Teaching Research")

    def test_unknown_or_marketing_mail_is_not_an_alert(self) -> None:
        message = self.message("promo@example.com", "Publish with us", "Buy this service")
        self.assertEqual(parse_message(message), ([], "unrecognized"))
        _, stats = parse_messages([message])
        self.assertEqual(stats["errors"], 0)
        self.assertEqual(stats["failed_messages"], [])

    def test_parser_failure_records_subject_without_private_message_data(self) -> None:
        message = self.message("private@example.com", "期刊更新 <2026>", "private body")
        with patch("scripts.email_source.parse_message", side_effect=ValueError("private body")):
            articles, stats = parse_messages([message])
        self.assertEqual(articles, [])
        self.assertEqual(stats["errors"], 1)
        self.assertEqual(stats["failed_messages"], [{"subject": "期刊更新 <2026>", "reason": "ValueError"}])
        self.assertNotIn("private", json.dumps(stats))

    def test_empty_alert_is_distinct_from_parser_failure(self) -> None:
        message = self.message("alerts@example.com", "期刊目录", "")
        with patch("scripts.email_source.parse_message", return_value=([], "no_articles")):
            _, stats = parse_messages([message])
        self.assertEqual(stats["empty_messages"], [{"subject": "期刊目录"}])
        self.assertEqual(stats["errors"], 0)

    def test_rfc822_decodes_multipart_headers_without_side_effects(self) -> None:
        raw = ("From: alerts@tandfonline.com\n"
               "Subject: =?utf-8?b?TmV3IGFydGljbGVz?=\n"
               "Message-ID: <abc@example.com>\n"
               "Content-Type: text/html; charset=utf-8\n\n"
               '<a href="https://www.tandfonline.com/doi/10.1080/1234">A feedback study in teaching</a>').encode()
        parsed = parse_rfc822(raw, "INBOX", self.RECEIVED)
        self.assertEqual(parsed.subject, "New articles")
        articles, _ = parse_messages([parsed])
        self.assertEqual(len(articles), 1)


class EmailBuildTests(unittest.TestCase):
    def test_open_access_without_keyword_match_stays_in_other_articles(self) -> None:
        config = {
            "mail": {},
            "ranking": {"keywords": [{"term": "feedback", "weight": 3}]},
            "recommendations": {"minimum_score": 1},
            "metadata_fallback": {"enabled": False},
            "doi_page": {"enabled": False},
        }
        message = MailMessage(
            "oa-1", "INBOX", datetime(2026, 9, 21, 12, tzinfo=timezone.utc),
            "sciencedirect@notification.elsevier.com", "Test Journal: Alert",
            '<a href="https://click.notification.elsevier.com/article/1">Research on digital teaching</a>'
            '<p>Open Access - Research article</p><p>Available Online 21 September 2026</p>', "",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            with patch.dict("os.environ", {"GMAIL_USERNAME": "user@example.com", "GMAIL_APP_PASSWORD": "secret"}), patch(
                "scripts.update.fetch_messages",
                return_value=([message], {"folders": [], "candidate_count": 1, "duplicate_count": 0}),
            ):
                build(config_path, root / "data", now=datetime(2026, 9, 22, 12, tzinfo=timezone.utc))
            output = json.loads((root / "data" / "recommendations.json").read_text(encoding="utf-8"))
            self.assertEqual(output["articles"], [])
            article = output["other_articles"][0]
            self.assertEqual(article["matched_keywords"], [])
            self.assertEqual(article["published"], "2026-09-21")
            self.assertTrue(article["is_open_access"])

    def test_email_build_prunes_history_and_papers_to_seven_days(self) -> None:
        config = {
            "mail": {},
            "history_retention_days": 7,
            "ranking": {"keywords": []},
            "recommendations": {"minimum_score": 1},
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            output = root / "data"
            output.mkdir()
            papers = {
                "articles": [
                    {"id": "old", "title": "Old paper", "history_date": "2026-08-30"},
                    {"id": "boundary", "title": "Boundary paper", "history_date": "2026-08-31"},
                ]
            }
            history = {
                "version": 1,
                "days": {
                    "2026-08-30": {"article_ids": ["old"]},
                    "2026-08-31": {"article_ids": ["boundary"]},
                },
            }
            (output / "papers.json").write_text(json.dumps(papers), encoding="utf-8")
            (output / "history.json").write_text(json.dumps(history), encoding="utf-8")

            with patch.dict("os.environ", {"GMAIL_USERNAME": "user@example.com", "GMAIL_APP_PASSWORD": "secret"}), patch(
                "scripts.update.fetch_messages",
                return_value=([], {"folders": [], "candidate_count": 0, "duplicate_count": 0}),
            ):
                status = build(config_path, output, now=datetime(2026, 9, 6, 16, 0, tzinfo=timezone.utc))

            saved_history = json.loads((output / "history.json").read_text(encoding="utf-8"))
            saved_papers = json.loads((output / "papers.json").read_text(encoding="utf-8"))
            self.assertEqual(list(saved_history["days"]), ["2026-08-31", "2026-09-06"])
            self.assertEqual([article["id"] for article in saved_papers["articles"]], ["boundary"])
            self.assertEqual(status["counts"]["all_articles"], 1)

    def test_email_build_uses_yesterday_batch_and_only_fetches_high_score_pages(self) -> None:
        config = {
            "mail": {"host": "imap.gmail.com", "port": 993},
            "doi_page": {"enabled": True, "max_lookups_per_run": 5, "max_bytes": 10000},
            "window": {"enabled": True, "timezone": "Asia/Shanghai"},
            "ranking": {"title_multiplier": 2, "keywords": [{"term": "feedback", "weight": 3}]},
            "recommendations": {"minimum_score": 3, "limit": 5},
        }
        received = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)
        message = MailMessage(
            "gm-1", "INBOX", received, "alerts@tandfonline.com",
            "New articles for Feedback Journal are now available online",
            '<a href="https://www.tandfonline.com/doi/10.1080/1234">Feedback study</a>', "",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            with patch.dict("os.environ", {"GMAIL_USERNAME": "user@example.com", "GMAIL_APP_PASSWORD": "secret"}), patch(
                "scripts.update.fetch_messages", return_value=([message], {"folders": [{"name": "INBOX", "status": "ok", "in_window": 1}], "candidate_count": 1, "duplicate_count": 0})
            ), patch("scripts.update.fetch_page_metadata", return_value={"doi": "10.1080/1234", "abstract": "A complete public abstract about feedback."}) as page:
                status = build(config_path, root / "data", now=datetime(2026, 9, 5, 0, 0, tzinfo=timezone.utc))
            self.assertEqual(status["outcome"], "success")
            self.assertEqual(status["window"]["start"], "2026-09-04T00:00:00+08:00")
            self.assertEqual(status["email"]["recognized_alerts"], 1)
            self.assertEqual(status["abstracts"]["replaced"], 1)
            page.assert_called_once()
            homepage = json.loads((root / "data" / "recommendations.json").read_text(encoding="utf-8"))
            self.assertEqual(homepage["date"], "2026-09-04")
            article = json.loads((root / "data" / "papers.json").read_text(encoding="utf-8"))["articles"][0]
            self.assertEqual(article["abstract_source"], "doi-page")
            history = json.loads((root / "data" / "history.json").read_text(encoding="utf-8"))
            self.assertIn("2026-09-04", history["days"])

    def test_email_failure_keeps_existing_public_data(self) -> None:
        config = {"mail": {}, "status_max_age_hours": 48, "ranking": {"keywords": []}, "recommendations": {"minimum_score": 1}}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            output = root / "data"
            output.mkdir()
            old = {"generated_at": "2026-09-03T20:00:00Z", "articles": [{"id": "old", "title": "Old"}]}
            (output / "papers.json").write_text(json.dumps(old), encoding="utf-8")
            (output / "recommendations.json").write_text(json.dumps(old), encoding="utf-8")
            with patch.dict("os.environ", {"GMAIL_USERNAME": "user@example.com", "GMAIL_APP_PASSWORD": "secret",
                                           "GITHUB_RUN_ID": "123", "GITHUB_REPOSITORY": "charlieliucc/scholarly-tracker"}), patch(
                "scripts.update.fetch_messages", side_effect=RuntimeError("temporary IMAP failure")
            ):
                status = build(config_path, output, now=datetime(2026, 9, 5, tzinfo=timezone.utc))
            self.assertEqual(status["outcome"], "stale")
            self.assertEqual(json.loads((output / "papers.json").read_text(encoding="utf-8"))["articles"][0]["id"], "old")
            self.assertEqual(json.loads((output / "recommendations.json").read_text()), old)
            self.assertEqual(status["content_updated_at"], old["generated_at"])
            self.assertEqual(status["freshness_max_age_hours"], 48)
            self.assertEqual(status["run_url"], "https://github.com/charlieliucc/scholarly-tracker/actions/runs/123")

    def test_failed_subjects_reach_public_status(self) -> None:
        config = {"mail": {}, "ranking": {"keywords": []}, "recommendations": {"minimum_score": 1}}
        message = MailMessage("m1", "INBOX", datetime(2026, 9, 4, 12, tzinfo=timezone.utc),
                              "private@example.com", "期刊更新", "private body", "")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.json"
            config_path.write_text(json.dumps(config))
            with patch.dict("os.environ", {"GMAIL_USERNAME": "user@example.com", "GMAIL_APP_PASSWORD": "secret"}), patch(
                "scripts.update.fetch_messages", return_value=([message], {"folders": []})
            ), patch("scripts.email_source.parse_message", side_effect=ValueError("private body")):
                status = build(config_path, root / "data", now=datetime(2026, 9, 5, tzinfo=timezone.utc))
            self.assertEqual(status["outcome"], "partial")
            self.assertEqual(status["email"]["failed_messages"], [{"subject": "期刊更新", "reason": "ValueError"}])
            self.assertNotIn("private", (root / "data/status.json").read_text())

    def test_offline_and_unreadable_folders_preserve_content_and_its_date(self) -> None:
        config = {"mail": {}, "ranking": {"keywords": []}, "recommendations": {"minimum_score": 1}}
        for offline in (False, True):
            with self.subTest(offline=offline), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                config_path = root / "config.json"
                config_path.write_text(json.dumps(config))
                output = root / "data"
                output.mkdir()
                old = {"generated_at": "2026-09-03T20:00:00Z", "articles": [{"id": "old", "title": "Old"}]}
                for name in ("papers.json", "recommendations.json"):
                    (output / name).write_text(json.dumps(old))
                with patch.dict("os.environ", {"GMAIL_USERNAME": "user@example.com", "GMAIL_APP_PASSWORD": "secret"}), patch(
                    "scripts.update.fetch_messages", return_value=([], {"folders": [{"name": "INBOX", "status": "error"}]})
                ):
                    status = build(config_path, output, now=datetime(2026, 9, 5, tzinfo=timezone.utc), offline=offline)
                self.assertEqual(status["email"]["status"], "offline" if offline else "error")
                self.assertEqual(status["content_updated_at"], old["generated_at"])
                for name in ("papers.json", "recommendations.json"):
                    self.assertEqual(json.loads((output / name).read_text()), old)

    def test_imap_tracks_failed_search_and_individual_reads(self) -> None:
        class FakeIMAP:
            def __init__(self, host, port):
                self.folder = ""

            def login(self, username, password):
                return "OK", []

            def list(self):
                return "OK", [b'* LIST () "/" "INBOX"', b'* LIST (\\Junk) "/" "[Gmail]/Spam"']

            def select(self, folder, readonly=False):
                self.folder = folder
                return "OK", []

            def uid(self, command, *args):
                if command == "search":
                    return ("NO", []) if self.folder == "[Gmail]/Spam" else ("OK", [b"1"])
                return "NO", []

            def logout(self):
                return "OK", []

        messages, stats = fetch_messages("user@example.com", "secret",
            datetime(2026, 9, 2, tzinfo=timezone.utc), datetime(2026, 9, 3, tzinfo=timezone.utc), client_factory=FakeIMAP)
        self.assertEqual(messages, [])
        self.assertEqual(stats["read_errors"], 1)
        self.assertEqual(stats["folders"][0]["status"], "partial")
        self.assertEqual(stats["folders"][1]["error"], "SEARCH failed")

    def test_imap_fetch_is_read_only_and_filters_exact_internal_date(self) -> None:
        class FakeIMAP:
            instances = []

            def __init__(self, host, port):
                self.calls = []
                self.selected = []
                self.__class__.instances.append(self)

            def login(self, username, password):
                self.calls.append(("login", username, password))
                return "OK", []

            def list(self):
                return "OK", [b'* LIST (\\HasNoChildren) "/" "INBOX"', b'* LIST (\\HasNoChildren \\Junk) "/" "[Gmail]/Spam"']

            def select(self, folder, readonly=False):
                self.selected.append((folder, readonly))
                return "OK", [b""]

            def uid(self, command, *args):
                self.calls.append((command, args))
                if command == "search":
                    return "OK", [b"1 2"]
                uid = args[0]
                received = b"02-Sep-2026 16:00:00 +0000" if uid == b"1" else b"03-Sep-2026 16:00:00 +0000"
                raw = (b"From: alerts@tandfonline.com\nSubject: New articles\nMessage-ID: <" + uid + b">\n"
                       b"Content-Type: text/html; charset=utf-8\n\n"
                       b'<a href="https://www.tandfonline.com/doi/10.1080/1234">Feedback study article</a>')
                return "OK", [(b'1 FETCH (INTERNALDATE "' + received + b'" X-GM-MSGID ' + uid + b' BODY[] {' + str(len(raw)).encode() + b'})', raw)]

            def logout(self):
                self.calls.append(("logout",))
                return "OK", []

        messages, stats = fetch_messages(
            "user@example.com", "app-password", datetime(2026, 9, 2, 16, tzinfo=timezone.utc),
            datetime(2026, 9, 3, 16, tzinfo=timezone.utc), client_factory=FakeIMAP,
        )
        self.assertEqual(len(messages), 1)
        self.assertEqual(stats["candidate_count"], 4)
        self.assertTrue(all(readonly for _, readonly in FakeIMAP.instances[0].selected))
        fetch_calls = [call for call in FakeIMAP.instances[0].calls if call[0] == "fetch"]
        self.assertTrue(all("BODY.PEEK" in call[1][1] for call in fetch_calls))
class RankingTests(unittest.TestCase):
    def test_broad_ai_terms_share_one_low_weight_score(self) -> None:
        config = json.loads((ROOT / "config" / "journals.json").read_text(encoding="utf-8"))
        settings = config["ranking"]
        for term in ("GenAI", "generative AI", "artificial intelligence"):
            with self.subTest(term=term):
                score, matches = score_article({"title": term, "abstract": term, "authors": []}, settings)
                self.assertEqual(score, 2)
                self.assertEqual([item["keyword"] for item in matches], [term])

        score, matches = score_article(
            {"title": "GenAI, generative AI and artificial intelligence", "abstract": "GenAI", "authors": []},
            settings,
        )
        self.assertEqual(score, 2)
        self.assertEqual(len(matches), 1)

        score, _ = score_article(
            {"title": "A teaching study", "abstract": "GenAI in assessment", "authors": []}, settings
        )
        self.assertEqual(score, config["recommendations"]["minimum_score"])

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

    def test_merge_preserves_confirmed_open_access(self) -> None:
        old = {"id": "old", "title": "Same title", "journal": "J", "is_open_access": True}
        new = {"id": "new", "title": "Same title", "journal": "J", "is_open_access": False}
        self.assertTrue(merge_articles([old], [new])[0]["is_open_access"])

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

    def test_online_first_query_uses_published_online_date_filter(self) -> None:
        client = CrossrefClient(contact_email="", user_agent="test")
        with patch.object(client, "_get_json", return_value={"message": {"items": []}}) as get_json:
            client.journal_online_first("0260-2938", datetime(2026, 9, 1).date(), datetime(2026, 9, 1).date())
        self.assertEqual(get_json.call_args.args[0], "journals/0260-2938/works")
        filters = get_json.call_args.args[1]["filter"]
        self.assertIn("from-online-pub-date:2026-09-01", filters)
        self.assertIn("until-online-pub-date:2026-09-01", filters)
        self.assertIn("type:journal-article", filters)

    def test_online_first_requires_missing_volume_and_issue(self) -> None:
        messages = [
            {
                "DOI": "10.1234/online",
                "URL": "https://doi.org/10.1234/online",
                "title": ["Online first article"],
                "container-title": ["Test Journal"],
                "published-online": {"date-parts": [[2026, 9, 1]]},
                "author": [{"given": "Jane", "family": "Doe"}],
                "type": "journal-article",
            },
            {
                "DOI": "10.1234/issue",
                "URL": "https://doi.org/10.1234/issue",
                "title": ["Issue article"],
                "container-title": ["Test Journal"],
                "published-online": {"date-parts": [[2026, 9, 1]]},
                "volume": "12",
                "issue": "3",
                "type": "journal-article",
            },
        ]
        articles, stats = articles_from_crossref_online_first(
            messages, {"id": "test", "name": "Test Journal", "publisher": "Taylor & Francis"}, "2026-09-02T00:00:00Z"
        )
        self.assertEqual([article["doi"] for article in articles], ["10.1234/online"])
        self.assertEqual(stats["with_issue"], 1)
        self.assertEqual(articles[0]["online_first_status"], "confirmed")


class BuildTests(unittest.TestCase):
    def test_crossref_online_first_source_does_not_request_rss(self) -> None:
        config = {
            "journals": [
                {
                    "id": "test-tf",
                    "name": "Test Journal",
                    "publisher": "Taylor & Francis",
                    "discovery_mode": "crossref_online_first",
                    "crossref_issn": "1234-5678",
                    "site_url": "https://www.tandfonline.com/journals/test",
                }
            ],
            "window": {"enabled": True, "timezone": "Asia/Shanghai"},
            "crossref": {"enabled": True, "doi_page_enabled": False},
            "ranking": {"keywords": [{"term": "feedback", "weight": 3}]},
            "recommendations": {"minimum_score": 1},
        }
        message = {
            "DOI": "10.1234/online-first",
            "URL": "https://doi.org/10.1234/online-first",
            "title": ["Feedback online first"],
            "container-title": ["Test Journal"],
            "publisher": "Taylor & Francis",
            "published-online": {"date-parts": [[2026, 9, 1]]},
            "author": [{"given": "Jane", "family": "Doe"}],
            "type": "journal-article",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            with patch.object(CrossrefClient, "journal_online_first", return_value=[message]), patch(
                "scripts.update.request_bytes", side_effect=AssertionError("T&F RSS must not be requested")
            ) as request:
                status = build(config_path, root / "data", now=datetime(2026, 9, 2, 1, tzinfo=timezone.utc))
            self.assertFalse(request.called)
            self.assertEqual(status["feeds"][0]["source"], "crossref")
            self.assertEqual(status["feeds"][0]["discovery_mode"], "crossref_online_first")
            self.assertEqual(status["feeds"][0]["items"], 1)
            self.assertEqual(status["counts"]["items_in_window"], 1)
            self.assertEqual(status["crossref"]["primary_attempted"], 1)
            article = json.loads((root / "data" / "papers.json").read_text(encoding="utf-8"))["articles"][0]
            self.assertEqual(article["metadata_source"], "crossref-onlinefirst")
            self.assertEqual(article["published"], "2026-09-01")

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
            history = json.loads((root / "data" / "history.json").read_text(encoding="utf-8"))
            day = history["days"]["2026-08-22"]
            papers = json.loads((root / "data" / "papers.json").read_text(encoding="utf-8"))["articles"]
            title_by_id = {article["id"]: article["title"] for article in papers}
            self.assertEqual([title_by_id[item] for item in day["recommended_article_ids"]], ["Feedback study"])
            self.assertEqual([title_by_id[item] for item in day["other_article_ids"]], ["Unrelated study"])

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
