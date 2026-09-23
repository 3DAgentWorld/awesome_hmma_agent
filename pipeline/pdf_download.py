import html
import http.cookiejar
import os
import re
import threading
import time
import uuid
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

from bs4 import BeautifulSoup
from curl_cffi import requests

from paper_utils import valid_pdf

PROXY = os.environ.get('PAPER_PROXY', '')
COOKIE_FILE = os.environ.get('PAPER_COOKIES', '')
BROWSER = False
BROWSER_PROFILE = Path(__file__).parent / 'papers_data' / 'browser_profile'
_browser_lock = threading.Lock()
_rate_lock = threading.Lock()
_next_request = {}
_cooldowns = {}
_pmlr_lock = threading.Lock()
_pmlr = {}
CORL_VOLUMES = {2023: 229, 2024: 270, 2025: 305}


def create_session():
    session = requests.Session(impersonate='chrome', timeout=45)
    if PROXY:
        session.proxies.update({'http': PROXY, 'https': PROXY})
    if COOKIE_FILE:
        jar = http.cookiejar.MozillaCookieJar(COOKIE_FILE)
        jar.load(ignore_discard=True)
        session.cookies.update(jar)
    return session


def normalized_title(title):
    return ''.join(c for c in html.unescape(title).casefold() if c.isalnum())


def _pmlr_corl_url(session, paper):
    volume = CORL_VOLUMES.get(int(paper['year']))
    if not volume:
        return ''
    with _pmlr_lock:
        if volume not in _pmlr:
            response = session.get(f'https://proceedings.mlr.press/v{volume}/')
            response.raise_for_status()
            soup = BeautifulSoup(response.text, 'html.parser')
            index = {}
            for block in soup.select('div.paper'):
                title = block.select_one('p.title')
                link = next((a for a in block.select('a[href]')
                             if a.get_text(strip=True).lower() == 'download pdf'), None)
                if title and link:
                    key = normalized_title(title.get_text(' ', strip=True))
                    index.setdefault(key, []).append(urljoin(response.url, link['href']))
            if not index:
                raise ValueError('PMLR index could not be parsed')
            _pmlr[volume] = index
        matches = _pmlr[volume].get(normalized_title(paper.get('title', '')), [])
    return matches[0] if len(matches) == 1 else ''


def canonical_pdf_url(paper):
    url = html.unescape(paper.get('pdf_url') or '')
    parsed = urlparse(url)
    host = (parsed.hostname or '').lower()
    if host == 'aclanthology.org' and not parsed.path.endswith('.pdf'):
        return urljoin(url, parsed.path.rstrip('/') + '.pdf')
    if host in {'doi.org', 'dx.doi.org', 'dl.acm.org'}:
        doi = re.search(r'10\.1145/[^?#]+', url, re.I)
        if doi:
            return f'https://dl.acm.org/doi/pdf/{doi[0]}'
    if host in {'doi.org', 'dx.doi.org', 'ieeexplore.ieee.org'}:
        number = parse_qs(parsed.query).get('arnumber', [''])[0]
        if not number:
            match = re.search(r'/document/(\d+)|10\.1109/[^?#]+\.(\d+)$', url, re.I)
            number = next((v for v in match.groups() if v), '') if match else ''
        if number:
            return f'https://ieeexplore.ieee.org/stampPDF/getPDF.jsp?tp=&arnumber={number}'
    if host.endswith('openreview.net') and parsed.path in {'/forum', '/pdf'}:
        oid = parse_qs(parsed.query).get('id', [''])[0]
        if oid:
            return 'https://openreview.net/pdf?' + urlencode({'id': oid})
    return url


def candidate_urls(session, paper):
    url = canonical_pdf_url(paper)
    urls = []
    if paper.get('conference') == 'CoRL':
        try:
            official = _pmlr_corl_url(session, paper)
            if official:
                urls.append(official)
        except Exception:
            pass
    if url:
        urls.append(url)
    parsed = urlparse(url)
    if (parsed.hostname or '') in {'openreview.net', 'api.openreview.net', 'api2.openreview.net'}:
        oid = parse_qs(parsed.query).get('id', [''])[0]
        if oid:
            urls.extend(f'https://{host}/pdf?{urlencode({"id": oid})}'
                        for host in ('api2.openreview.net', 'api.openreview.net'))
    if parsed.hostname == 'dl.acm.org':
        urls.append(url.replace('/doi/pdf/', '/doi/epdf/'))
    for candidate in list(urls):
        match = re.fullmatch(r'https://proceedings.mlr.press/(v\d+)/([^/]+)/[^/]+\.pdf', candidate)
        if match:
            urls.append(f'https://raw.githubusercontent.com/mlresearch/{match[1]}/main/assets/{match[2]}/{match[2]}.pdf')
        match = re.fullmatch(r'https://raw.githubusercontent.com/mlresearch/(v\d+)/[^/]+/assets/([^/]+)/[^/]+\.pdf', candidate)
        if match:
            urls.append(f'https://proceedings.mlr.press/{match[1]}/{match[2]}/{match[2]}.pdf')
    urls = list(dict.fromkeys(urls))
    if paper.get('conference') == 'CoRL':
        urls.sort(key=lambda value: (
            'openreview.net' in (urlparse(value).hostname or ''),
            urlparse(value).hostname == 'raw.githubusercontent.com',
        ))
    return urls


