#!/usr/bin/env python3
"""
Supplementary annotation script: labels papers present in taxonomy_final but
missing from annotations/ with the interface_type field only
(Symbolic / Continuous / Mixed).

Output: papers_data/annotations_interface_supplement.jsonl
Each line: {"paper_id": ..., "interface_type": ..., "interface_details": ...}
Supports resume (already-written papers are skipped).
"""

import json
import os
import re
import sys
import time
import uuid
import html
import threading
import glob
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import fitz  # PyMuPDF
import requests
from tqdm import tqdm


BASE_DIR = Path(__file__).parent
TAXONOMY_DIR = BASE_DIR / 'papers_data' / 'taxonomy_final'
ANNOTATIONS_DIR = BASE_DIR / 'papers_data' / 'annotations'
METADATA_DIR = BASE_DIR / 'papers_data' / 'metadata'
PDF_DIR = BASE_DIR / 'papers_data' / 'pdfs'
OUTPUT_PATH = BASE_DIR / 'papers_data' / 'annotations_interface_supplement.jsonl'

LOCAL_API_URL = 'http://YOUR_API_HOST:PORT/v1/chat/completions'
LOCAL_API_TOKEN = 'Bearer YOUR_API_KEY'
LOCAL_API_WSID = 'YOUR_WSID'

MODEL_NAME = 'kimi-k2.6-0507'
WORKERS = 20

GEN_PARAMS = {
    'temperature': 0.1,
    'top_p': 0.9,
    'top_k': 20,
    'repetition_penalty': 1.0,
    'output_seq_len': 10384,
    'max_input_seq_len': 65536,
}

MAX_PRE_REF_PAGES = 13
MAX_TEXT_CHARS = 60000
TIMEOUT = (30, 180)

_session = requests.Session()
adapter = requests.adapters.HTTPAdapter(pool_connections=64, pool_maxsize=64, max_retries=3)
_session.mount('http://', adapter)
_session.mount('https://', adapter)


SYS_PROMPT = """You are an expert AI researcher labeling papers for a survey on Heterogeneous Multi-Model Agents (HMMA).
Given the paper text below, decide how heterogeneous models in the system communicate with each other.

Output STRICT JSON only, in the form:
{
  "interface_type": "<Symbolic | Continuous | Mixed>",
  "interface_details": "<one short sentence: what is passed between models>"
}

Definitions:
- Symbolic: models communicate via text, JSON, bounding boxes, labels, segmentation masks, OCR strings, code, or other discrete structured tokens.
- Continuous: models communicate by passing embeddings / feature vectors / hidden states / soft prompts directly through trained adapters or projectors (e.g. Q-Former, visual projector, ControlNet-style conditional branches).
- Mixed: the system uses BOTH discrete symbolic and continuous-feature interfaces between different module pairs.

If the system only uses ONE central LLM with no other heterogeneous models, return "Symbolic" with details "single-model system, no inter-model interface" (this should be rare).
Be precise and base your label only on what the paper actually describes.
Respond with ONLY the JSON object, no markdown, no extra commentary."""


_REF_PATTERNS = [
    re.compile(r'(?:^|\n)\s*(References|REFERENCES|Bibliography|BIBLIOGRAPHY)\s*(?:\n|$)', re.MULTILINE),
    re.compile(r'(?:^|\n)\s*\d+[\.\s]+(References|REFERENCES)\s*(?:\n|$)', re.MULTILINE),
]


def extract_pre_ref_text(pdf_path: str) -> str:
    try:
        doc = fitz.open(pdf_path)
    except Exception:
        return ''
    total = doc.page_count
    ref_idx = None
    ref_off = None
    for pn in range(total):
        text = doc[pn].get_text()
        for pat in _REF_PATTERNS:
            m = pat.search(text)
            if m:
                ref_idx, ref_off = pn, m.start()
                break
        if ref_idx is not None:
            break
    out = ''
    if ref_idx is not None:
        for pn in range(ref_idx + 1):
            t = doc[pn].get_text()
            out += t[:ref_off] if pn == ref_idx else t
    else:
        for pn in range(min(MAX_PRE_REF_PAGES, total)):
            out += doc[pn].get_text()
    doc.close()
    if len(out) > MAX_TEXT_CHARS:
        out = out[:MAX_TEXT_CHARS] + '\n\n[... text truncated ...]'
    return out


def call_llm(user_content: str) -> str:
    headers = {
        'Content-Type': 'application/json',
        'Authorization': LOCAL_API_TOKEN,
        'Wsid': LOCAL_API_WSID,
    }
    body = {
        'model': MODEL_NAME,
        'query_id': 'iface_' + str(uuid.uuid4()),
        'messages': [
            {'role': 'system', 'content': SYS_PROMPT},
            {'role': 'user', 'content': user_content},
        ],
        'stream': False,
        'random_seed': 42,
        'openai_infer': True,
        **GEN_PARAMS,
    }
    try:
        r = _session.post(LOCAL_API_URL, headers=headers, json=body, timeout=TIMEOUT)
        if r.status_code != 200:
            return ''
        j = r.json()
        if 'error' in j:
            return ''
        return j['choices'][0]['message']['content'] or ''
    except Exception:
        return ''


