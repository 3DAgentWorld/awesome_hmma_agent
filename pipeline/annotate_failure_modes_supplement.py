#!/usr/bin/env python3
"""
Supplementary annotation script for HMMA survey writing.

Reads papers from papers_data/taxonomy_final, extracts local PDF text before
references, and asks the LLM API to label writing-focused fields useful for
expanding the ACM survey: concrete_failure_modes, evaluation_gap,
uncertainty_signal, interface_bottleneck, useful_evidence_passage, survey_use.

Supports resume. Example:
    python annotate_failure_modes_supplement.py --limit 80 --workers 8
"""

import argparse
import html
import json
import random
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import fitz
import requests
from tqdm import tqdm

BASE_DIR = Path(__file__).parent
TAXONOMY_DIR = BASE_DIR / 'papers_data' / 'taxonomy_final'
METADATA_DIR = BASE_DIR / 'papers_data' / 'metadata'
PDF_DIR = BASE_DIR / 'papers_data' / 'pdfs'
OUTPUT_PATH = BASE_DIR / 'papers_data' / 'failure_modes_supplement.jsonl'

LOCAL_API_URL = 'http://YOUR_API_HOST:PORT/v1/chat/completions'
LOCAL_API_TOKEN = 'Bearer YOUR_API_KEY'
LOCAL_API_WSID = 'YOUR_WSID'
MODEL_NAME = 'kimi-k2.6-0507'

MAX_PRE_REF_PAGES = 13
MAX_TEXT_CHARS = 50000
TIMEOUT = (30, 300)

GEN_PARAMS = {
    'temperature': 0.1,
    'top_p': 0.9,
    'top_k': 20,
    'repetition_penalty': 1.0,
    'output_seq_len': 4096,
    'max_input_seq_len': 65536,
}

SYS_PROMPT = """You are annotating papers for an ACM Computing Surveys article on Heterogeneous Multi-Model Agents (HMMA).
Return STRICT JSON only.

Given a paper text and an existing taxonomy annotation, extract writing-focused evidence for a survey. Use simple, factual wording. Do not invent details.

Output schema:
{
  "concrete_failure_modes": ["short phrase", "short phrase"],
  "evaluation_gap": "one short sentence about what the paper does or does not evaluate",
  "uncertainty_signal": "one short sentence about confidence, verification, retry, fallback, human feedback, or none",
  "interface_bottleneck": "one short sentence about what is passed between models and what may be lost",
  "useful_evidence_passage": "one concise passage from the paper text, at most 80 words, useful for survey writing",
  "survey_use": "one of: taxonomy, architecture, applications, challenges, appendix"
}

If the paper does not discuss one field, write "not stated". Keep the answer concise."""

_REF_PATTERNS = [
    re.compile(r'(?:^|\n)\s*(References|REFERENCES|Bibliography|BIBLIOGRAPHY)\s*(?:\n|$)', re.MULTILINE),
    re.compile(r'(?:^|\n)\s*\d+[\.\s]+(References|REFERENCES)\s*(?:\n|$)', re.MULTILINE),
]

thread_local = threading.local()
write_lock = threading.Lock()

# Global token statistics
_stats_lock = threading.Lock()
_total_input_tokens = 0
_total_output_tokens = 0
_total_reasoning_tokens = 0
_total_llm_time = 0.0  # seconds


def get_session():
    if not hasattr(thread_local, 'session'):
        s = requests.Session()
        adapter = requests.adapters.HTTPAdapter(pool_connections=32, pool_maxsize=32, max_retries=3)
        s.mount('http://', adapter)
        s.mount('https://', adapter)
        thread_local.session = s
    return thread_local.session


def extract_pre_ref_text(pdf_path: Path) -> str:
    try:
        doc = fitz.open(str(pdf_path))
    except Exception:
        return ''
    ref_idx = None
    ref_off = None
    for pn in range(doc.page_count):
        text = doc[pn].get_text()
        for pat in _REF_PATTERNS:
            m = pat.search(text)
            if m:
                ref_idx = pn
                ref_off = m.start()
                break
        if ref_idx is not None:
            break
    chunks = []
    if ref_idx is not None:
        for pn in range(ref_idx + 1):
            t = doc[pn].get_text()
            chunks.append(t[:ref_off] if pn == ref_idx else t)
    else:
        for pn in range(min(MAX_PRE_REF_PAGES, doc.page_count)):
            chunks.append(doc[pn].get_text())
    doc.close()
    text = '\n'.join(chunks)
    if len(text) > MAX_TEXT_CHARS:
        text = text[:MAX_TEXT_CHARS] + '\n\n[... text truncated ...]'
    return text


