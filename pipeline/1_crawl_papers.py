#!/usr/bin/env python3
"""
Crawl conference paper metadata and download the PDFs of ALL papers.

Phase 1 fetches paper metadata (title, authors, abstract, pdf_url) from
papers.cool and DBLP; Phase 2 downloads every paper's PDF with a thread
pool. Papers whose metadata has no direct PDF link (DBLP source carries
DOI links only) are first resolved to an arxiv PDF URL by title search
on papers.cool.

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
import time
import signal
import hashlib
import logging
import argparse
import threading
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict, field

import requests
from tqdm import tqdm

BASE_DIR = Path(__file__).parent / "papers_data"
METADATA_DIR = BASE_DIR / "metadata"
PDF_DIR = BASE_DIR / "pdfs"
CACHE_FILE = BASE_DIR / "download_cache.json"
ARXIV_RESOLVE_CACHE = BASE_DIR / "arxiv_resolve_cache.json"

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
RESOLVE_WORKERS = 8        # threads for papers.cool arxiv title search
RATE_LIMIT_DELAY = 0.5     # seconds between requests per thread

# Conferences available on papers.cool (manually verified)
# Format: {conference_name: [available_years]}
PAPERS_COOL_VENUES = {
    # ML/AI flagship
    "NeurIPS":      [2022, 2023, 2024, 2025],
    "ICLR":         [2022, 2023, 2024, 2025],
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
    "IJCAI":        [2022, 2023, 2024],
    "COLM":         [2024, 2025],
    "MICCAI":       [2024, 2025],
    "MLSYS":        [2022, 2023, 2024, 2025],
    "INTERSPEECH":  [2022, 2023, 2024, 2025],
}

# Venues not on papers.cool, fetched via the DBLP API
DBLP_VENUES = {
    "ACMMM":    {"dblp_key": "conf/mm", "years": [2022, 2023, 2024, 2025]},
    "KDD":      {"dblp_key": "conf/kdd", "years": [2022, 2023, 2024]},
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
    paper_id: str          # unique ID (openreview ID or hash)
    title: str
    authors: List[str]
    abstract: str
    pdf_url: str           # direct PDF download URL
    conference: str        # e.g. "NeurIPS"
    year: int              # e.g. 2024
    source: str            # "papers_cool" or "dblp"
    subject: str = ""      # e.g. "Oral", "Spotlight", etc.


def create_session() -> requests.Session:
    """Create a requests session with retry and proper headers."""
    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    })
    return session


def resolve_pdf_url(paper: dict) -> Optional[str]:
    """
    Resolve a downloadable PDF URL for a paper, in priority order:
    1. resolved_pdf_url (from papers.cool arxiv title search)
    2. aclanthology links with .pdf appended
    3. the original pdf_url (if directly downloadable)
    """
    resolved = paper.get("resolved_pdf_url", "")
    if resolved:
        return resolved

    pdf_url = paper.get("pdf_url", "")
    if not pdf_url:
        return None

    if "aclanthology.org" in pdf_url and not pdf_url.endswith(".pdf"):
        return pdf_url.rstrip("/") + ".pdf"

    # DOI links are not directly downloadable
    if "doi.org" in pdf_url and not pdf_url.endswith(".pdf"):
        return None

    return pdf_url


def get_pdf_path(paper: dict) -> Path:
    """Build the PDF save path: pdfs/{conference}/{year}/{paper_id}_{safe_title}.pdf"""
    safe_title = re.sub(r'[^\w\s-]', '', html.unescape(paper.get('title', '')))[:80].strip()
    safe_title = re.sub(r'\s+', '_', safe_title)
    return PDF_DIR / paper['conference'] / str(paper['year']) / f"{paper['paper_id']}_{safe_title}.pdf"


class PapersCoolResolver:
    """
    Search papers.cool /arxiv/search by title to obtain the arxiv PDF URL
    for papers without a direct PDF link (DBLP source).
    Results are cached locally to avoid repeat queries.
    """

    def __init__(self, cache_path: Path):
        self.cache_path = cache_path
        self._lock = threading.Lock()
        self.cache: Dict[str, Optional[str]] = {}  # paper_id -> pdf_url or None
        self._load()

    def _load(self):
        if self.cache_path.exists():
            self.cache = json.loads(self.cache_path.read_text())

    def save(self):
        with self._lock:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps(self.cache, indent=2))

    def resolve(self, session: requests.Session, paper_id: str, title: str) -> Optional[str]:
        """
        Search papers.cool for the paper title and return the arxiv PDF URL.
        Returns None if not found.
        """
        with self._lock:
            if paper_id in self.cache:
                return self.cache[paper_id]

        # Decode HTML entities and extract alphanumeric words
        clean_title = html.unescape(title)
        words = re.findall(r'[a-zA-Z0-9]+', clean_title)
        if not words:
            with self._lock:
                self.cache[paper_id] = None
            return None

        query = '+'.join(words)
        url = f"https://papers.cool/arxiv/search?query={query}&show=5"

        pdf_url = None

        for attempt in range(MAX_RETRIES):
            if _shutdown_requested:
                return None
            try:
                resp = session.get(url, timeout=20)
                if resp.status_code == 200:
                    # Parse HTML: <div id="2402.07945" class="panel paper">
                    panels = re.findall(
                        r'id="([^"]+)"\s+class="panel paper"', resp.text
                    )
                    result_titles = re.findall(
                        r'class="title-link[^"]*"[^>]*>([^<]+)', resp.text
                    )

                    # Fuzzy title matching via word-level Jaccard similarity
                    title_words = set(w.lower() for w in words if len(w) > 2)
                    for i, rt in enumerate(result_titles[:5]):
                        if i >= len(panels):
                            break
                        rt_words = set(
                            w.lower() for w in re.findall(r'[a-zA-Z0-9]+', rt)
                            if len(w) > 2
                        )
                        if not title_words or not rt_words:
                            continue
                        common = len(title_words & rt_words)
                        jaccard = common / len(title_words | rt_words)
                        if jaccard >= 0.75:
                            arxiv_id = panels[i]
                            pdf_url = f"https://arxiv.org/pdf/{arxiv_id}.pdf"
                            break

                    break  # request succeeded; no retry

                elif resp.status_code == 429:
                    time.sleep(2 * (attempt + 1))
                else:
                    break

            except Exception:
                if attempt < MAX_RETRIES - 1:
                    time.sleep(RETRY_DELAY * (attempt + 1))

        with self._lock:
            self.cache[paper_id] = pdf_url

        return pdf_url


def resolve_paper_pdf_urls(papers: List[dict], max_workers: int = RESOLVE_WORKERS) -> int:
    """
    Resolve a downloadable arxiv PDF URL for papers without a direct PDF
    link by title search on papers.cool. Multithreaded (papers.cool has
    no rate limit). Returns the number of papers with a resolved URL.
    """
    resolver = PapersCoolResolver(ARXIV_RESOLVE_CACHE)

    # Split into already-cached vs pending
    pending = []
    resolved = 0
    for paper in papers:
        pid = paper["paper_id"]
        with resolver._lock:
            if pid in resolver.cache:
                if resolver.cache[pid]:
                    paper["resolved_pdf_url"] = resolver.cache[pid]
                    resolved += 1
                continue
        pending.append(paper)

    logger.info(
        f"PDF URL resolve: {len(papers)} papers, "
        f"{len(papers) - len(pending)} cached ({resolved} with URL), {len(pending)} pending"
    )

    if not pending:
        resolver.save()
        return resolved

    # One session per thread
    thread_sessions = {}
    session_lock = threading.Lock()

    def _get_session():
        tid = threading.current_thread().ident
        with session_lock:
            if tid not in thread_sessions:
                thread_sessions[tid] = create_session()
            return thread_sessions[tid]

    success_count = 0
    pbar = tqdm(total=len(pending), desc="Resolving arxiv PDF URLs (papers.cool)", unit="paper")

    try:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {}
            for paper in pending:
                if _shutdown_requested:
                    break
                future = executor.submit(
                    resolver.resolve, _get_session(), paper["paper_id"], paper["title"]
                )
                futures[future] = paper

            for future in as_completed(futures):
                if _shutdown_requested:
                    break
                paper = futures[future]
                try:
                    pdf_url = future.result()
                    if pdf_url:
                        paper["resolved_pdf_url"] = pdf_url
                        success_count += 1
                except Exception:
                    pass

                pbar.update(1)
                pbar.set_postfix_str(
                    f"found={resolved + success_count}", refresh=False
                )
    finally:
        pbar.close()
        resolver.save()

    total_resolved = resolved + success_count
    logger.info(f"PDF URL resolve complete: {total_resolved}/{len(papers)} arxiv PDFs found")
    return total_resolved


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

        paper_id = full_id.split("@")[0] if "@" in full_id else full_id

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

        if title and pdf_url:
            papers.append(PaperInfo(
                paper_id=paper_id,
                title=title,
                authors=authors,
                abstract=abstract,
                pdf_url=pdf_url,
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
                    return all_papers

        html = resp.text

        # Total count is only parsed from the first page
        if skip == 0:
            total_match = re.search(r'Total:\s*(\d+)', html)
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
            break

        all_papers.extend(page_papers)
        if pbar is not None:
            pbar.update(len(page_papers))

        logger.debug(f"  Fetched {len(all_papers)} papers so far (skip={skip})")

        skip += PAPERS_COOL_PAGE_SIZE

        if len(all_papers) >= total:
            break

        time.sleep(RATE_LIMIT_DELAY)

    logger.info(f"  {conference}.{year}: collected {len(all_papers)} papers")
    return all_papers


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
            results[key] = [PaperInfo(**p) for p in existing]
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
                    return all_papers

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
            break

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
                results[key] = [PaperInfo(**p) for p in existing]
                continue

            papers = fetch_dblp_venue_metadata(session, conf, config["dblp_key"], y)

            if papers:
                results[key] = papers
                meta_file.parent.mkdir(parents=True, exist_ok=True)
                meta_file.write_text(
                    json.dumps([asdict(p) for p in papers], ensure_ascii=False, indent=2)
                )

    return results


class DownloadCache:
    """Track downloaded PDFs to support resume."""

    def __init__(self, cache_path: Path):
        self.cache_path = cache_path
        self._lock = __import__("threading").Lock()
        self.downloaded: set = set()
        self.failed: dict = {}  # paper_id -> error message
        self._load()

    def _load(self):
        if self.cache_path.exists():
            data = json.loads(self.cache_path.read_text())
            self.downloaded = set(data.get("downloaded", []))
            self.failed = data.get("failed", {})
            logger.info(f"Loaded download cache: {len(self.downloaded)} downloaded, {len(self.failed)} failed")

    def save(self):
        with self._lock:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps({
                "downloaded": sorted(self.downloaded),
                "failed": self.failed,
            }, indent=2))

    def is_downloaded(self, paper_id: str) -> bool:
        return paper_id in self.downloaded

    def mark_downloaded(self, paper_id: str):
        with self._lock:
            self.downloaded.add(paper_id)
            self.failed.pop(paper_id, None)

    def mark_failed(self, paper_id: str, error: str):
        with self._lock:
            self.failed[paper_id] = error


def download_single_pdf(
    session: requests.Session,
    paper: dict,
    cache: DownloadCache,
) -> Tuple[bool, str]:
    """
    Download a single PDF file.
    Returns (success: bool, message: str).
    """
    if _shutdown_requested:
        return False, "shutdown"

    paper_id = paper["paper_id"]

    if cache.is_downloaded(paper_id):
        return True, "cached"

    pdf_url = resolve_pdf_url(paper)
    if not pdf_url:
        cache.mark_failed(paper_id, "no_pdf_url")
        return False, "no_pdf_url"

    pdf_path = get_pdf_path(paper)
    pdf_path.parent.mkdir(parents=True, exist_ok=True)

    if pdf_path.exists() and pdf_path.stat().st_size > 1000:
        cache.mark_downloaded(paper_id)
        return True, "exists"

    for retry in range(MAX_RETRIES):
        if _shutdown_requested:
            return False, "shutdown"
        try:
            resp = session.get(
                pdf_url,
                timeout=PDF_DOWNLOAD_TIMEOUT,
                stream=True,
            )
            resp.raise_for_status()

            # Verify it's actually a PDF
            content_type = resp.headers.get("Content-Type", "")
            if "pdf" not in content_type and "octet-stream" not in content_type:
                # Some servers redirect; read first bytes to check
                first_chunk = next(resp.iter_content(1024), b"")
                if not first_chunk.startswith(b"%PDF"):
                    cache.mark_failed(paper_id, f"not_pdf: {content_type}")
                    return False, f"not_pdf: {content_type}"
                with open(pdf_path, "wb") as f:
                    f.write(first_chunk)
                    for chunk in resp.iter_content(chunk_size=8192):
                        if _shutdown_requested:
                            return False, "shutdown"
                        f.write(chunk)
            else:
                with open(pdf_path, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=8192):
                        if _shutdown_requested:
                            return False, "shutdown"
                        f.write(chunk)

            if pdf_path.stat().st_size < 1000:
                pdf_path.unlink(missing_ok=True)
                raise ValueError("PDF too small, likely error page")

            cache.mark_downloaded(paper_id)
            return True, "ok"

        except Exception as e:
            if retry < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY * (retry + 1))
            else:
                pdf_path.unlink(missing_ok=True)
                cache.mark_failed(paper_id, str(e))
                return False, str(e)

    return False, "max_retries"


def download_pdfs(
    papers: List[dict],
    max_workers: int = PDF_DOWNLOAD_WORKERS,
):
    """Download PDFs for all papers using thread pool."""
    cache = DownloadCache(CACHE_FILE)

    pending = [p for p in papers if not cache.is_downloaded(p["paper_id"])]
    logger.info(
        f"PDF download: {len(papers)} total, {len(papers) - len(pending)} cached, "
        f"{len(pending)} pending"
    )

    if not pending:
        logger.info("All PDFs already downloaded!")
        return

    success_count = 0
    fail_count = 0

    pbar = tqdm(total=len(pending), desc="Downloading PDFs", unit="pdf")

    # One session per thread for connection pooling
    sessions = {}

    def _get_session(thread_id):
        if thread_id not in sessions:
            sessions[thread_id] = create_session()
        return sessions[thread_id]

    try:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {}
            for paper in pending:
                if _shutdown_requested:
                    break
                thread_id = hash(paper["paper_id"]) % max_workers
                future = executor.submit(
                    download_single_pdf,
                    _get_session(thread_id),
                    paper,
                    cache,
                )
                futures[future] = paper

            for future in as_completed(futures):
                if _shutdown_requested:
                    break
                paper = futures[future]
                try:
                    ok, msg = future.result()
                    if ok:
                        success_count += 1
                    else:
                        fail_count += 1
                        if msg not in ("cached", "shutdown"):
                            logger.debug(f"Failed: {paper.title[:50]}... -> {msg}")
                except Exception as e:
                    fail_count += 1
                    logger.error(f"Exception downloading {paper['paper_id']}: {e}")

                pbar.update(1)

    except KeyboardInterrupt:
        logger.warning("Download interrupted by user")
    finally:
        pbar.close()
        cache.save()
        logger.info(f"Download complete: {success_count} succeeded, {fail_count} failed")
        logger.info(f"Cache saved to {CACHE_FILE}")


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
        papers = [PaperInfo(**p) for p in data]
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

        pdf_dir = PDF_DIR / conf / year_str
        n_pdfs = len(list(pdf_dir.glob("*.pdf"))) if pdf_dir.exists() else 0

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
        default=None,
        help="Only process these conferences (e.g. NeurIPS ICLR CVPR)",
    )
    parser.add_argument(
        "--years", "-y",
        nargs="+",
        type=int,
        default=None,
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
        choices=["papers_cool", "dblp", "all"],
        default="all",
        help="Which data source to use for metadata (default: all)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable verbose/debug logging",
    )

    args = parser.parse_args()

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

        # Resolve an arxiv PDF URL for papers without a direct PDF link
        # (DBLP source carries DOI links only)
        need_resolve = [p for p in paper_dicts if not resolve_pdf_url(p)]
        if need_resolve:
            logger.info("=" * 50)
            logger.info(f"Resolving PDF URLs for {len(need_resolve)} papers without a direct link")
            logger.info("=" * 50)
            resolve_paper_pdf_urls(need_resolve, max_workers=RESOLVE_WORKERS)

            if _shutdown_requested:
                logger.warning("PDF URL resolution interrupted. Progress has been saved.")
                return

        if _shutdown_requested:
            logger.warning("Metadata fetch interrupted. Progress has been saved.")
            return

        logger.info(f"Downloading {len(paper_dicts)} PDFs with {args.workers} workers")
        download_pdfs(paper_dicts, max_workers=args.workers)

    print_summary()


if __name__ == "__main__":
    main()