def parse_label(text: str):
    if not text:
        return None, None
    # strip a possible ```json fence
    s = text.strip()
    s = re.sub(r'^```(?:json)?\s*', '', s)
    s = re.sub(r'\s*```$', '', s)
    m = re.search(r'\{.*\}', s, re.DOTALL)
    if not m:
        return None, None
    try:
        d = json.loads(m.group(0))
        it = d.get('interface_type', '').strip()
        if it not in ('Symbolic', 'Continuous', 'Mixed'):
            return None, None
        return it, d.get('interface_details', '')
    except Exception:
        return None, None


def load_target_papers():
    """Find papers present in taxonomy_final but missing from annotations."""
    final_pids = {}
    for f in TAXONOMY_DIR.glob('*.jsonl'):
        for line in open(f):
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            if d.get('taxonomy_status') == 'OK':
                final_pids[d['paper_id']] = d

    annotated = set()
    for f in ANNOTATIONS_DIR.glob('annotate_*.jsonl'):
        for line in open(f):
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            if d.get('paper_id'):
                annotated.add(d['paper_id'])

    missing = [final_pids[p] for p in final_pids if p not in annotated]
    return missing


def build_pdf_map(target_papers):
    """Build a paper_id -> pdf_path mapping from metadata."""
    needed = {p['paper_id'] for p in target_papers}
    pdf_map = {}
    for meta_file in METADATA_DIR.glob('*.json'):
        try:
            data = json.loads(meta_file.read_text())
        except Exception:
            continue
        stem = meta_file.stem
        parts = stem.rsplit('.', 1)
        if len(parts) != 2:
            continue
        venue, year = parts
        for p in data:
            pid = p.get('paper_id')
            if pid not in needed:
                continue
            safe_title = re.sub(r'[^\w\s-]', '', html.unescape(p.get('title', '')))[:80].strip()
            safe_title = re.sub(r'\s+', '_', safe_title)
            pdf_path = PDF_DIR / venue / year / f'{pid}_{safe_title}.pdf'
            if pdf_path.exists():
                pdf_map[pid] = str(pdf_path)
    return pdf_map


def process_one(paper, pdf_path):
    pid = paper['paper_id']
    title = paper.get('title', '')
    text = extract_pre_ref_text(pdf_path)
    if not text or len(text) < 500:
        return {'paper_id': pid, 'title': title, 'interface_type': '', 'interface_details': '',
                'error': 'pdf_extract_failed'}
    user = f'Title: {title}\n\n--- Paper text ---\n{text}'
    for attempt in range(3):
        out = call_llm(user)
        it, det = parse_label(out)
        if it:
            return {'paper_id': pid, 'title': title,
                    'interface_type': it, 'interface_details': det}
        time.sleep(1.0)
    return {'paper_id': pid, 'title': title, 'interface_type': '', 'interface_details': '',
            'error': 'llm_failed_after_retries'}


def load_done():
    done = set()
    if OUTPUT_PATH.exists():
        for line in open(OUTPUT_PATH):
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                if d.get('interface_type'):
                    done.add(d['paper_id'])
            except Exception:
                pass
    return done


def main():
    targets = load_target_papers()
    print(f'[info] taxonomy_final missing in annotations: {len(targets)} papers')
    pdf_map = build_pdf_map(targets)
    print(f'[info] resolved pdf for {len(pdf_map)}/{len(targets)} papers')

    done = load_done()
    pending = [p for p in targets if p['paper_id'] not in done and p['paper_id'] in pdf_map]
    print(f'[info] already done: {len(done)}, pending: {len(pending)}')

    if not pending:
        print('[info] nothing to do.')
        return

    write_lock = threading.Lock()
    fout = open(OUTPUT_PATH, 'a')

    def write_record(rec):
        with write_lock:
            fout.write(json.dumps(rec, ensure_ascii=False) + '\n')
            fout.flush()

    pbar = tqdm(total=len(pending), desc='annotate iface')
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        fut_to_p = {pool.submit(process_one, p, pdf_map[p['paper_id']]): p for p in pending}
        for fut in as_completed(fut_to_p):
            try:
                rec = fut.result()
            except Exception as e:
                p = fut_to_p[fut]
                rec = {'paper_id': p['paper_id'], 'title': p.get('title', ''),
                       'interface_type': '', 'interface_details': '', 'error': str(e)}
            write_record(rec)
            pbar.update(1)
    pbar.close()
    fout.close()
    print('[done]', OUTPUT_PATH)


if __name__ == '__main__':
    main()