def safe_pdf_name(title: str) -> str:
    safe = re.sub(r'[^\w\s-]', '', html.unescape(title))[:80].strip()
    return re.sub(r'\s+', '_', safe)


def load_taxonomy_records():
    records = []
    for fp in sorted(TAXONOMY_DIR.glob('taxonomy_*.jsonl')):
        with fp.open(encoding='utf-8') as f:
            for line in f:
                if not line.strip():
                    continue
                rec = json.loads(line)
                if rec.get('taxonomy_status') == 'OK':
                    records.append(rec)
    return records


def build_pdf_map(records):
    needed = {r['paper_id'] for r in records}
    pdf_map = {}
    for meta_file in METADATA_DIR.glob('*.json'):
        parts = meta_file.stem.rsplit('.', 1)
        if len(parts) != 2:
            continue
        venue, year = parts
        try:
            data = json.loads(meta_file.read_text())
        except Exception:
            continue
        for p in data:
            pid = p.get('paper_id')
            if pid not in needed:
                continue
            path = PDF_DIR / venue / year / f"{pid}_{safe_pdf_name(p.get('title', ''))}.pdf"
            if path.exists() and path.stat().st_size > 1000:
                pdf_map[pid] = path
    return pdf_map


def load_done():
    done = set()
    failed = set()
    if OUTPUT_PATH.exists():
        with OUTPUT_PATH.open(encoding='utf-8') as f:
            for line in f:
                if line.strip():
                    try:
                        rec = json.loads(line)
                        paper_id = rec.get('paper_id')
                        if not paper_id:
                            continue
                        if rec.get('error'):
                            failed.add(paper_id)
                        else:
                            done.add(paper_id)
                    except Exception:
                        pass
    failed -= done
    return done, failed


def _parse_stream_response(resp) -> dict:
    """Parse SSE stream response. Returns dict with content, reasoning, and usage info."""
    full_content = ''
    full_reasoning = ''
    usage = {}
    for line in resp.iter_lines():
        if not line:
            continue
        decoded_line = line.decode('utf-8')
        if decoded_line.startswith('data:'):
            data_str = decoded_line[5:].strip()
        elif decoded_line.startswith('data :'):
            data_str = decoded_line[6:].strip()
        else:
            continue

        if data_str == '[DONE]':
            break

        try:
            chunk_json = json.loads(data_str)
            if 'error' in chunk_json:
                error_str = str(chunk_json['error'])
                if 'model info not found or no available instances' in error_str:
                    raise RuntimeError(f'model_not_available: {error_str[:200]}')
                return {'content': '', 'reasoning': '', 'usage': {}}
            # usage info usually arrives in the final chunk
            if 'usage' in chunk_json:
                usage = chunk_json['usage']
            choices = chunk_json.get('choices', [])
            if not choices:
                continue
            delta = choices[0].get('delta', {})
            reasoning_chunk = delta.get('reasoning_content', '') or delta.get('reasoning', '') or ''
            if reasoning_chunk:
                full_reasoning += reasoning_chunk
            content_chunk = delta.get('content', '')
            if content_chunk:
                full_content += content_chunk
        except json.JSONDecodeError:
            continue
    return {'content': full_content, 'reasoning': full_reasoning, 'usage': usage}


def call_llm(user_content: str) -> str:
    global _total_input_tokens, _total_output_tokens, _total_reasoning_tokens, _total_llm_time
    headers = {
        'Content-Type': 'application/json',
        'Authorization': LOCAL_API_TOKEN,
        'Wsid': LOCAL_API_WSID,
    }
    body = {
        'model': MODEL_NAME,
        'query_id': 'failure_modes_' + str(uuid.uuid4()),
        'messages': [
            {'role': 'system', 'content': SYS_PROMPT},
            {'role': 'user', 'content': user_content},
        ],
        'stream': True,
        'random_seed': 42,
        'openai_infer': True,
        'thinking': True,
        'chat_template_kwargs': {'thinking': True, 'enable_thinking': True},
        **GEN_PARAMS,
    }
    try:
        t0 = time.time()
        r = get_session().post(LOCAL_API_URL, headers=headers, json=body, stream=True, timeout=TIMEOUT)
        if r.status_code != 200:
            return ''
        result = _parse_stream_response(r)
        elapsed = time.time() - t0
        usage = result.get('usage', {})
        with _stats_lock:
            _total_llm_time += elapsed
            if usage:
                _total_input_tokens += usage.get('prompt_tokens', 0)
                _total_output_tokens += usage.get('completion_tokens', 0)
                _total_reasoning_tokens += usage.get('reasoning_tokens', 0) or usage.get('completion_tokens_details', {}).get('reasoning_tokens', 0)
        return result.get('content', '') or ''
    except Exception:
        return ''


