"""Byline regressions from public article rows observed in Safari Gmail.

Only the visible article structure is reconstructed here; no private headers or
real tracking tokens are stored.
"""
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from scripts.email_source import MailMessage, parse_message
from scripts.update import build, enrich_email_metadata, merge_articles, restore_email_bylines


def alert(journal, body, publisher='Taylor & Francis', received=None):
    return MailMessage(
        'test-byline', 'INBOX', received or datetime(2026, 10, 9, 9, 45, tzinfo=timezone.utc),
        'alerts@tandfonline.com' if publisher == 'Taylor & Francis' else 'sciencedirect@notification.elsevier.com',
        f'New articles for {journal} are now available online' if publisher == 'Taylor & Francis' else f'{journal}: Alert',
        body, '',
    )


def article_row(title, byline, summary='', host='url.tandfonline.com', before=''):
    return (f'<h3><a href="https://{host}/{title[:8].replace(" ", "-")}">{title}</a></h3>'
            + before + (f'<p>{byline}</p>' if byline else '')
            + (f'<p>{summary}</p>' if summary else ''))


class EmailBylineTests(unittest.TestCase):
    def test_tf_original_byline_and_summary_are_separate(self):
        cases = [
            ('From workload to verified learning: reframing ECTS for AI-rich assessment',
             'Federico Pablo-Martí, Carlos Mir Fernández &amp; Juan de-Lucio',
             'Federico Pablo-Martí, Carlos Mir Fernández & Juan de-Lucio',
             'Generative artificial intelligence (GenAI) weakens a practical assumption in ECTS …'),
            ('Supervisors’ taboos in doctoral supervision: working outside ideals of excellence',
             'Karla Brücknerová, Jakub Vlček, Jana Navrátilová, Barbora Nekardová, Tomáš Lintner &amp; others',
             'Karla Brücknerová, Jakub Vlček, Jana Navrátilová, Barbora Nekardová, Tomáš Lintner & others',
             'Research on doctoral supervision foregrounds openness yet pays less attention to taboos …'),
        ]
        for title, html_byline, raw_byline, summary in cases:
            with self.subTest(title=title):
                papers, result = parse_message(alert('Feedback Journal', article_row(title, html_byline, summary)))
                self.assertEqual(result, 'ok')
                self.assertEqual(len(papers), 1)
                self.assertEqual(papers[0]['authors_text'], raw_byline)
                self.assertEqual(papers[0]['abstract'], summary)

    def test_elsevier_issue_byline_and_authorless_editorial(self):
        title = ('Secondary school teachers’ experiences in extending education for sustainable '
                 'development beyond their classrooms: A multiple-case study')
        body = article_row(title, 'Tuba Stouthart, Dury Bayram, Jan van der Veen',
                           host='click.notification.elsevier.com', before='<p>Article Number 105811</p>')
        papers, _ = parse_message(alert('Teaching and Teacher Education', body, 'Elsevier'))
        self.assertEqual(papers[0]['authors_text'], 'Tuba Stouthart, Dury Bayram, Jan van der Veen')
        self.assertEqual(papers[0]['abstract'], '')
        body = (article_row('TBLT in the L2 classroom: A cognitive-processing approach', '',
                            host='click.notification.elsevier.com',
                            before='<p>Editorial</p><p>Available Online 06 October 2026</p>')
                + article_row('Digital Resilience in AIGC-Mediated English Learning',
                              'Jia Zhenxia, Jiang Renfeng, Yang Gang', host='click.notification.elsevier.com',
                              before='<p>Research article</p><p>Available Online 06 October 2026</p>'))
        papers, _ = parse_message(alert('System', body, 'Elsevier'))
        self.assertEqual(papers[0]['authors_text'], '')
        self.assertEqual(papers[0]['authors'], [])
        self.assertEqual(papers[1]['authors_text'], 'Jia Zhenxia, Jiang Renfeng, Yang Gang')

    def test_authorless_elsevier_types_are_not_bylines(self):
        for label in ('Editorial', 'Short communication', 'Correspondence'):
            with self.subTest(label=label):
                body = article_row('An article without a byline', '', host='click.notification.elsevier.com',
                                   before=f'<p>{label}</p><p>Available Online 06 October 2026</p>')
                papers, _ = parse_message(alert('System', body, 'Elsevier'))
                self.assertEqual(papers[0]['authors_text'], '')
                self.assertEqual(papers[0]['authors'], [])

    def test_byline_has_no_name_or_length_gate_and_keeps_punctuation(self):
        byline = '王小明；李华 &amp; bell hooks, ' + ', '.join('author ' + str(i) for i in range(60))
        body = article_row('A study of teaching practice', byline.replace(', ', ',\n  '), 'Short summary.')
        papers, _ = parse_message(alert('Feedback Journal', body))
        self.assertEqual(papers[0]['authors_text'], byline.replace('&amp;', '&'))
        self.assertGreater(len(papers[0]['authors_text']), 300)
        self.assertEqual(papers[0]['abstract'], 'Short summary.')

    def test_api_never_fills_an_explicitly_empty_email_byline(self):
        article = {'title': 'Editorial', 'authors': [], 'authors_text': '', 'doi': '10.1234/editorial',
                   'abstract': 'Existing complete abstract.'}
        cfg = {'metadata_fallback': {'enabled': True}}
        with patch('scripts.update.CrossrefClient.lookup') as lookup:
            self.assertEqual(enrich_email_metadata([article], cfg)['attempted'], 0)
            lookup.assert_not_called()
        article['doi'] = ''
        with patch('scripts.update.CrossrefClient.lookup', return_value={
            'doi': '10.1234/editorial', 'authors': ['API Person'], 'abstract': 'Abstract.'
        }):
            enrich_email_metadata([article], cfg)
        self.assertEqual(article['authors'], [])
        self.assertEqual(article['authors_text'], '')
        self.assertEqual(article['doi'], '10.1234/editorial')

    def test_same_day_rerun_preserves_explicit_absence(self):
        old = {'id': 'editorial', 'title': 'Editorial', 'authors': ['Old API Person'],
               'first_seen': 'original', 'metadata_source': 'email+crossref'}
        fresh = dict(old, authors=[], authors_text='')
        merged = merge_articles([old], [fresh])[0]
        self.assertEqual(merged['authors_text'], '')
        self.assertEqual(merged['authors'], [])
        self.assertEqual(merged['first_seen'], 'original')

    def test_retained_backfill_keeps_identity_batch_and_api_abstract(self):
        message = alert('Feedback Journal', article_row('Doctoral supervision study',
                                                       'Karla Brücknerová &amp; others', 'Correct summary.'))
        fresh = parse_message(message)[0][0]
        old = dict(fresh, id='original-id', history_date='2026-10-09', first_seen='original-first',
                   last_seen='original-last', abstract='Karla Brücknerová & others Correct summary.')
        old.pop('authors_text')
        old['authors'] = []
        extra = dict(old, id='other-journal', journal='Different Journal')
        records = [old, extra]
        self.assertEqual(restore_email_bylines(records, [message]), 1)
        self.assertEqual(old['authors_text'], 'Karla Brücknerová & others')
        self.assertEqual(old['abstract'], 'Correct summary.')
        self.assertEqual(old['id'], 'original-id')
        self.assertEqual(old['history_date'], '2026-10-09')
        self.assertEqual(old['first_seen'], 'original-first')
        self.assertEqual(old['last_seen'], 'original-last')
        self.assertNotIn('authors_text', extra)
        self.assertEqual(restore_email_bylines(records, [message]), 0)
        old.pop('authors_text')
        old['abstract_source'] = 'crossref'
        old['abstract'] = 'Complete API abstract.'
        restore_email_bylines([old], [message])
        self.assertEqual(old['abstract'], 'Complete API abstract.')

    def test_build_migrates_history_without_counting_it_as_new_mail(self):
        historic = alert('Feedback Journal', article_row('Doctoral supervision study',
                                                         'Karla Brücknerová &amp; others', 'Correct summary.'))
        current = alert('System', article_row('TBLT in the L2 classroom', '', host='click.notification.elsevier.com',
                         before='<p>Editorial</p><p>Available Online 06 October 2026</p>'),
                        'Elsevier', datetime(2026, 10, 10, 9, tzinfo=timezone.utc))
        old = parse_message(historic)[0][0]
        old.update(history_date='2026-10-09', authors=[], abstract='Old contaminated summary.')
        old.pop('authors_text')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / 'config.json'
            config.write_text(json.dumps({'mail': {}, 'history_retention_days': 7,
                'ranking': {'keywords': []}, 'metadata_fallback': {'enabled': False},
                'doi_page': {'enabled': False}}))
            output = root / 'data'
            output.mkdir()
            (output / 'papers.json').write_text(json.dumps({'articles': [old]}))
            (output / 'history.json').write_text(json.dumps({'days': {'2026-10-09': {'article_ids': [old['id']]}}}))
            with patch.dict('os.environ', {'GMAIL_USERNAME': 'test@example.com', 'GMAIL_APP_PASSWORD': 'test'}), patch(
                'scripts.update.fetch_messages', side_effect=[([current], {}), ([historic], {})]
            ) as fetch:
                status = build(config, output, now=datetime(2026, 10, 11, tzinfo=timezone.utc))
            saved = json.loads((output / 'papers.json').read_text())['articles']
            homepage = json.loads((output / 'recommendations.json').read_text())
            history = json.loads((output / 'history.json').read_text())['days']
            self.assertEqual(fetch.call_count, 2)
            self.assertEqual(fetch.call_args_list[1].args[2].isoformat(), '2026-10-08T16:00:00+00:00')
            self.assertEqual(fetch.call_args_list[1].args[3].isoformat(), '2026-10-09T16:00:00+00:00')
            self.assertEqual(status['email']['byline_restore']['restored'], 1)
            self.assertEqual(status['counts']['items_in_window'], 1)
            self.assertEqual(len(saved), 2)
            restored = next(a for a in saved if a['id'] == old['id'])
            self.assertEqual(restored['authors_text'], 'Karla Brücknerová & others')
            self.assertEqual(restored['history_date'], '2026-10-09')
            self.assertEqual(history['2026-10-09']['article_ids'], [old['id']])
            self.assertEqual(len(homepage['other_articles']), 1)
            with patch.dict('os.environ', {'GMAIL_USERNAME': 'test@example.com', 'GMAIL_APP_PASSWORD': 'test'}), patch(
                'scripts.update.fetch_messages', return_value=([current], {})
            ) as fetch:
                build(config, output, now=datetime(2026, 10, 11, tzinfo=timezone.utc))
            self.assertEqual(fetch.call_count, 1)


if __name__ == '__main__':
    unittest.main()
