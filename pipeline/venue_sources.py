import hashlib
import re
import time
import xml.etree.ElementTree as ET
from urllib.parse import urljoin

from bs4 import BeautifulSoup

ROBOTICS_YEARS = {name: [2023, 2024, 2025] for name in ('CoRL', 'ICRA', 'IROS', 'RSS')}
IEEE_NAMES = {
    'ICRA': 'IEEE International Conference on Robotics and Automation',
    'IROS': 'IEEE/RSJ International Conference on Intelligent Robots and Systems',
}


def _crossref_items(session, container, prefix):
    cursor = '*'
    seen = set()
    while cursor not in seen:
        seen.add(cursor)
        response = session.get('https://api.crossref.org/works', params={
            'filter': f'container-title:{container},type:proceedings-article,prefix:{prefix}',
            'rows': 1000, 'cursor': cursor,
        }, timeout=90)
        response.raise_for_status()
        message = response.json()['message']
        items = message.get('items', [])
        yield from (item for item in items if container in item.get('container-title', []))
        if len(items) < 1000:
            return
        cursor = message.get('next-cursor')
        if not cursor or cursor in seen:
            raise RuntimeError(f'Incomplete Crossref pagination: {container}')
        time.sleep(1)


def fetch_acm(session, conference, year):
    def ordinal(number):
        suffix = 'th' if 10 <= number % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(number % 10, 'th')
        return f'{number}{suffix}'

    container = {
        'ACMMM': f'Proceedings of the {ordinal(year - 1992)} ACM International Conference on Multimedia',
        'KDD': f'Proceedings of the {ordinal(year - 1994)} ACM SIGKDD Conference on Knowledge Discovery and Data Mining',
        'SIGIR': f'Proceedings of the {ordinal(year - 1977)} International ACM SIGIR Conference on Research and Development in Information Retrieval',
        'WWW': f'Proceedings of the ACM Web Conference {year}',
    }[conference]
    containers = [container]
    if conference == 'KDD' and year >= 2025:
        containers = [container + f' V.{volume}' for volume in (1, 2)]
    papers = {}
    for name in containers:
        for item in _crossref_items(session, name, '10.1145'):
            title = BeautifulSoup(' '.join(item.get('title', [])), 'html.parser').get_text(' ', strip=True).rstrip('.')
            doi = item.get('DOI', '')
            if not title or not item.get('author') or not doi.lower().startswith('10.1145/'):
                continue
            papers[doi.lower()] = {
                'paper_id': hashlib.md5(title.encode()).hexdigest()[:12], 'title': title,
                'authors': [' '.join(filter(None, [a.get('given'), a.get('family')])) for a in item['author']],
                'abstract': BeautifulSoup(item.get('abstract', ''), 'html.parser').get_text(' ', strip=True),
                'pdf_url': f'https://doi.org/{doi}', 'conference': conference,
                'year': year, 'source': 'crossref_acm', 'subject': '',
            }
    if not papers:
        raise RuntimeError(f'No Crossref proceedings found: {conference}.{year}')
    return list(papers.values())


def fetch_coling(session, year):
    collection = f'{year}.lrec' if year == 2024 else f'{year}.coling'
    response = session.get(f'https://raw.githubusercontent.com/acl-org/acl-anthology/master/data/xml/{collection}.xml')
    response.raise_for_status()
    root = ET.fromstring(response.content)
    papers = []
    for volume in root.findall('volume'):
        if volume.get('id') not in {'1', 'main'}:
            continue
        for item in volume.findall('paper'):
            if not item.findall('author') or item.get('id') == '0':
                continue
            title = ''.join(item.find('title').itertext()).strip().rstrip('.')
            pid = f'{collection}-{volume.get("id")}.{item.get("id")}'
            abstract = item.find('abstract')
            papers.append({
                'paper_id': hashlib.md5(title.encode()).hexdigest()[:12], 'title': title,
                'authors': [' '.join(a.itertext()).strip() for a in item.findall('author')],
                'abstract': ''.join(abstract.itertext()).strip() if abstract is not None else '',
                'pdf_url': f'https://aclanthology.org/{pid}.pdf', 'conference': 'COLING',
                'year': year, 'source': 'acl_anthology', 'subject': 'Main Conference',
            })
    if not papers:
        raise RuntimeError(f'No ACL Anthology proceedings found: COLING.{year}')
    return papers


def fetch_ieee(session, conference, year):
    name = IEEE_NAMES[conference]
    container = f'{year} {name} ({conference})'
    cursor = '*'
    seen_cursors = set()
    papers = {}
    while cursor not in seen_cursors:
        seen_cursors.add(cursor)
        response = session.get('https://api.crossref.org/works', params={
            'filter': f'container-title:{container},type:proceedings-article,prefix:10.1109',
            'rows': 1000, 'cursor': cursor,
        }, timeout=90)
        response.raise_for_status()
        message = response.json()['message']
        items = message.get('items', [])
        for item in items:
            doi = item.get('DOI', '')
            venue = ' '.join(item.get('container-title', []))
            match = re.fullmatch(rf'10\.1109/{conference}[A-Za-z0-9.-]*\.(\d+)', doi, re.I)
            title = BeautifulSoup(' '.join(item.get('title', [])), 'html.parser').get_text(' ', strip=True)
            if not match or str(year) not in venue or name.lower() not in venue.lower():
                continue
            if not item.get('author'):
                continue
            papers[doi.lower()] = {
                'paper_id': match[1], 'title': title,
                'authors': [' '.join(filter(None, [a.get('given'), a.get('family')]))
                            for a in item.get('author', [])],
                'abstract': BeautifulSoup(item.get('abstract', ''), 'html.parser').get_text(' ', strip=True),
                'pdf_url': f'https://doi.org/{doi}', 'conference': conference,
                'year': year, 'source': 'crossref_ieee', 'subject': 'Main Conference',
            }
        if len(items) < 1000:
            break
        cursor = message.get('next-cursor')
        if not cursor or cursor in seen_cursors:
            raise RuntimeError(f'Incomplete Crossref pagination: {conference}.{year}')
        time.sleep(1)
    return list(papers.values())


def fetch_rss(session, year):
    base = f'https://www.roboticsproceedings.org/rss{year - 2004:02d}/'
    response = session.get(base)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, 'html.parser')
    papers = {}
    for link in soup.find_all('a', href=re.compile(r'^p\d+\.html$')):
        pid = link['href'].split('.')[0]
        if pid in papers:
            continue
        response = session.get(urljoin(base, link['href']))
        response.raise_for_status()
        page = BeautifulSoup(response.text, 'html.parser')
        def meta(name):
            return [tag['content'] for tag in page.find_all('meta', attrs={'name': name}) if tag.get('content')]
        label = page.find('b', string=lambda text: text and text.strip().lower() == 'abstract:')
        paragraph = label.find_parent('p').find_next_sibling('p') if label and label.find_parent('p') else None
        papers[pid] = {
            'paper_id': pid, 'title': (meta('citation_title') or [link.get_text(' ', strip=True)])[0],
            'authors': meta('citation_author'),
            'abstract': paragraph.get_text(' ', strip=True) if paragraph else '',
            'pdf_url': urljoin(base, pid + '.pdf'), 'conference': 'RSS',
            'year': year, 'source': 'rss_proceedings', 'subject': 'Main Conference',
        }
        time.sleep(0.2)
    return list(papers.values())
