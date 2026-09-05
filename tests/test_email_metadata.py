import unittest
from datetime import datetime, timezone
from unittest.mock import patch
import json
from scripts.email_source import MailMessage, parse_message, clean_legacy_email_articles
from scripts.update import enrich_email_metadata, lookup_openalex


class MailRegressionTests(unittest.TestCase):
    def parse(self, body, publisher='tf'):
        sender, subject = {
            'tf': ('alerts@tandfonline.com', 'New articles for Feedback Journal are now available online'),
            'elsevier': ('sciencedirect@notification.elsevier.com', 'System: Alert'),
            'sage': ('noreply@sagepub.com', 'New OnlineFirst articles available for Language Teaching Research'),
            'wiley': ('alerts@wiley.com', 'Articles Alert'),
        }[publisher]
        return parse_message(MailMessage('test', 'INBOX', datetime.now(timezone.utc), sender, subject, body, ''))[0]

    def test_tf_article_not_button_or_journal_and_no_css_in_abstract(self):
        abstract = 'Generative artificial intelligence is reshaping feedback and assessment design in higher education …'
        body = f'''<head><style>.mobile {{color:red}}</style></head>
        <a href="https://url.tandfonline.com/journal">Feedback Journal</a>
        <p>Research Article</p><a href="https://url.tandfonline.com/title">Calibrating GenAI and human feedback</a>
        <p>Siliang Yu, Chao Wang &amp; Qianxiao Zhang</p><p>{abstract}</p>
        <a href="https://url.tandfonline.com/button">Read article</a>'''
        articles = self.parse(body)
        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0]['authors'], ['Siliang Yu', 'Chao Wang', 'Qianxiao Zhang'])
        self.assertEqual(articles[0]['abstract'], abstract)

    def test_two_elsevier_articles_have_own_authors_and_no_fake_abstract(self):
        body = ''.join(f'<a href="https://click.notification.elsevier.com/{n}">Research into language learning {n}</a><p>Open Access - Research article</p><p>Available Online 03 September 2026</p><p>{author}</p>' for n, author in [(1, 'Pelin Irgin, Nataliya Borkovska'), (2, 'Jane Doe')])
        body += '<a href="https://click.notification.elsevier.com/issue">New Articles in Press, 03 September</a><a href="https://click.notification.elsevier.com/manage">Manage my alerts</a>'
        articles = self.parse(body, 'elsevier')
        self.assertEqual(len(articles), 2)
        self.assertEqual(articles[0]['authors'], ['Pelin Irgin', 'Nataliya Borkovska'])
        self.assertEqual(articles[1]['authors'], ['Jane Doe'])
        self.assertEqual(articles[0]['published'], '2026-09-03')
        self.assertEqual(articles[0]['abstract'], '')

    def test_wiley_section_heading_not_part_of_title(self):
        articles = self.parse('<p>Journal of Computer Assisted Learning</p><p>Volume 42, Issue 5</p><p>ORIGINAL ARTICLE</p><p>Interactive and Intelligent Learning Environments</p><a href="https://el.wiley.com/a">Teacher Readiness for Interactive Learning</a><p>Ayşe Eminoğlu Güven,</p><p>İbrahim Savran</p><p>e70316</p><p>| First Published: 01 September 2026</p>', 'wiley')
        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0]['title'], 'Teacher Readiness for Interactive Learning')
        self.assertEqual(articles[0]['authors'], ['Ayşe Eminoğlu Güven', 'İbrahim Savran'])
        self.assertEqual(articles[0]['journal'], 'Journal of Computer Assisted Learning')

    def test_sage_multiline_title_keeps_author_separate(self):
        articles = self.parse('Article\nBridging Local Roots and Global Goals:\nPolicy in Higher Education\nPaudel Pitambar\nSep 01, 2026 | OnlineFirst\nhttps://url.sagepub.com/a', 'sage')
        self.assertEqual(articles[0]['title'], 'Bridging Local Roots and Global Goals: Policy in Higher Education')
        self.assertEqual(articles[0]['authors'], ['Paudel Pitambar'])
        self.assertEqual(articles[0]['published'], '2026-09-01')

    def test_legacy_navigation_removed_and_label_not_kept_as_abstract(self):
        base = {'metadata_source': 'email', 'abstract_source': 'email', 'publisher': 'Taylor & Francis', 'journal': 'Feedback Journal', 'url': 'https://url.tandfonline.com/a'}
        old = [dict(base, id='button', title='Read article'), dict(base, id='journal', title='Feedback Journal'), dict(base, id='paper', title='Feedback in teaching research', abstract='Research Article')]
        result = clean_legacy_email_articles(old)
        self.assertEqual([a['id'] for a in result], ['paper'])
        self.assertEqual(result[0]['abstract'], '')
        self.assertEqual(old[2]['abstract'], 'Research Article')


class MetadataFallbackTests(unittest.TestCase):
    def test_openalex_reconstructs_positions_and_rejects_wrong_title(self):
        work = {'title': 'Feedback in teaching', 'doi': 'https://doi.org/10.1234/example', 'abstract_inverted_index': {'feedback': [1, 3], 'Teacher': [0], 'improves': [2]}}
        with patch('scripts.update.request_bytes', return_value=json.dumps({'results': [work]}).encode()):
            found = lookup_openalex({'title': 'Feedback in teaching'}, 'test', 1)
            self.assertEqual(found['abstract'], 'Teacher feedback improves feedback')
            self.assertIsNone(lookup_openalex({'title': 'Learning with robots'}, 'test', 1))

    def test_crossref_error_still_uses_openalex_with_lookup_limit(self):
        articles = [{'title': 'Feedback in teaching', 'url': 'https://original.example', 'abstract': '', 'authors': []}, {'title': 'Skipped'}]
        cfg = {'metadata_fallback': {'enabled': True, 'max_lookups_per_run': 1}}
        with patch('scripts.update.CrossrefClient.lookup', side_effect=OSError('unavailable')), patch('scripts.update.lookup_openalex', return_value={'doi': '10.1234/a', 'authors': ['Jane Doe'], 'abstract': 'A public abstract.'}) as lookup:
            result = enrich_email_metadata(articles, cfg)
        self.assertEqual(result['errors'], 1)
        self.assertEqual(result['abstracts_replaced'], 1)
        self.assertEqual(articles[0]['authors'], ['Jane Doe'])
        self.assertEqual(articles[0]['abstract_source'], 'openalex')
        self.assertEqual(articles[0]['url'], 'https://original.example')
        lookup.assert_called_once()

    def test_complete_abstract_is_preserved(self):
        article = {'title': 'Feedback in teaching', 'abstract': 'Existing complete public abstract.', 'authors': []}
        with patch('scripts.update.CrossrefClient.lookup', return_value={'authors': ['Jane Doe'], 'abstract': 'A much longer replacement that must not overwrite a complete original abstract.'}), patch('scripts.update.lookup_openalex') as lookup:
            enrich_email_metadata([article], {'metadata_fallback': {'enabled': True}})
        self.assertEqual(article['abstract'], 'Existing complete public abstract.')
        lookup.assert_not_called()
