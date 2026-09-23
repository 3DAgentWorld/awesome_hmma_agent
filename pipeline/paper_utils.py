import html
import json
import re
from pathlib import Path


def paper_key(paper):
    return json.dumps([paper.get('conference', ''), int(paper.get('year') or 0),
                       paper['paper_id']], ensure_ascii=False, separators=(',', ':'))


def pdf_path(root, paper):
    title = re.sub(r'[^\w\s-]', '', html.unescape(paper.get('title', '')))[:80].strip()
    title = re.sub(r'\s+', '_', title)
    pid = re.sub(r'[/\\\x00-\x1f]', '_', paper['paper_id'])
    conference = re.sub(r'[^\w.-]', '_', paper['conference'])
    return Path(root) / conference / str(int(paper['year'])) / f'{pid}_{title}.pdf'


def valid_pdf(path):
    try:
        with Path(path).open('rb') as stream:
            size = Path(path).stat().st_size
            if stream.read(5) != b'%PDF-' or size < 1000:
                return False
            stream.seek(max(0, size - 4096))
            return b'%%EOF' in stream.read()
    except OSError:
        return False
