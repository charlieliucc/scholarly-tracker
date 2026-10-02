import unittest
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

from scripts.email_source import MailMessage, parse_message, parse_messages, parse_rfc822


class WileyNatureMailTests(unittest.TestCase):
    RECEIVED = datetime(2026, 10, 2, 1, 0, tzinfo=timezone.utc)
    SAMPLES = {
        '2026-09-15_Wiley-TOC-TESOL-Quarterly-60-3':
            ('Table of Contents Alert: TESOL Quarterly, Vol. 60, No. 3, September 2026', 34),
        '2026-09-29_Wiley-EarlyView-Modern-Language-Journal':
            ('Early View Alert: The Modern Language Journal', 1),
        '2026-10-01_Wiley-NewArticles-JCAL-42-6':
            ('New Articles Alert: Journal of Computer Assisted Learning, Vol. 42, No. 6, December 2026', 2),
        '2026-10-01_Wiley-EarlyView-BJET':
            ('Early View Alert: British Journal of Educational Technology', 1),
        '2026-10-01_Wiley-EarlyView-TESOL-Quarterly':
            ('Early View Alert: TESOL Quarterly', 2),
        '2026-09-17_Nature-alert': ('Nature alert for 17th September 2026', 103),
        '2026-09-24_Nature-alert': ('Nature alert for 24th September 2026', 107),
        '2026-10-01_Nature-alert': ('Nature alert for 01st October 2026', 103),
    }

    def message(self, name, encoding='quoted-printable'):
        subject, _ = self.SAMPLES[name]
        body = (Path(__file__).parent / 'fixtures' / (name + '.html')).read_text()
        mail = EmailMessage()
        mail['From'] = 'wileyonlinelibrary@wiley.com' if 'Wiley' in name else 'alerts@nature.com'
        mail['Subject'] = subject
        mail.set_content('View the HTML journal alert.')
        mail.add_alternative(body, subtype='html', cte=encoding)
        return parse_rfc822(mail.as_bytes(), 'INBOX', self.RECEIVED, name)

    def articles(self, name):
        articles, result = parse_message(self.message(name))
        self.assertEqual(result, 'ok')
        return articles

    def test_all_eight_samples_and_mime_encodings(self):
        for name, (_, count) in self.SAMPLES.items():
            for encoding in ('quoted-printable', 'base64'):
                with self.subTest(name=name, encoding=encoding):
                    articles, result = parse_message(self.message(name, encoding))
                    self.assertEqual(result, 'ok')
                    self.assertEqual(len(articles), count)
                    self.assertEqual(len({a['id'] for a in articles}), count)
                    self.assertTrue(all(a['feed_timestamp'] == '2026-10-02T01:00:00Z' for a in articles))
                    self.assertFalse(any(a['title'] in {'Issue Information', 'Browse table of contents',
                                                       "This issue's Research Highlights"} for a in articles))

    def test_wiley_toc_badges_pages_and_original_dates(self):
        articles = self.articles('2026-09-15_Wiley-TOC-TESOL-Quarterly-60-3')
        self.assertTrue(all((a['journal'], a['volume'], a['issue']) == ('TESOL Quarterly', '60', '3')
                            for a in articles))
        self.assertTrue(all(a['authors'] and a['published'] and a['pages'] and not a['abstract'] for a in articles))
        self.assertEqual(sum(a['is_open_access'] for a in articles), 16)
        self.assertEqual(articles[0]['authors'], ['Jim McKinley', 'Sihan Zhou'])
        self.assertEqual(articles[0]['published'], '2026-08-17')
        self.assertEqual(articles[0]['pages'], '1021-1032')
        self.assertEqual(articles[1]['published'], '2025-12-22')
        # The badge preceding Kobayashi's title must not apply to Kawasaki.
        self.assertFalse(articles[1]['is_open_access'])
        self.assertTrue(articles[2]['is_open_access'])
        self.assertFalse(articles[3]['is_open_access'])

    def test_wiley_new_articles_own_badges_and_elocators(self):
        first, second = self.articles('2026-10-01_Wiley-NewArticles-JCAL-42-6')
        self.assertEqual(first['authors'], ['Tanguy Dubois', 'Damien De Meyere', 'Patrick Watrin', 'Magali Paquot'])
        self.assertEqual(second['authors'], ['Fahimeh Keshavarzi', 'Elham Heidari', 'Zahra Rafatjoo',
                                              'Ghasem Salimi', 'Mohammadreza Farrokhnia'])
        self.assertEqual([first['is_open_access'], second['is_open_access']], [False, True])
        self.assertEqual([first['pages'], second['pages']], ['e70338', 'e70348'])
        for article in (first, second):
            self.assertEqual((article['volume'], article['issue'], article['published']), ('42', '6', '2026-09-29'))
            self.assertEqual(article['journal'], 'Journal of Computer Assisted Learning')

    def test_wiley_early_view_authors_and_dates(self):
        modern = self.articles('2026-09-29_Wiley-EarlyView-Modern-Language-Journal')[0]
        self.assertEqual(modern['authors'], ['Fubiao Zhen', 'Wei Dong Shi', 'Sandra Acosta'])
        self.assertEqual(modern['published'], '2026-09-28')
        bjet = self.articles('2026-10-01_Wiley-EarlyView-BJET')[0]
        self.assertEqual(bjet['authors'], ['Ahmed Tlili', 'Yan Wang', 'Ronghuai Huang', 'Dejian Liu', 'Thomas K. F. Chiu'])
        self.assertEqual(bjet['published'], '2026-09-30')
        first, second = self.articles('2026-10-01_Wiley-EarlyView-TESOL-Quarterly')
        self.assertEqual(second['authors'], ['Meng Xiong', 'Timothy Teo'])
        self.assertIn('GenAI‐Mediated', second['title'])
        self.assertTrue(first['is_open_access'] and second['is_open_access'])
        self.assertTrue(all(not a['volume'] and not a['issue'] and not a['abstract'] for a in (modern, bjet, first, second)))

    def test_nature_all_sections_short_summary_and_truncated_authors(self):
        articles = self.articles('2026-10-01_Nature-alert')
        self.assertEqual({a['section'] for a in articles},
                         {'This week', 'News in Focus', 'Books & Arts', 'Opinion', 'Work', 'Research'})
        self.assertTrue(all(a['journal'] == 'Nature' and not a['published'] for a in articles))
        by_title = {a['title']: a for a in articles}
        note = by_title['The buck stops with me']
        self.assertEqual(note['authors'], ['Mercer Eddy'])
        self.assertEqual(note['abstract'], 'A note to self.')
        research = by_title['Scalable decision-making for games of imperfect information']
        self.assertEqual(research['authors'], ['Samuel Sokota', 'Eugene Vinitsky', 'Hengyuan Hu'])
        self.assertTrue(research['authors_truncated'])
        self.assertTrue(research['abstract'].startswith('Ataraxos, an AI for the board wargame Stratego'))
        work = by_title['From Trojan horses to AI-proof exams: how professors are tackling students’ AI use']
        self.assertEqual(work['authors'], ['Katarina Zimmer'])
        self.assertIn('sneaking hidden AI prompts', work['abstract'])
        self.assertNotIn('Katarina Zimmer', work['abstract'])
        self.assertNotIn('CRISPR and the genome', by_title)
        self.assertNotIn('Precision medicine', by_title)

    def test_nature_no_authorship_and_corrections(self):
        articles = self.articles('2026-09-17_Nature-alert')
        self.assertEqual(articles[0]['authors'], [])
        self.assertTrue(articles[0]['abstract'].startswith('An era-defining mathematics claim'))
        self.assertEqual(sum(a['section'] == 'Amendments & Corrections' for a in articles), 4)
        work = next(a for a in articles if a['section'] == 'Work')
        self.assertEqual(work['authors'], ['Amanda Heidt'])

    def test_messages_pipeline_counts_publishers(self):
        articles, stats = parse_messages(self.message(name) for name in self.SAMPLES)
        self.assertEqual(len(articles), 353)
        self.assertEqual(stats['recognized'], 8)
        self.assertEqual(stats['errors'], 0)
        self.assertEqual({a['publisher'] for a in articles}, {'Wiley', 'Nature'})

    def test_sender_and_link_domain_boundaries(self):
        for name in ('2026-10-01_Wiley-EarlyView-TESOL-Quarterly', '2026-10-01_Nature-alert'):
            mail = self.message(name)
            mail.sender += '.evil.example'
            self.assertEqual(parse_message(mail), ([], 'unrecognized'))
        body = '<a>Research</a><td><span style="font-size:18px"><a href="https://nature.com.evil.example/article">An untrusted article title</a></span></td>'
        mail = MailMessage('bad-link', 'INBOX', self.RECEIVED, 'nature@ealerts.nature.com', 'Nature Alert', body, '')
        self.assertEqual(parse_message(mail), ([], 'no_articles'))

    def test_nature_card_boundaries_ignore_promotions_and_incidental_dates(self):
        def card(title, summary, author):
            return ('<td><span style="font-size:18px"><a href="https://links.springernature.com/'
                    + title.replace(' ', '-') + '">' + title + '</a></span>'
                    '<span style="font-size:16px">' + summary + '</span>'
                    '<span style="font-size:14px">' + author + '</span></td>')
        body = ('<h2><a>Work</a></h2>'
                + card('First research article', 'A trial began on 01 October 2026.', 'Jane Doe')
                + '<h2><a>Research</a></h2>'
                + card('Second research article', 'Independent results.', 'John Smith')
                + '<h2><a>Collections</a></h2>'
                + card('A promotional collection', 'A collection of earlier papers.', 'Collection Editor'))
        mail = MailMessage('boundaries', 'INBOX', self.RECEIVED, 'alerts@nature.com', 'Nature Alert', body, '')
        articles, result = parse_message(mail)
        self.assertEqual(result, 'ok')
        self.assertEqual(len(articles), 2)
        self.assertEqual([a['authors'] for a in articles], [['Jane Doe'], ['John Smith']])
        self.assertEqual([a['section'] for a in articles], ['Work', 'Research'])
        self.assertEqual(articles[0]['published'], '')
        self.assertEqual(articles[1]['abstract'], 'Independent results.')


if __name__ == '__main__':
    unittest.main()