def _rate_key(url):
    host = urlparse(url).hostname or ''
    return 'openreview.net' if host.endswith('.openreview.net') else host


def _wait_turn(url, stopped):
    host = _rate_key(url)
    with _rate_lock:
        now = time.time()
        if _cooldowns.get(host, 0) > now:
            raise RuntimeError(f'{host}: rate limited until {int(_cooldowns[host])}; retry later')
        ready = max(now, _next_request.get(host, 0))
        _next_request[host] = ready + 1
    while time.time() < ready:
        if stopped():
            raise InterruptedError('shutdown')
        time.sleep(min(0.2, max(0, ready - time.time())))
    with _rate_lock:
        if _cooldowns.get(host, 0) > time.time():
            raise RuntimeError(f'{host}: rate limited; retry later')


def _set_cooldown(url, response):
    value = response.headers.get('Retry-After', '')
    try:
        delay = float(value)
    except ValueError:
        try:
            delay = parsedate_to_datetime(value).timestamp() - time.time()
        except (TypeError, ValueError, OverflowError):
            delay = 300
    with _rate_lock:
        _cooldowns[_rate_key(url)] = time.time() + max(1, delay)


def _stream_pdf(response, target, stopped):
    partial = target.with_name(f'.{uuid.uuid4().hex}.part')
    total = 0
    checked_header = False
    started = time.monotonic()
    try:
        with partial.open('wb') as stream:
            for chunk in response.iter_content(chunk_size=65536):
                if stopped():
                    raise InterruptedError('shutdown')
                if time.monotonic() - started > 180 or total + len(chunk) > 200 * 1024 * 1024:
                    raise RuntimeError('PDF exceeds download limits')
                stream.write(chunk)
                total += len(chunk)
                if total >= 5 and not checked_header:
                    stream.flush()
                    with partial.open('rb') as check:
                        if check.read(5) != b'%PDF-':
                            raise ValueError('response is HTML or a challenge, not PDF')
                    checked_header = True
        if not valid_pdf(partial):
            raise ValueError('invalid or incomplete PDF')
        partial.replace(target)
    finally:
        partial.unlink(missing_ok=True)


def _browser_pdf(url, target, stopped):
    from playwright.sync_api import sync_playwright

    with _browser_lock, sync_playwright() as playwright:
        options = {'headless': False, 'accept_downloads': True}
        if PROXY:
            options['proxy'] = {'server': PROXY}
        with playwright.chromium.launch_persistent_context(str(BROWSER_PROFILE), **options) as context:
            page = context.new_page()
            blobs = []

            def response_pdf(response):
                if 'application/pdf' in response.headers.get('content-type', ''):
                    try:
                        blobs.append(response.body())
                    except Exception:
                        pass

            page.on('response', response_pdf)
            downloads = []
            page.on('download', downloads.append)
            try:
                page.goto(url, wait_until='domcontentloaded', timeout=60000)
            except Exception:
                pass
            for _ in range(45):
                if stopped():
                    raise InterruptedError('shutdown')
                if blobs or downloads:
                    break
                page.wait_for_timeout(1000)
            partial = target.with_name(f'.{uuid.uuid4().hex}.part')
            try:
                if downloads:
                    downloads[0].save_as(str(partial))
                elif blobs:
                    partial.write_bytes(blobs[0])
                if not valid_pdf(partial):
                    raise RuntimeError('browser did not return a PDF; publisher login or challenge still required')
                partial.replace(target)
            finally:
                partial.unlink(missing_ok=True)


def download(session, paper, target, stopped=lambda: False):
    target.parent.mkdir(parents=True, exist_ok=True)
    urls = candidate_urls(session, paper)
    if not urls:
        raise ValueError('no publisher PDF URL')
    errors = []
    browser_url = ''
    for url in urls:
        for attempt in range(2):
            if stopped():
                raise InterruptedError('shutdown')
            response = None
            try:
                _wait_turn(url, stopped)
                response = session.get(url, stream=True, timeout=120, headers={'Referer': url})
                if response.status_code in {429, 503}:
                    _set_cooldown(url, response)
                response.raise_for_status()
                _stream_pdf(response, target, stopped)
                return {'url': response.url, 'method': 'http_chrome', 'version': 'publisher'}
            except InterruptedError:
                raise
            except Exception as exc:
                status = response.status_code if response is not None else None
                errors.append(f'{urlparse(url).hostname}: {type(exc).__name__} (HTTP {status})')
                if status in {401, 403, 418} or isinstance(exc, ValueError):
                    browser_url = browser_url or url
                if (status is not None and status < 500) or isinstance(exc, RuntimeError):
                    break
                if attempt == 0:
                    time.sleep(2)
            finally:
                if response is not None:
                    response.close()
    if BROWSER and browser_url and _cooldowns.get(_rate_key(browser_url), 0) <= time.time():
        _browser_pdf(browser_url, target, stopped)
        return {'url': browser_url, 'method': 'browser', 'version': 'publisher'}
    raise RuntimeError('; '.join(errors[-4:]) + '; try --proxy or --cookies / --browser with publisher access')