def parse_json(text: str):
    if not text:
        return None
    s = text.strip()
    s = re.sub(r'^```(?:json)?\s*', '', s)
    s = re.sub(r'\s*```$', '', s)
    m = re.search(r'\{.*\}', s, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def process_one(rec, pdf_path):
    text = extract_pre_ref_text(pdf_path)
    if len(text) < 500:
        return {'paper_id': rec['paper_id'], 'title': rec.get('title', ''), 'error': 'pdf_text_too_short'}
    compact_annotation = {
        'collaboration_pattern': rec.get('collaboration_pattern'),
        'application_environment': rec.get('application_environment'),
        'feedback_structure': rec.get('feedback_structure'),
        'uncertainty_handling': rec.get('uncertainty_handling'),
        'model_coupling': rec.get('model_coupling'),
        'non_llm_models': rec.get('non_llm_models'),
        'pipeline_summary': rec.get('pipeline_summary'),
    }
    user = (
        f"Title: {rec.get('title', '')}\n"
        f"Venue: {rec.get('conference', '')} {rec.get('year', '')}\n"
        f"Existing taxonomy annotation:\n{json.dumps(compact_annotation, ensure_ascii=False, indent=2)}\n\n"
        f"Paper text before references:\n{text}"
    )
    for attempt in range(3):
        out = call_llm(user)
        parsed = parse_json(out)
        if parsed:
            parsed.update({
                'paper_id': rec['paper_id'],
                'title': rec.get('title', ''),
                'conference': rec.get('conference', ''),
                'year': rec.get('year', ''),
            })
            return parsed
        time.sleep(1 + random.random())
    return {'paper_id': rec['paper_id'], 'title': rec.get('title', ''), 'error': 'llm_failed'}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--limit', type=int, default=0, help='maximum number of pending papers to annotate')
    parser.add_argument('--workers', type=int, default=8)
    parser.add_argument('--domains', nargs='*', default=None, help='optional application_environment filter')
    parser.add_argument('--patterns', nargs='*', default=None, help='optional collaboration_pattern filter')
    args = parser.parse_args()

    records = load_taxonomy_records()
    if args.domains:
        records = [r for r in records if r.get('application_environment') in args.domains]
    if args.patterns:
        records = [r for r in records if r.get('collaboration_pattern') in args.patterns]

    done, failed = load_done()
    pdf_map = build_pdf_map(records)
    pending = [r for r in records if r['paper_id'] not in done and r['paper_id'] in pdf_map]
    if args.limit and args.limit > 0:
        pending = pending[:args.limit]

    print(f'[info] taxonomy records: {len(records)}')
    print(f'[info] pdf resolved: {len(pdf_map)}')
    print(f'[info] already done successfully: {len(done)}')
    print(f'[info] previous failed, will retry if selected: {len(failed)}')
    print(f'[info] pending this run: {len(pending)}')
    if not pending:
        return

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    run_start = time.time()
    completed_count = 0
    error_count = 0
    with OUTPUT_PATH.open('a', encoding='utf-8') as fout:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(process_one, r, pdf_map[r['paper_id']]): r for r in pending}
            pbar = tqdm(as_completed(futures), total=len(futures), desc='failure modes')
            for fut in pbar:
                rec = fut.result()
                with write_lock:
                    fout.write(json.dumps(rec, ensure_ascii=False) + '\n')
                    fout.flush()
                completed_count += 1
                if rec.get('error'):
                    error_count += 1
                # update progress bar with token throughput
                with _stats_lock:
                    elapsed_total = time.time() - run_start
                    out_toks = _total_output_tokens + _total_reasoning_tokens
                    tok_speed = out_toks / _total_llm_time if _total_llm_time > 0 else 0
                pbar.set_postfix({
                    'ok': completed_count - error_count,
                    'err': error_count,
                    'tok/s': f'{tok_speed:.1f}',
                    'out_tok': out_toks,
                }, refresh=True)
            pbar.close()

    total_elapsed = time.time() - run_start
    print(f'\n[done] {OUTPUT_PATH}')
    print(f'[stats] completed: {completed_count}, errors: {error_count}')
    print(f'[stats] total time: {total_elapsed:.1f}s')
    print(f'[stats] input tokens: {_total_input_tokens:,}, output tokens: {_total_output_tokens:,}, reasoning tokens: {_total_reasoning_tokens:,}')
    total_out = _total_output_tokens + _total_reasoning_tokens
    avg_speed = total_out / _total_llm_time if _total_llm_time > 0 else 0
    print(f'[stats] avg output speed: {avg_speed:.1f} tok/s (across {args.workers} workers)')


if __name__ == '__main__':
    main()
