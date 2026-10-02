import unittest
from datetime import datetime, timezone
from unittest.mock import patch
import json
from scripts.email_source import MailMessage, parse_message, parse_rfc822, clean_legacy_email_articles
from scripts.update import enrich_email_metadata, lookup_openalex, build, _trusted_article_url
from pathlib import Path
import tempfile


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
        <a href="https://url.tandfonline.com/association">www.isatt.org</a>
        <a href="https://url.tandfonline.com/button">Read article</a>'''
        articles = self.parse(body)
        self.assertEqual(len(articles), 1)
        self.assertEqual(articles[0]['authors'], ['Siliang Yu', 'Chao Wang', 'Qianxiao Zhang'])
        self.assertEqual(articles[0]['abstract'], abstract)

    def test_domain_and_url_titles_are_not_articles(self):
        for title in ('www.isatt.org', 'isatt.org', 'https://www.isatt.org/about', 'isatt.org/about'):
            with self.subTest(title=title):
                articles = self.parse(f'<a href="https://url.tandfonline.com/tracked">{title}</a>')
                self.assertEqual(articles, [])

    def test_two_elsevier_articles_have_own_authors_and_no_fake_abstract(self):
        body = ''.join(f'<a href="https://click.notification.elsevier.com/{n}">Research into language learning {n}</a><p>Open Access - Research article</p><p>Available Online 03 September 2026</p><p>{author}</p>' for n, author in [(1, 'Pelin Irgin, Nataliya Borkovska'), (2, 'Jane Doe')])
        body += '<a href="https://click.notification.elsevier.com/issue">New Articles in Press, 03 September</a><a href="https://click.notification.elsevier.com/manage">Manage my alerts</a>'
        articles = self.parse(body, 'elsevier')
        self.assertEqual(len(articles), 2)
        self.assertEqual(articles[0]['authors'], ['Pelin Irgin', 'Nataliya Borkovska'])
        self.assertEqual(articles[1]['authors'], ['Jane Doe'])
        self.assertEqual(articles[0]['published'], '2026-09-03')
        self.assertEqual(articles[0]['abstract'], '')
        self.assertTrue(all(article['is_open_access'] for article in articles))

    def test_open_access_belongs_to_its_article_for_all_three_publishers(self):
        cases = (
            ('elsevier', '<p>Open Access - Research article</p>'),
            ('tf', '<p>Open Access</p>'),
            ('wiley', '<img alt="Open Access">'),
        )
        for publisher, label in cases:
            with self.subTest(publisher=publisher):
                body = (
                    '<a href="https://' + {
                        'elsevier': 'click.notification.elsevier.com',
                        'tf': 'url.tandfonline.com',
                        'wiley': 'el.wiley.com',
                    }[publisher] + '/first">Research on digital teaching</a>'
                    + label + '<p>Available Online 21 September 2026</p><p>Jane Doe</p>'
                    + '<a href="https://' + {
                        'elsevier': 'click.notification.elsevier.com',
                        'tf': 'url.tandfonline.com',
                        'wiley': 'el.wiley.com',
                    }[publisher] + '/second">Learning in higher education</a>'
                    + '<p>Available Online 21 September 2026</p><p>John Smith</p>'
                )
                articles = self.parse(body, publisher)
                self.assertEqual(len(articles), 2)
                self.assertTrue(articles[0]['is_open_access'])
                self.assertFalse(articles[1]['is_open_access'])

    def test_open_access_before_wiley_title_is_recognized(self):
        body = ('<a href="https://el.wiley.com/first">Research on digital teaching</a>'
                '<p>Jane Doe</p><p>| First Published: 20 September 2026</p>'
                '<p>ORIGINAL ARTICLE</p><p>OPEN ACCESS</p>'
                '<a href="https://el.wiley.com/a">Teacher Readiness for Interactive Learning</a>'
                '<p>Jane Doe</p><p>| First Published: 21 September 2026</p>')
        articles = self.parse(body, 'wiley')
        self.assertEqual(len(articles), 2)
        self.assertFalse(articles[0]['is_open_access'])
        self.assertTrue(articles[1]['is_open_access'])
        self.assertEqual(articles[1]['published'], '2026-09-21')

    def test_elsevier_full_issue_link_is_not_an_article(self):
        body = '''
        <a href="https://click.notification.elsevier.com/CL0/https:%2F%2Fwww.sciencedirect.com%2Fjournal%2Fjournal-of-english-for-academic-purposes%2Fvol%2F83%2Fsuppl%2FC%3Fdgcid=raven_sd_via_email/3/010001a074b6935a-848ee069-0f24-4e5f-a229-d7f4f8e46bef-000000/k5HmOqdHNKKdnUdrJhCNRst45m_yOn334JkTrThiQnY=452">
          Read the full issue on ScienceDirect
        </a>'''
        self.assertEqual(self.parse(body, 'elsevier'), [])

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

    def test_sage_manuscript_and_article_are_independent_in_plain_and_html_mail(self):
        # Public metadata observed in Safari Gmail; tracking tokens omitted.
        title = ('Digital Literacy in Adult Lifelong Learning: A Systematic Review '
                 'of Theoretical Frameworks and Pedagogical Effectiveness')
        plain = ('View these articles online now at:\nhttps://url8709.sagepub.com/issue\n'
                 '----------------\nManuscript\n' + title + '\n'
                 'Ampofo Joshua, Li Jiacheng, Zou Wen and Zhu Weiwei\n'
                 'Sep 30, 2026 | OnlineFirst\nhttps://url8709.sagepub.com/manuscript\n'
                 '----------------\nArticle\nOpen Access\nFeedback in teacher education\n'
                 'Jane Doe\nOct 01, 2026 | OnlineFirst\nhttps://url8709.sagepub.com/article\n'
                 '----------------\nTo stop receiving alerts, unsubscribe here:\nhttps://url8709.sagepub.com/unsubscribe')
        for html_body, text_body in (('', plain), ('<pre>' + plain + '</pre>', ''),
                                      ('<a href="https://url8709.sagepub.com/article">Feedback in teacher education</a><p>Jane Doe</p>', plain)):
            with self.subTest(html=bool(html_body), text=bool(text_body)):
                articles, result = parse_message(MailMessage(
                    'sage', 'INBOX', datetime(2026, 10, 1, 11, 1, tzinfo=timezone.utc),
                    'noreply@sagepub.com', 'New OnlineFirst articles available for Review of Educational Research',
                    html_body, text_body))
                self.assertEqual(result, 'ok')
                self.assertEqual(len(articles), 2)
                manuscript = next(a for a in articles if a['title'] == title)
                self.assertEqual(manuscript['authors'], ['Ampofo Joshua', 'Li Jiacheng', 'Zou Wen', 'Zhu Weiwei'])
                self.assertEqual(manuscript['published'], '2026-09-30')
                self.assertEqual(manuscript['journal'], 'Review of Educational Research')
                self.assertEqual(manuscript['abstract'], '')
                self.assertFalse(manuscript['is_open_access'])
                other = next(a for a in articles if a['title'] == 'Feedback in teacher education')
                self.assertTrue(other['is_open_access'])
                self.assertEqual(other['published'], '2026-10-01')

    def test_legacy_navigation_removed_and_label_not_kept_as_abstract(self):
        base = {'metadata_source': 'email', 'abstract_source': 'email', 'publisher': 'Taylor & Francis', 'journal': 'Feedback Journal', 'url': 'https://url.tandfonline.com/a'}
        old = [dict(base, id='button', title='Read article'), dict(base, id='issue', title='Read the full issue on ScienceDirect'), dict(base, id='journal', title='Feedback Journal'), dict(base, id='domain', title='www.isatt.org'), dict(base, id='paper', title='Feedback in teaching research', abstract='Research Article')]
        result = clean_legacy_email_articles(old)
        self.assertEqual([a['id'] for a in result], ['paper'])
        self.assertEqual(result[0]['abstract'], '')
        self.assertEqual(old[4]['abstract'], 'Research Article')


class NewPublisherAlertTests(unittest.TestCase):
    RECEIVED = datetime(2026, 10, 1, 12, 35, tzinfo=timezone.utc)

    def message(self, publisher):
        sender, subject = {
            'apa': ('psycalerts@info.apa.org', 'APA PsycAlert - Journal of Personality and Social Psychology'),
            'cambridge': ('academic@updates.cambridge.org', 'New Issue of Language Teaching available on Cambridge Core'),
        }[publisher]
        body = (Path(__file__).parent / 'fixtures' / (publisher + '_alert.html')).read_text(encoding='utf-8')
        # Exercise the production MIME decoding path without personal headers.
        raw = ('From: ' + sender + '\nSubject: ' + subject + '\n'
               'Content-Type: text/html; charset=utf-8\n\n' + body).encode('utf-8')
        return parse_rfc822(raw, 'INBOX', self.RECEIVED, publisher)

    def test_apa_names_dates_and_non_article_links(self):
        articles, result = parse_message(self.message('apa'))
        self.assertEqual(result, 'ok')
        self.assertEqual(len(articles), 10)
        self.assertEqual(articles[0]['authors'], ['Lu, Sirui', 'Efendić, Emir', 'Feldman, Gilad'])
        self.assertEqual(articles[0]['published'], '2025-12-15')
        self.assertEqual(articles[1]['published'], '2026-07-09')
        self.assertEqual(articles[6]['authors'], [])
        self.assertTrue(all(a['volume'] == '131' and a['issue'] == '4' for a in articles))
        self.assertTrue(all(a['journal'] == 'Journal of Personality and Social Psychology' for a in articles))
        self.assertTrue(all(a['abstract'] == '' for a in articles))

    def test_cambridge_doi_oa_and_citation_links(self):
        articles, result = parse_message(self.message('cambridge'))
        self.assertEqual(result, 'ok')
        self.assertEqual(len(articles), 9)
        self.assertEqual(articles[0]['authors'], ['Dan Zhou'])
        self.assertEqual(articles[2]['authors'], ['Helen Donaghue'])
        self.assertEqual(articles[2]['doi'], '10.1017/s0261444826101165')
        self.assertEqual(articles[2]['published'], '2026-02-25')
        self.assertEqual(articles[2]['pages'], '429-475')
        self.assertEqual(articles[5]['authors'], ['Pia Resnik', 'Jean-Marc Dewaele', 'Chengchen Li', 'Elouise Botes'])
        self.assertEqual(articles[6]['authors'], ['Tomasz Róg'])
        self.assertFalse(articles[6]['is_open_access'])
        self.assertTrue(all(a['is_open_access'] for a in articles[:6]))
        self.assertTrue(all(a['volume'] == '59' and a['issue'] == '4' for a in articles))
        self.assertTrue(all(a['abstract'] == '' for a in articles))
        self.assertEqual(len({a['id'] for a in articles}), 9)

    def test_rejects_untrusted_senders_and_mismatched_templates(self):
        for publisher in ('apa', 'cambridge'):
            message = self.message(publisher)
            message.sender += '.evil.example'
            self.assertEqual(parse_message(message), ([], 'unrecognized'))
            message = self.message(publisher)
            message.subject = 'Join our membership today'
            self.assertEqual(parse_message(message), ([], 'unrecognized'))
        for publisher, domain in (('APA', 'apa.org'), ('Cambridge', 'cambridge.org')):
            self.assertTrue(_trusted_article_url('https://click.info.' + domain + '/article', publisher))
            self.assertFalse(_trusted_article_url('https://' + domain + '.evil.example/article', publisher))

    def test_new_templates_reach_homepage_and_history_by_received_day(self):
        messages = [self.message('apa'), self.message('cambridge'), MailMessage(
            'sage', 'INBOX', self.RECEIVED, 'noreply@sagepub.com',
            'New OnlineFirst articles available for Review of Educational Research', '',
            'Manuscript\nDigital Literacy in Adult Lifelong Learning: A Systematic Review of Theoretical Frameworks and Pedagogical Effectiveness\n'
            'Ampofo Joshua, Li Jiacheng, Zou Wen and Zhu Weiwei\nSep 30, 2026 | OnlineFirst\nhttps://url8709.sagepub.com/article')]
        config = {'mail': {}, 'metadata_fallback': {'enabled': False}, 'doi_page': {'enabled': False},
                  'ranking': {'keywords': [{'term': 'feedback', 'weight': 3}]},
                  'recommendations': {'minimum_score': 1}}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / 'config.json'
            config_path.write_text(json.dumps(config), encoding='utf-8')
            with patch.dict('os.environ', {'GMAIL_USERNAME': 'test@example.com', 'GMAIL_APP_PASSWORD': 'test'}), patch(
                'scripts.update.fetch_messages', return_value=(messages, {'folders': [], 'candidate_count': 3, 'duplicate_count': 0})):
                status = build(config_path, root / 'data', now=datetime(2026, 10, 2, 0, tzinfo=timezone.utc))
            homepage = json.loads((root / 'data/recommendations.json').read_text())
            history = json.loads((root / 'data/history.json').read_text())
            papers = json.loads((root / 'data/papers.json').read_text())['articles']
        self.assertEqual(status['email']['recognized_alerts'], 3)
        self.assertEqual(len(papers), 20)
        self.assertEqual(len(history['days']['2026-10-01']['article_ids']), 20)
        self.assertTrue(any(a['doi'] == '10.1017/s0261444826101165' for a in homepage['articles']))
        self.assertTrue(any(a['title'].startswith('Digital Literacy in Adult') for a in homepage['other_articles']))
        self.assertTrue(all(a['history_date'] == '2026-10-01' for a in papers))



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
