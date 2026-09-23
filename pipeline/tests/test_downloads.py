import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

PIPELINE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PIPELINE))
import pdf_download as downloader
from paper_utils import paper_key, pdf_path, valid_pdf
from venue_sources import ROBOTICS_YEARS, fetch_acm, fetch_coling


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, PIPELINE / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


crawler = load('crawl_pipeline', '1_crawl_papers.py')
PDF = b'%PDF-1.7\n' + b'x' * 1500 + b'\n%%EOF\n'
PAPER = {'paper_id': 'p001', 'conference': 'RSS', 'year': 2024,
         'title': 'Example', 'pdf_url': 'https://www.roboticsproceedings.org/rss20/p001.pdf'}


def response(body, status=200):
    result = Mock(status_code=status, url=PAPER['pdf_url'], headers={})
    result.iter_content.side_effect = lambda **kw: iter([body])
    if status >= 400:
        result.raise_for_status.side_effect = RuntimeError(f'HTTP {status}')
    return result


class DownloadTests(unittest.TestCase):
    def setUp(self):
        downloader._cooldowns.clear()
        downloader._next_request.clear()

    def test_robotics_metadata_is_fetched_and_cached_locally(self):
        self.assertEqual(set(ROBOTICS_YEARS), {'CoRL', 'ICRA', 'IROS', 'RSS'})
        for conf in ROBOTICS_YEARS:
            paper = {**PAPER, 'conference': conf, 'authors': [], 'abstract': '', 'source': 'test'}
            source = 'fetch_ieee' if conf in {'ICRA', 'IROS'} else 'fetch_rss' if conf == 'RSS' else 'fetch_venue_metadata'
            records = [crawler.paper_info(paper)] if conf == 'CoRL' else [paper]
            with self.subTest(conference=conf), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                with patch.object(crawler, 'METADATA_DIR', root), patch.object(crawler, 'create_session'), patch.object(crawler, source, return_value=records) as fetch:
                    crawler.fetch_robotics_metadata([conf], [2024])
                    self.assertEqual(fetch.call_count, 1)
                    saved = json.loads((root / f'{conf}.2024.json').read_text())
                    self.assertEqual(paper_key(saved[0]), paper_key(paper))
                    crawler.fetch_robotics_metadata([conf], [2024])
                    self.assertEqual(fetch.call_count, 1)
                    crawler.fetch_robotics_metadata([conf], [2024], refresh=True)
                    self.assertEqual(fetch.call_count, 2)

    def test_failed_robotics_refresh_preserves_local_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / 'RSS.2024.json'
            original = json.dumps([PAPER])
            target.write_text(original)
            with patch.object(crawler, 'METADATA_DIR', root), patch.object(crawler, 'create_session'), patch.object(crawler, 'fetch_rss', return_value=[]):
                with self.assertRaises(RuntimeError):
                    crawler.fetch_robotics_metadata(['RSS'], [2024], refresh=True)
            self.assertEqual(target.read_text(), original)

    def test_publisher_urls_are_used_instead_of_legacy_arxiv_resolution(self):
        for url, expected in [
            ('https://doi.org/10.1145/123.456', 'https://dl.acm.org/doi/pdf/10.1145/123.456'),
            ('https://doi.org/10.1109/ICRA57147.2024.10610700', 'https://ieeexplore.ieee.org/stampPDF/getPDF.jsp?tp=&arnumber=10610700'),
            ('https://aclanthology.org/2024.acl-long.1/', 'https://aclanthology.org/2024.acl-long.1.pdf'),
        ]:
            p = {**PAPER, 'pdf_url': url, 'resolved_pdf_url': 'https://arxiv.org/pdf/wrong'}
            self.assertEqual(downloader.canonical_pdf_url(p), expected)

    def test_html_challenge_never_becomes_a_pdf_or_success(self):
        session = Mock()
        session.get.return_value = response(b'<!DOCTYPE html>' + b'x' * 5000)
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'paper.pdf'
            with self.assertRaises(RuntimeError):
                downloader.download(session, PAPER, target)
            self.assertFalse(target.exists())
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_truncated_pdf_is_not_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'truncated.pdf'
            with self.assertRaises(ValueError):
                downloader._stream_pdf(response(PDF[:-7]), target, lambda: False)
            self.assertFalse(target.exists())

    def test_success_is_atomically_published_with_provenance(self):
        session = Mock()
        session.get.return_value = response(PDF)
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'paper.pdf'
            provenance = downloader.download(session, PAPER, target)
            self.assertTrue(valid_pdf(target))
            self.assertEqual(provenance['version'], 'publisher')
            self.assertEqual(provenance['url'], PAPER['pdf_url'])
            self.assertEqual(len(list(Path(tmp).iterdir())), 1)

    def test_cancel_cleans_partial_file_and_preserves_existing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'paper.pdf'
            target.write_bytes(PDF)
            with self.assertRaises(InterruptedError):
                downloader._stream_pdf(response(PDF), target, lambda: True)
            self.assertEqual(target.read_bytes(), PDF)
            self.assertEqual(len(list(Path(tmp).iterdir())), 1)

    def test_rate_limit_applies_across_openreview_api_aliases(self):
        limited = response(b'limited', 429)
        limited.headers = {'Retry-After': '120'}
        session = Mock()
        session.get.return_value = limited
        p = {**PAPER, 'pdf_url': 'https://openreview.net/pdf?id=abc'}
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(RuntimeError):
                downloader.download(session, p, Path(tmp) / 'p.pdf')
        self.assertEqual(session.get.call_count, 1)

    def test_cache_cannot_hide_another_year_or_a_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(crawler, 'PDF_DIR', Path(tmp)):
            cache = crawler.DownloadCache(Path(tmp) / 'cache.json')
            old = {**PAPER, 'year': 2023}
            cache.mark_downloaded(paper_key(old))
            cache.mark_downloaded(paper_key(PAPER))
            def downloaded(session, paper, target, stopped):
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(PDF)
                return {'version': 'publisher', 'url': paper['pdf_url']}
            with patch.object(downloader, 'download', side_effect=downloaded) as fetch:
                self.assertTrue(crawler.download_single_pdf(Mock(), PAPER, cache)[0])
            fetch.assert_called_once()
            self.assertNotEqual(paper_key(old), paper_key(PAPER))

    def test_legacy_arxiv_file_requires_publisher_refresh(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'arxiv_resolve_cache.json').write_text(json.dumps({'p001': 'https://arxiv.org/pdf/1234'}))
            cache = crawler.DownloadCache(root / 'cache.json')
            self.assertFalse(cache.can_reuse(PAPER))
            cache.mark_downloaded(paper_key(PAPER), {'version': 'publisher'})
            self.assertTrue(cache.can_reuse(PAPER))

    def test_screening_resume_distinguishes_rss_years(self):
        screening = load('screen_pipeline', '2_deep_screen_papers.py')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'deep_screen_RSS.2023.jsonl').write_text(json.dumps({**PAPER, 'year': 2023, 'label': 'YES'}) + '\n')
            done = screening.load_completed(root)
        self.assertIn(paper_key({**PAPER, 'year': 2023}), done)
        self.assertNotIn(paper_key(PAPER), done)

    def test_corl_mapping_uses_conference_year_not_publication_year(self):
        session = Mock()
        session.get.return_value.text = '<div class="paper"><p class="title">Exact Title</p><a href="https://proceedings.mlr.press/v270/test25a/test25a.pdf">Download PDF</a></div>'
        session.get.return_value.url = 'https://proceedings.mlr.press/v270/'
        with patch.object(downloader, '_pmlr', {}):
            urls = downloader.candidate_urls(session, {**PAPER, 'conference': 'CoRL', 'title': 'Exact Title',
                'pdf_url': 'https://openreview.net/pdf?id=abc'})
        self.assertEqual(urls[0], 'https://proceedings.mlr.press/v270/test25a/test25a.pdf')
        self.assertNotIn('arxiv.org', ' '.join(urls))

    def test_crossref_fallback_keeps_exact_proceedings_only(self):
        session = Mock()
        container = 'Proceedings of the 32nd ACM International Conference on Multimedia'
        article = {'container-title': [container], 'DOI': '10.1145/123.456', 'title': ['Real paper.'],
                   'author': [{'given': 'First', 'family': 'Last'}]}
        session.get.return_value.json.return_value = {'message': {'items': [article,
            {**article, 'DOI': '10.1145/123.789', 'container-title': [container + ' Workshops']}]}}
        papers = fetch_acm(session, 'ACMMM', 2024)
        self.assertEqual(len(papers), 1)
        self.assertEqual(papers[0]['title'], 'Real paper')
        self.assertEqual(papers[0]['pdf_url'], 'https://doi.org/10.1145/123.456')

    def test_coling_fallback_uses_anthology_ids_for_pdf(self):
        session = Mock()
        session.get.return_value.content = b'<collection><volume id="main"><paper id="0"><title>Frontmatter</title></paper><paper id="1"><title>Paper title</title><author><first>A</first><last>B</last></author><abstract>Abstract</abstract></paper></volume><volume id="tutorials"><paper id="1"><title>Other</title><author><first>C</first></author></paper></volume></collection>'
        papers = fetch_coling(session, 2024)
        self.assertEqual(len(papers), 1)
        self.assertEqual(papers[0]['pdf_url'], 'https://aclanthology.org/2024.lrec-main.1.pdf')

    def test_corl_parser_preserves_source_identity(self):
        records = crawler.parse_papers_cool_page('<div id="abc@OpenReview" class="panel paper"><a class="title-link">Title</a></div>', 'CoRL', 2024)
        self.assertEqual(records[0].paper_id, 'abc@OpenReview')

    def test_download_failure_is_checkpointed_and_retried(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(crawler, 'PDF_DIR', root), patch.object(crawler, 'CACHE_FILE', root / 'cache.json'), patch.object(crawler, 'create_session', return_value=Mock()):
                with patch.object(downloader, 'download', side_effect=RuntimeError('challenge')):
                    crawler.download_pdfs([PAPER], max_workers=1)
                cache = crawler.DownloadCache(root / 'cache.json')
                self.assertIn(paper_key(PAPER), cache.failed)
                def downloaded(session, paper, target, stopped):
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(PDF)
                    return {'version': 'publisher'}
                with patch.object(downloader, 'download', side_effect=downloaded):
                    crawler.download_pdfs([PAPER], max_workers=1)
                cache = crawler.DownloadCache(root / 'cache.json')
                self.assertNotIn(paper_key(PAPER), cache.failed)
                self.assertIn(paper_key(PAPER), cache.downloaded)

    def test_supplement_pdf_maps_keep_repeated_ids_separate(self):
        for filename in ('annotate_interface_supplement.py', 'annotate_failure_modes_supplement.py',
                         'annotate_evaluation_deployment_supplement.py'):
            module = load(filename[:-3], filename)
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                records = [PAPER, {**PAPER, 'year': 2023}]
                for paper in records:
                    (root / f'RSS.{paper["year"]}.json').write_text(json.dumps([paper]))
                    target = pdf_path(root, paper)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(PDF)
                with patch.object(module, 'PDF_DIR', root), patch.object(module, 'METADATA_DIR', root):
                    mapping = module.build_pdf_map(records)
                self.assertEqual(set(mapping), {paper_key(p) for p in records})


if __name__ == '__main__':
    unittest.main()
