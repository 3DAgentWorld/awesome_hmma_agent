#!/usr/bin/env python3
"""
Crawl conference paper metadata and download the PDFs of ALL papers.

Collect metadata and fetch publisher PDFs with resumable, validated downloads.

Usage:
    python 1_crawl_papers.py --phase metadata   # only fetch metadata
    python 1_crawl_papers.py --phase download   # only download PDFs
    python 1_crawl_papers.py --phase all        # both phases
    python 1_crawl_papers.py --phase metadata --conferences NeurIPS ICLR --years 2024 2025
"""

import os
import re
import sys
import json
import html
from html import unescape as html_unescape
import time
import signal
import hashlib
import logging
import argparse
import threading
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from dataclasses import dataclass, asdict, fields

import requests
from tqdm import tqdm
import pdf_download
from paper_utils import paper_key, pdf_path, valid_pdf
from venue_sources import ROBOTICS_YEARS, fetch_ieee, fetch_rss, fetch_acm, fetch_coling

BASE_DIR = Path(__file__).parent / "papers_data"
METADATA_DIR = BASE_DIR / "metadata"
PDF_DIR = BASE_DIR / "pdfs"
CACHE_FILE = BASE_DIR / "download_cache.json"

PAPERS_COOL_BASE = "https://papers.cool/venue"
PAPERS_COOL_PAGE_SIZE = 1000  # max papers per request (tested: show=1000 works)

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
REQUEST_TIMEOUT = 30
PDF_DOWNLOAD_TIMEOUT = 120
MAX_RETRIES = 3
RETRY_DELAY = 2  # seconds

METADATA_WORKERS = 4       # threads for fetching metadata pages
PDF_DOWNLOAD_WORKERS = 8   # threads for downloading PDFs
RATE_LIMIT_DELAY = 0.5     # seconds between requests per thread

# Conferences available on papers.cool (manually verified)
# Format: {conference_name: [available_years]}
PAPERS_COOL_VENUES = {
    # ML/AI flagship
    "NeurIPS":      [2022, 2023, 2024, 2025],
    "ICLR":         [2022, 2023, 2024, 2025, 2026],
    "ICML":         [2022, 2023, 2024, 2025],
    "AAAI":         [2022, 2023, 2024, 2025],
    # NLP
    "ACL":          [2022, 2023, 2024, 2025],
    "EMNLP":        [2022, 2023, 2024, 2025],
    "NAACL":        [2022, 2024, 2025],
    # CV
    "CVPR":         [2022, 2023, 2024, 2025],
    "ECCV":         [2022, 2024],
    "ICCV":         [2023, 2025],
    # Others
    "IJCAI":        [2022, 2023, 2024, 2025],
    "COLM":         [2024, 2025],
    "MICCAI":       [2024, 2025],
    "MLSYS":        [2022, 2023, 2024, 2025],
    "INTERSPEECH":  [2022, 2023, 2024, 2025],
}

# Venues not on papers.cool, fetched via the DBLP API
DBLP_VENUES = {
    "ACMMM":    {"dblp_key": "conf/mm", "years": [2022, 2023, 2024, 2025]},
    "KDD":      {"dblp_key": "conf/kdd", "years": [2022, 2023, 2024, 2025]},
    "SIGIR":    {"dblp_key": "conf/sigir", "years": [2022, 2023, 2024, 2025]},
    "COLING":   {"dblp_key": "conf/coling", "years": [2022, 2024, 2025]},
    "WWW":      {"dblp_key": "conf/www", "years": [2022, 2023, 2024, 2025]},
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

_shutdown_requested = False

def _signal_handler(signum, frame):
    global _shutdown_requested
    if _shutdown_requested:
        logger.warning("Force exit requested. Exiting immediately.")
        sys.exit(1)
    _shutdown_requested = True
    logger.warning("Ctrl+C received. Finishing current tasks and saving progress...")

signal.signal(signal.SIGINT, _signal_handler)


@dataclass
class PaperInfo:
    """Metadata for a single paper."""
    paper_id: str
    title: str
    authors: List[str]
    abstract: str
    pdf_url: str           # direct PDF download URL
    conference: str        # e.g. "NeurIPS"
    year: int              # e.g. 2024
    source: str            # "papers_cool" or "dblp"
    subject: str = ""      # e.g. "Oral", "Spotlight", etc.


def create_session() -> requests.Session:
    return pdf_download.create_session()


def resolve_pdf_url(paper: dict) -> Optional[str]:
    return pdf_download.canonical_pdf_url(paper) or None


def get_pdf_path(paper: dict) -> Path:
    return pdf_path(PDF_DIR, paper)


def paper_info(record):
    return PaperInfo(**{f.name: record[f.name] for f in fields(PaperInfo) if f.name in record})


def parse_papers_cool_page(html: str, conference: str, year: int) -> List[PaperInfo]:
    """
    Parse papers from a papers.cool HTML page using split-based approach.

    Each paper block starts with:
    <div id="aVh9KRZdRk@OpenReview" class="panel paper" keywords="...">
    """
    papers = []

    paper_starts = list(re.finditer(
        r'<div\s+id="([^"]+)"\s+class="panel paper"[^>]*>',
        html
    ))

    for i, match in enumerate(paper_starts):
        full_id = match.group(1)  # e.g. "aVh9KRZdRk@OpenReview"

        # Extract block content between this paper and the next
        start = match.end()
        end = paper_starts[i + 1].start() if i + 1 < len(paper_starts) else len(html)
        block = html[start:end]

        paper_id = full_id if conference == 'CoRL' else full_id.split('@')[0]

        title_match = re.search(
            r'class="title-link[^"]*"[^>]*>([^<]+)', block
        )
        title = title_match.group(1).strip() if title_match else ""

        # PDF URL lives in the data attribute of the pdf link
        pdf_match = re.search(r'class="title-pdf[^"]*"[^>]*data="([^"]+)"', block)
        if not pdf_match:
            # Fallback: any data attribute with pdf in the URL
            pdf_match = re.search(r'data="([^"]*(?:\.pdf|/pdf\?)[^"]*)"', block)
        pdf_url = pdf_match.group(1) if pdf_match else ""

        authors_match = re.search(
            r'class="metainfo authors[^"]*">(.*?)</p>', block, re.DOTALL
        )
        authors = []
        if authors_match:
            author_names = re.findall(
                r'class="author[^"]*"[^>]*>([^<]+)', authors_match.group(1)
            )
            authors = [a.strip() for a in author_names]

        summary_match = re.search(
            r'class="summary[^"]*">([^<]+)', block
        )
        abstract = summary_match.group(1).strip() if summary_match else ""

        # Subject: Oral, Spotlight, etc.
        subject_match = re.search(
            r'class="subject-\d+"[^>]*>([^<]+)', block
        )
        subject = subject_match.group(1).strip() if subject_match else ""

        if title:
            papers.append(PaperInfo(
                paper_id=paper_id,
                title=html_unescape(title),
                authors=[html_unescape(a) for a in authors],
                abstract=html_unescape(abstract),
                pdf_url=html_unescape(pdf_url),
                conference=conference,
                year=year,
                source="papers_cool",
                subject=subject,
            ))

    return papers


def fetch_venue_metadata(
    session: requests.Session,
    conference: str,
    year: int,
    pbar: Optional[tqdm] = None,
) -> List[PaperInfo]:
    """Fetch all papers for a given conference+year from papers.cool."""
    all_papers = []
    skip = 0
    total = 0

    url = f"{PAPERS_COOL_BASE}/{conference}.{year}?show={PAPERS_COOL_PAGE_SIZE}"
    logger.info(f"Fetching metadata for {conference}.{year} ...")

    while not _shutdown_requested:
        page_url = f"{url}&skip={skip}" if skip > 0 else url

        for retry in range(MAX_RETRIES):
            try:
                resp = session.get(page_url, timeout=REQUEST_TIMEOUT)
                resp.raise_for_status()
                break
            except Exception as e:
                if retry < MAX_RETRIES - 1:
                    logger.warning(f"Retry {retry+1}/{MAX_RETRIES} for {page_url}: {e}")
                    time.sleep(RETRY_DELAY * (retry + 1))
                else:
                    logger.error(f"Failed to fetch {page_url}: {e}")
                    raise RuntimeError(f'Incomplete metadata: {conference}.{year}') from e

        html = resp.text

        # Total count is only parsed from the first page
        if skip == 0:
            total_match = re.search(r'Total:\s*(\d+)', html)
            if not total_match:
                raise RuntimeError(f'Unrecognized metadata response: {conference}.{year}')
            total = int(total_match.group(1)) if total_match else 0
            if total == 0:
                logger.warning(f"No papers found for {conference}.{year}")
                return []
            logger.info(f"  {conference}.{year}: {total} papers total")
            if pbar is not None:
                pbar.total = total
                pbar.refresh()

        page_papers = parse_papers_cool_page(html, conference, year)
        if not page_papers:
            raise RuntimeError(f'Empty metadata page: {conference}.{year}, offset {skip}')

        all_papers.extend(page_papers)
        if pbar is not None:
            pbar.update(len(page_papers))

        logger.debug(f"  Fetched {len(all_papers)} papers so far (skip={skip})")

        skip += PAPERS_COOL_PAGE_SIZE

        if skip >= total:
            break

        time.sleep(RATE_LIMIT_DELAY)

    if _shutdown_requested:
        return []
    logger.info(f"  {conference}.{year}: collected {len(all_papers)} papers")
    return list({p.paper_id: p for p in all_papers}.values())


def fetch_all_papers_cool_metadata(
    conferences: Optional[List[str]] = None,
    years: Optional[List[int]] = None,
) -> Dict[str, List[PaperInfo]]:
    """
    Fetch metadata for all specified conference-year combinations from papers.cool.
    Returns dict: {"NeurIPS.2024": [PaperInfo, ...], ...}
    """
    session = create_session()
    results = {}

    tasks = []
    for conf, available_years in PAPERS_COOL_VENUES.items():
        if conferences and conf not in conferences:
            continue
        for y in available_years:
            if years and y not in years:
                continue
            tasks.append((conf, y))

    logger.info(f"Will fetch metadata for {len(tasks)} conference-year combinations")

    for conf, year in tasks:
        if _shutdown_requested:
            break

        key = f"{conf}.{year}"
        meta_file = METADATA_DIR / f"{key}.json"

        # Resume support: skip venues already fetched
        if meta_file.exists():
            logger.info(f"  Skipping {key} (metadata already exists)")
            existing = json.loads(meta_file.read_text())
            results[key] = [paper_info(p) for p in existing]
            continue

        pbar = tqdm(desc=f"  {key}", unit="paper", leave=True)
        papers = fetch_venue_metadata(session, conf, year, pbar)
        pbar.close()

        if papers:
            results[key] = papers
            meta_file.parent.mkdir(parents=True, exist_ok=True)
            meta_file.write_text(
                json.dumps([asdict(p) for p in papers], ensure_ascii=False, indent=2)
            )
            logger.info(f"  Saved {len(papers)} papers to {meta_file}")

    return results


def fetch_dblp_venue_metadata(
    session: requests.Session,
    conference: str,
    dblp_key: str,
    year: int,
) -> List[PaperInfo]:
    """
    Fetch paper list from DBLP API for a given venue and year.
    Supports pagination (DBLP max 1000 results per request).
    """
    all_papers = []
    page_size = 1000
    offset = 0

    logger.info(f"Fetching DBLP metadata for {conference} {year} ...")

    total = None
    while not _shutdown_requested:
        api_url = (
            f"https://dblp.org/search/publ/api?"
            f"q=stream%3Astreams%2F{dblp_key}%3A+year%3A{year}"
            f"&h={page_size}&f={offset}&format=json"
        )

        data = None
        for retry in range(MAX_RETRIES):
            try:
                resp = session.get(api_url, timeout=REQUEST_TIMEOUT)
                resp.raise_for_status()
                data = resp.json()
                break
            except Exception as e:
                if retry < MAX_RETRIES - 1:
                    logger.warning(f"Retry {retry+1}/{MAX_RETRIES} for DBLP {conference}.{year}: {e}")
                    time.sleep(RETRY_DELAY * (retry + 1))
                else:
                    logger.error(f"Failed to fetch DBLP {conference}.{year}: {e}")
                    raise RuntimeError(f'Incomplete DBLP metadata: {conference}.{year}') from e

        if data is None:
            break

        hits = data.get("result", {}).get("hits", {})
        if total is None:
            total = int(hits.get("@total", 0))
            if total == 0:
                logger.warning(f"No DBLP results for {conference}.{year}")
                return []
            logger.info(f"  DBLP {conference}.{year}: {total} results total")

        hit_list = hits.get("hit", [])
        if not hit_list:
            raise RuntimeError(f'Incomplete DBLP metadata: {conference}.{year}')

        for item in hit_list:
            info = item.get("info", {})
            title = info.get("title", "").rstrip(".")

            authors_info = info.get("authors", {}).get("author", [])
            if isinstance(authors_info, dict):
                authors_info = [authors_info]
            authors = [a.get("text", "") if isinstance(a, dict) else str(a) for a in authors_info]

            # Stable ID derived from the title
            paper_id = hashlib.md5(title.encode()).hexdigest()[:12]

            # DBLP provides DOI/ee links, not direct PDF URLs
            ee = info.get("ee", "")
            if isinstance(ee, list):
                ee = ee[0] if ee else ""
            pdf_url = ee  # DOI link; PDF download handled separately

            if title:
                all_papers.append(PaperInfo(
                    paper_id=paper_id,
                    title=title,
                    authors=authors,
                    abstract="",  # DBLP doesn't have abstracts
                    pdf_url=pdf_url,
                    conference=conference,
                    year=year,
                    source="dblp",
                ))

        offset += page_size
        if offset >= total:
            break
        time.sleep(RATE_LIMIT_DELAY)

    if _shutdown_requested:
        return []
    logger.info(f"  {conference}.{year}: collected {len(all_papers)} papers from DBLP")
    return all_papers


def fetch_all_dblp_metadata(
    conferences: Optional[List[str]] = None,
    years: Optional[List[int]] = None,
) -> Dict[str, List[PaperInfo]]:
    """Fetch metadata for DBLP-based venues."""
    session = create_session()
    results = {}

    for conf, config in DBLP_VENUES.items():
        if conferences and conf not in conferences:
            continue
        for y in config["years"]:
            if years and y not in years:
                continue
            if _shutdown_requested:
                break

            key = f"{conf}.{y}"
            meta_file = METADATA_DIR / f"{key}.json"

            if meta_file.exists():
                logger.info(f"  Skipping {key} (metadata already exists)")
                existing = json.loads(meta_file.read_text())
                results[key] = [paper_info(p) for p in existing]
                continue

            try:
                papers = fetch_dblp_venue_metadata(session, conf, config["dblp_key"], y)
                if not papers and not _shutdown_requested:
                    raise RuntimeError('Empty DBLP response')
            except RuntimeError:
                logger.warning('DBLP unavailable for %s.%s; trying publisher metadata', conf, y)
                records = fetch_coling(session, y) if conf == 'COLING' else fetch_acm(session, conf, y)
                papers = [paper_info(p) for p in records]

            if papers:
                results[key] = papers
                meta_file.parent.mkdir(parents=True, exist_ok=True)
                meta_file.write_text(
                    json.dumps([asdict(p) for p in papers], ensure_ascii=False, indent=2)
                )

    return results


def fetch_robotics_metadata(conferences=None, years=None, refresh=False):
    with create_session() as session:
        for conf, available_years in ROBOTICS_YEARS.items():
            if conferences and conf not in conferences:
                continue
            for year in available_years:
                if years and year not in years:
                    continue
                if _shutdown_requested:
                    return
                target = METADATA_DIR / f'{conf}.{year}.json'
                if target.exists() and not refresh:
                    continue
                if conf in {'ICRA', 'IROS'}:
                    papers = fetch_ieee(session, conf, year)
                elif conf == 'RSS':
                    papers = fetch_rss(session, year)
                else:
                    papers = [asdict(p) for p in fetch_venue_metadata(session, conf, year)]
                if not papers:
                    raise RuntimeError(f'No metadata for {conf}.{year}')
                records = [asdict(paper_info(p)) for p in papers]
                unique = {paper_key(p): p for p in records}
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = target.with_suffix('.tmp')
                temporary.write_text(json.dumps(list(unique.values()), ensure_ascii=False, indent=2))
                temporary.replace(target)
                logger.info('%s.%s: %s papers', conf, year, len(unique))


class DownloadCache:
    """Track downloaded PDFs to support resume."""

    def __init__(self, cache_path: Path):
        self.cache_path = cache_path
        self._lock = __import__("threading").Lock()
        self.downloaded: set = set()
        self.failed: dict = {}  # paper_id -> error message
        self.provenance = {}
        legacy_path = cache_path.parent / 'arxiv_resolve_cache.json'
        self.legacy_arxiv = json.loads(legacy_path.read_text()) if legacy_path.exists() else {}
        self._load()

    def _load(self):
        if self.cache_path.exists():
            data = json.loads(self.cache_path.read_text())
            if data.get('version') == 2:
                self.downloaded = set(data.get("downloaded", []))
                self.failed = data.get("failed", {})
                self.provenance = data.get('provenance', {})
            logger.info(f"Loaded download cache: {len(self.downloaded)} downloaded, {len(self.failed)} failed")

    def save(self):
        with self._lock:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.cache_path.with_suffix('.tmp')
            temporary.write_text(json.dumps({
                'version': 2,
                "downloaded": sorted(self.downloaded),
                "failed": self.failed,
                'provenance': self.provenance,
            }, indent=2))
            temporary.replace(self.cache_path)

    def is_downloaded(self, paper_id: str) -> bool:
        return paper_id in self.downloaded

    def can_reuse(self, paper):
        key = paper_key(paper)
        if self.provenance.get(key, {}).get('version') == 'publisher':
            return True
        return not self.legacy_arxiv.get(paper['paper_id'])

    def mark_downloaded(self, paper_id: str, provenance=None):
        with self._lock:
            self.downloaded.add(paper_id)
            self.failed.pop(paper_id, None)
            if provenance:
                self.provenance[paper_id] = provenance

    def mark_failed(self, paper_id: str, error: str):
        with self._lock:
            self.failed[paper_id] = error
            self.downloaded.discard(paper_id)


def download_single_pdf(session, paper, cache):
    key = paper_key(paper)
    if _shutdown_requested:
        return False, 'shutdown'
    path = get_pdf_path(paper)
    if valid_pdf(path) and cache.can_reuse(paper):
        cache.mark_downloaded(key)
        return True, 'exists'
    try:
        provenance = pdf_download.download(session, paper, path, lambda: _shutdown_requested)
        cache.mark_downloaded(key, provenance)
        return True, 'ok'
    except Exception as exc:
        cache.mark_failed(key, str(exc))
        return False, str(exc)


def download_pdfs(papers, max_workers=PDF_DOWNLOAD_WORKERS):
    cache = DownloadCache(CACHE_FILE)
    papers = list({paper_key(p): p for p in papers}.values())
    pending = []
    for paper in papers:
        if valid_pdf(get_pdf_path(paper)) and cache.can_reuse(paper):
            cache.mark_downloaded(paper_key(paper))
        else:
            pending.append(paper)
    logger.info('PDF download: %s total, %s available, %s pending',
                len(papers), len(papers) - len(pending), len(pending))
    local = threading.local()
    sessions = []
    lock = threading.Lock()

    def worker(paper):
        try:
            if not hasattr(local, 'session'):
                local.session = create_session()
                with lock:
                    sessions.append(local.session)
            return download_single_pdf(local.session, paper, cache)
        except Exception as exc:
            cache.mark_failed(paper_key(paper), str(exc))
            return False, str(exc)

    success = failed = 0
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        remaining = iter(pending)
        active = {}
        with tqdm(total=len(pending), desc='Downloading PDFs', unit='pdf') as progress:
            try:
                while not _shutdown_requested:
                    while len(active) < max_workers:
                        paper = next(remaining, None)
                        if paper is None:
                            break
                        active[executor.submit(worker, paper)] = paper
                    if not active:
                        break
                    completed, _ = wait(active, timeout=0.2, return_when=FIRST_COMPLETED)
                    for future in completed:
                        paper = active.pop(future)
                        ok, message = future.result()
                        success += int(ok)
                        failed += int(not ok)
                        if not ok:
                            logger.warning('%s %s %s: %s', paper['conference'], paper['year'], paper['paper_id'], message)
                        progress.update(1)
                        if (success + failed) % 20 == 0:
                            cache.save()
            finally:
                for future in active:
                    future.cancel()
    for session in sessions:
        session.close()
    cache.save()
    logger.info('Download complete: %s succeeded, %s failed', success, failed)


def load_all_metadata(
    conferences: Optional[List[str]] = None,
    years: Optional[List[int]] = None,
) -> List[PaperInfo]:
    """Load all saved metadata files."""
    all_papers = []
    if not METADATA_DIR.exists():
        return all_papers

    for meta_file in sorted(METADATA_DIR.glob("*.json")):
        key = meta_file.stem  # e.g. "NeurIPS.2024"
        parts = key.rsplit(".", 1)
        if len(parts) != 2:
            continue
        conf, year_str = parts

        if conferences and conf not in conferences:
            continue
        if years and int(year_str) not in years:
            continue

        data = json.loads(meta_file.read_text())
        papers = [paper_info(p) for p in data]
        all_papers.extend(papers)
        logger.info(f"  Loaded {len(papers)} papers from {meta_file.name}")

    return all_papers


def print_summary():
    """Print summary of collected data."""
    if not METADATA_DIR.exists():
        print("No metadata collected yet.")
        return

    total_papers = 0
    total_pdfs = 0

    print("\n" + "=" * 70)
    print(f"{'Conference':<15} {'Year':<6} {'Papers':<8} {'PDFs':<8}")
    print("-" * 70)

    for meta_file in sorted(METADATA_DIR.glob("*.json")):
        key = meta_file.stem
        parts = key.rsplit(".", 1)
        if len(parts) != 2:
            continue
        conf, year_str = parts

        data = json.loads(meta_file.read_text())
        n_papers = len(data)

        n_pdfs = sum(valid_pdf(get_pdf_path(p)) for p in data)

        total_papers += n_papers
        total_pdfs += n_pdfs

        print(f"{conf:<15} {year_str:<6} {n_papers:<8} {n_pdfs:<8}")

    print("-" * 70)
    print(f"{'TOTAL':<15} {'':<6} {total_papers:<8} {total_pdfs:<8}")
    print("=" * 70)

    if CACHE_FILE.exists():
        cache_data = json.loads(CACHE_FILE.read_text())
        print(f"\nDownload cache: {len(cache_data.get('downloaded', []))} downloaded, "
              f"{len(cache_data.get('failed', {}))} failed")


def main():
    parser = argparse.ArgumentParser(
        description="Crawl conference paper metadata and PDFs for survey research."
    )
    parser.add_argument(
        "--phase",
        choices=["metadata", "download", "all", "summary"],
        default="all",
        help="Which phase to run (default: all)",
    )
    parser.add_argument(
        "--conferences", "-c",
        nargs="+",
        default=sorted((set(PAPERS_COOL_VENUES) | set(DBLP_VENUES) | set(ROBOTICS_YEARS)) - {'MLSYS'}),
        help="Only process these conferences (e.g. NeurIPS ICLR CVPR)",
    )
    parser.add_argument(
        "--years", "-y",
        nargs="+",
        type=int,
        default=[2023, 2024, 2025, 2026],
        help="Only process these years (e.g. 2023 2024 2025)",
    )
    parser.add_argument(
        "--workers", "-w",
        type=int,
        default=PDF_DOWNLOAD_WORKERS,
        help=f"Number of download workers (default: {PDF_DOWNLOAD_WORKERS})",
    )
    parser.add_argument(
        "--source",
        choices=["papers_cool", "dblp", "robotics", "all"],
        default="all",
        help="Which data source to use for metadata (default: all)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable verbose/debug logging",
    )

    parser.add_argument('--proxy', default=pdf_download.PROXY)
    parser.add_argument('--cookies', default=pdf_download.COOKIE_FILE, help='Netscape-format publisher cookies')
    parser.add_argument('--browser', action='store_true', help='Optional Playwright Chromium fallback')
    parser.add_argument('--browser-profile', type=Path, default=pdf_download.BROWSER_PROFILE)
    parser.add_argument('--refresh-robotics', action='store_true', help='Re-fetch robotics metadata even if locally cached')
    parser.add_argument('--limit', type=int, help='Maximum PDFs to attempt for a smoke test')
    args = parser.parse_args()
    if args.workers < 1 or args.limit is not None and args.limit < 1:
        parser.error('--workers and --limit must be positive')
    pdf_download.PROXY = args.proxy
    pdf_download.COOKIE_FILE = args.cookies
    pdf_download.BROWSER = args.browser
    pdf_download.BROWSER_PROFILE = args.browser_profile

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    BASE_DIR.mkdir(parents=True, exist_ok=True)
    METADATA_DIR.mkdir(parents=True, exist_ok=True)
    PDF_DIR.mkdir(parents=True, exist_ok=True)

    if args.phase == "summary":
        print_summary()
        return

    if args.phase in ("metadata", "all"):
        logger.info("=" * 50)
        logger.info("Phase 1: Fetching paper metadata")
        logger.info("=" * 50)

        if args.source in ("papers_cool", "all"):
            fetch_all_papers_cool_metadata(args.conferences, args.years)

        if args.source in ("dblp", "all"):
            fetch_all_dblp_metadata(args.conferences, args.years)

        if args.source in ('robotics', 'all'):
            fetch_robotics_metadata(args.conferences, args.years, args.refresh_robotics)

        if _shutdown_requested:
            logger.warning("Metadata fetch interrupted. Progress has been saved.")
            return

    if args.phase in ("download", "all"):
        logger.info("=" * 50)
        logger.info("Phase 2: Downloading PDFs for ALL papers")
        logger.info("=" * 50)

        all_papers = load_all_metadata(args.conferences, args.years)
        if not all_papers:
            logger.warning("No metadata found. Run with --phase metadata first.")
            return

        paper_dicts = [asdict(p) for p in all_papers]

        if args.limit:
            paper_dicts = paper_dicts[:args.limit]

        if _shutdown_requested:
            logger.warning("Metadata fetch interrupted. Progress has been saved.")
            return

        logger.info(f"Downloading {len(paper_dicts)} PDFs with {args.workers} workers")
        download_pdfs(paper_dicts, max_workers=args.workers)

    print_summary()


if __name__ == "__main__":
    main()
