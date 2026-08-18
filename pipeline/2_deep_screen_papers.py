#!/usr/bin/env python3
"""
Deep Paper Screening Pipeline — PDF-based fine-grained screening.

Fine-screens ALL downloaded papers using the PDF full text (truncated
before References): extracts the pre-references text with PyMuPDF and asks
an LLM for a precise relevance judgment.

Input:  papers_data/metadata/*.json + papers_data/pdfs/
Output: papers_data/deep_screening/deep_screen_<venue>.<year>.jsonl
        (YES/NO/MAYBE + reasoning + dimension tags)

Supports multi-process + multi-thread execution, checkpoint/resume, and
graceful Ctrl+C shutdown with progress saving.
"""

import json
import os
import re
import sys
import time
import uuid
import html
import argparse
import random
import threading
import queue
import itertools
import multiprocessing as mp
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED

import fitz  # PyMuPDF
import requests
from tqdm import tqdm

BASE_DIR = Path(__file__).parent
METADATA_DIR = BASE_DIR / "papers_data" / "metadata"
PDF_DIR = BASE_DIR / "papers_data" / "pdfs"
OUTPUT_DIR = BASE_DIR / "papers_data" / "deep_screening"

# Screening models: (model_name, max_concurrent_workers).
# Use models with long context (64K+) and thinking support.
SCREEN_MODELS = [
    ('your-model-name', 50),
]

WORKERS_PER_PROCESS = 50

# API config — replace with your own OpenAI-compatible API endpoint
LOCAL_API_URL = 'http://YOUR_API_HOST:PORT/v1/chat/completions'
LOCAL_API_TOKEN = 'Bearer YOUR_API_KEY'
LOCAL_API_WSID = ''  # optional

GEN_PARAMS = {
    'temperature': 0.1,
    'top_p': 0.9,
    'top_k': 20,
    'repetition_penalty': 1.0,
    'output_seq_len': 1024,          # deep screening needs longer output (reasoning + tags)
    'max_input_seq_len': 65536,      # PDF full text can be long
}

USE_THINKING = True

MAX_RETRIES = 3
RETRY_DELAY = 1

# PDF parsing parameters
MAX_PRE_REF_PAGES = 13       # References usually start on pages 6-13; take at most the first 13 pages
MAX_TEXT_CHARS = 80000       # cap on extracted body text length


DEEP_SCREEN_SYSTEM_PROMPT = """You are an expert AI researcher performing a FINE-GRAINED screening for a survey on "Multi-Model Agents" — systems that explicitly combine multiple heterogeneous models to build intelligent agents or complex AI pipelines.

You will receive the FULL TEXT of a paper (before references). Based on the paper's actual content, methods, and contributions, determine its relevance.

## Core Definition: Multi-Model Agent
A system that:
- Uses an LLM (or foundation model) as the "brain" / orchestrator / planner
- Explicitly invokes, coordinates, or composes OTHER heterogeneous neural models (vision, audio, 3D, grounding, segmentation, detection, generation, etc.) as tools or components
- These models work together to accomplish tasks that no single model can do alone

## Relevance Categories (answer ONE):

### YES — Directly relevant, must include in survey
- Systems combining LLM + other specialized neural models (e.g., LLM + SAM, LLM + CLIP, LLM + Stable Diffusion, LLM + ASR/TTS)
- Agent frameworks that orchestrate multiple heterogeneous models
- LLM-based planners that invoke vision/audio/3D models as tools
- Cross-modal agent pipelines (e.g., see-think-act with separate perception + reasoning models)
- Embodied agents using LLM + perception + control models
- GUI/Web agents combining LLM with grounding/detection models
- Benchmarks specifically for multi-model agent evaluation
- Research on error propagation, interface design, or model composition in multi-model systems

### MAYBE — Partially relevant, worth a closer look
- Papers that use multiple models but the agent/orchestration aspect is weak
- Papers with a modular architecture that COULD be seen as multi-model but primarily focus on training/fine-tuning
- Papers on model routing/selection that could apply to heterogeneous model pools
- Papers on tool-augmented LLMs where some tools are neural models but this isn't the main focus

### NO — Not relevant
- Single end-to-end multimodal models (e.g., a unified vision-language model trained jointly) — even if they process multiple modalities
- Multi-agent systems where ALL agents are LLMs (no heterogeneous model diversity)
- Pure NLP/CV/speech papers with no multi-model composition
- LLM + non-model tools only (RAG, search, calculator, code interpreter) without neural model components
- Pure model merging/ensemble of same-type models
- Pure training/fine-tuning methods for a single model
- Papers that merely use a pretrained model (like CLIP) as a frozen feature extractor without any orchestration or agent behavior

## Output format (strict):
```
REASONING: <2-4 sentences explaining your judgment based on the paper's actual methods and contributions>
LABEL: <YES/MAYBE/NO>
DIMENSIONS: <comma-separated list of relevant dimensions from: orchestration, perception, generation, grounding, embodied, gui_agent, benchmark, interface_design, error_propagation, cross_modal, tool_use, planning, memory, other>
KEY_MODELS: <comma-separated list of specific models/model types used, e.g.: GPT-4, SAM, CLIP, Stable Diffusion, Whisper, GroundingDINO>
```

Be STRICT in this pass. This is a fine-grained screening — we want precision. When in doubt between YES and MAYBE, choose MAYBE. When in doubt between MAYBE and NO, choose NO."""


_session = None
_shutdown = threading.Event()


def _configure_session_pool(total_workers):
    global _session
    if _session is None:
        _session = requests.Session()
    pool_size = max(100, int(total_workers * 1.2) + 50)
    adapter = requests.adapters.HTTPAdapter(
        pool_connections=pool_size, pool_maxsize=pool_size, max_retries=3
    )
    _session.mount('http://', adapter)
    _session.mount('https://', adapter)


class TokenTracker:
    def __init__(self, window=10.0, shared_counter=None):
        self._lock = threading.Lock()
        self._total_tokens = 0
        self._start_time = None
        self._window = window
        self._recent_records = []
        self._shared_counter = shared_counter

    def add(self, n=1):
        now = time.time()
        with self._lock:
            if self._start_time is None:
                self._start_time = now
            self._total_tokens += n
            self._recent_records.append((now, n))
            cutoff = now - self._window
            while self._recent_records and self._recent_records[0][0] < cutoff:
                self._recent_records.pop(0)
        if self._shared_counter is not None:
            with self._shared_counter.get_lock():
                self._shared_counter.value += n

    def stats(self):
        now = time.time()
        with self._lock:
            total = self._total_tokens
            if self._start_time is None or now <= self._start_time:
                return total, 0.0, 0.0
            avg_tps = total / (now - self._start_time)
            if not self._recent_records:
                return total, avg_tps, 0.0
            window_tokens = sum(c for _, c in self._recent_records)
            oldest = self._recent_records[0][0]
            window_dur = now - oldest
            recent_tps = window_tokens / window_dur if window_dur > 0 else 0.0
            return total, avg_tps, recent_tps

token_tracker = TokenTracker(window=10.0)


def _parse_stream_response(resp):
    full_content = ''
    full_reasoning = ''
    for line in resp.iter_lines():
        if _shutdown.is_set():
            resp.close()
            return None, None
        if not line:
            continue
        decoded = line.decode('utf-8')
        if decoded.startswith('data:'):
            data_str = decoded[5:].strip()
        elif decoded.startswith('data :'):
            data_str = decoded[6:].strip()
        else:
            continue
        if data_str == '[DONE]':
            break
        try:
            chunk = json.loads(data_str)
            if 'error' in chunk:
                return None, None
            choices = chunk.get('choices', [])
            if not choices:
                continue
            delta = choices[0].get('delta', {})
            reasoning_chunk = delta.get('reasoning_content', '') or delta.get('reasoning', '') or ''
            if reasoning_chunk:
                full_reasoning += reasoning_chunk
                token_tracker.add(1)
            content_chunk = delta.get('content', '')
            if content_chunk:
                full_content += content_chunk
                token_tracker.add(1)
        except json.JSONDecodeError:
            continue
    return full_content, full_reasoning


def _extract_thinking(content):
    if '</think>' in content:
        parts = content.split('</think>', 1)
        thinking = parts[0]
        if '<think>' in thinking:
            thinking = thinking.split('<think>', 1)[-1]
        return parts[1].strip(), thinking.strip()
    return content, None


def call_llm(sys_prompt, user_content, model_name, timeout=180):
    """Call the LLM API. Deep screening uses a longer timeout (full-text input)."""
    messages = [
        {'role': 'system', 'content': sys_prompt},
        {'role': 'user', 'content': user_content},
    ]
    headers = {
        'Content-Type': 'application/json',
        'Authorization': LOCAL_API_TOKEN,
        'Wsid': LOCAL_API_WSID,
    }
    use_stream = USE_THINKING
    json_data = {
        'model': model_name,
        'query_id': 'deep_screen_' + str(uuid.uuid4()),
        'messages': messages,
        'stream': use_stream,
        'random_seed': 42,
        'openai_infer': True,
        'thinking': USE_THINKING,
        'chat_template_kwargs': {'thinking': USE_THINKING, 'enable_thinking': USE_THINKING},
        **GEN_PARAMS,
    }
    try:
        req_timeout = (30, 150) if use_stream else timeout
        resp = _session.post(LOCAL_API_URL, headers=headers, json=json_data,
                             stream=use_stream, timeout=req_timeout)
        if use_stream:
            if resp.status_code != 200:
                return None, None
            content, reasoning = _parse_stream_response(resp)
        else:
            resp_json = resp.json()
            if 'error' in resp_json:
                return None, None
            content = resp_json['choices'][0]['message']['content']
            reasoning = None

        if not content:
            return None, None

        thinking = None
        if USE_THINKING:
            if reasoning and reasoning.strip():
                thinking = reasoning.strip()
                content, _ = _extract_thinking(content)
            else:
                content, thinking = _extract_thinking(content)

        return content, thinking
    except Exception:
        return None, None


# Reference heading patterns (strict to loose)
_REF_PATTERNS = [
    # Standalone "References" / "REFERENCES" line
    re.compile(r'(?:^|\n)\s*(References|REFERENCES|Bibliography|BIBLIOGRAPHY)\s*(?:\n|$)', re.MULTILINE),
    # Numbered heading like "8. References"
    re.compile(r'(?:^|\n)\s*\d+[\.\s]+(References|REFERENCES)\s*(?:\n|$)', re.MULTILINE),
]


def extract_pre_reference_text(pdf_path: str) -> tuple:
    """
    Extract the body text before the References section from a PDF.

    Returns: (text, ref_page, total_pages, error)
    - text: body text (the part before References)
    - ref_page: 1-based page where References appears, None if not found
    - total_pages: total PDF page count
    - error: error message, None on success
    """
    try:
        doc = fitz.open(pdf_path)
    except Exception as e:
        return '', None, 0, f'fitz_open_error: {e}'

    total_pages = doc.page_count

    ref_page_idx = None  # 0-based
    ref_char_offset = None

    for pn in range(total_pages):
        text = doc[pn].get_text()
        for pat in _REF_PATTERNS:
            m = pat.search(text)
            if m:
                ref_page_idx = pn
                ref_char_offset = m.start()
                break
        if ref_page_idx is not None:
            break

    pre_ref_text = ''

    if ref_page_idx is not None:
        # References found: all pages before it + that page truncated at the heading
        for pn in range(ref_page_idx + 1):
            page_text = doc[pn].get_text()
            if pn == ref_page_idx:
                pre_ref_text += page_text[:ref_char_offset]
            else:
                pre_ref_text += page_text
    else:
        # No References heading: take the first MAX_PRE_REF_PAGES pages
        for pn in range(min(MAX_PRE_REF_PAGES, total_pages)):
            pre_ref_text += doc[pn].get_text()

    doc.close()

    if len(pre_ref_text) > MAX_TEXT_CHARS:
        pre_ref_text = pre_ref_text[:MAX_TEXT_CHARS] + '\n\n[... text truncated ...]'

    ref_page = (ref_page_idx + 1) if ref_page_idx is not None else None
    return pre_ref_text, ref_page, total_pages, None


def load_papers_with_pdfs() -> list:
    """Load every paper with a downloaded PDF from the metadata files."""
    papers = []
    for meta_file in sorted(METADATA_DIR.glob('*.json')):
        stem = meta_file.stem
        parts = stem.rsplit('.', 1)
        if len(parts) != 2:
            continue
        venue, year_str = parts
        data = json.loads(meta_file.read_text())
        for p in data:
            p['conference'] = venue
            p['year'] = int(year_str)

            safe_title = re.sub(r'[^\w\s-]', '', html.unescape(p.get('title', '')))[:80].strip()
            safe_title = re.sub(r'\s+', '_', safe_title)
            pdf_path = PDF_DIR / venue / year_str / f"{p['paper_id']}_{safe_title}.pdf"
            if pdf_path.exists():
                p['pdf_path'] = str(pdf_path)
                papers.append(p)

    return papers


def load_completed(output_dir: Path) -> set:
    """Load already-processed paper_ids for checkpoint/resume."""
    completed = set()
    for f in output_dir.glob('deep_screen_*.jsonl'):
        with open(f, 'r') as fh:
            for line in fh:
                if line.strip():
                    rec = json.loads(line)
                    completed.add(rec['paper_id'])
    return completed


def build_deep_screen_input(paper: dict, pdf_text: str) -> str:
    """Build the deep-screening user prompt: title + venue + PDF body text."""
    title = html.unescape(paper.get('title', ''))
    conference = paper.get('conference', '')
    year = paper.get('year', '')
    abstract = paper.get('abstract', '')

    parts = [
        f"**Title**: {title}",
        f"**Venue**: {conference} {year}",
    ]
    if abstract:
        parts.append(f"**Abstract**: {abstract}")
    parts.append(f"\n**Full Paper Text (before references)**:\n{pdf_text}")
    parts.append("\nBased on the full paper content above, is this paper relevant to a survey on Multi-Model Agents?")

    return '\n'.join(parts)


def parse_deep_screen_output(output: str) -> dict:
    """
    Parse the deep-screening LLM output.
    Expected format:
        REASONING: ...
        LABEL: YES/MAYBE/NO
        DIMENSIONS: ...
        KEY_MODELS: ...
    """
    if not output:
        return {'label': 'ERROR', 'reasoning': 'empty output', 'dimensions': [], 'key_models': []}

    output = output.strip()
    result = {
        'label': 'ERROR',
        'reasoning': '',
        'dimensions': [],
        'key_models': [],
    }

    m = re.search(r'REASONING:\s*(.+?)(?=\nLABEL:|\nDIMENSIONS:|\nKEY_MODELS:|$)', output, re.DOTALL)
    if m:
        result['reasoning'] = m.group(1).strip()

    m = re.search(r'LABEL:\s*(YES|MAYBE|NO)', output, re.IGNORECASE)
    if m:
        result['label'] = m.group(1).upper()
    else:
        # Fallback: look for a standalone YES/MAYBE/NO in the output
        upper = output.upper()
        for label in ['YES', 'MAYBE', 'NO']:
            if label in upper:
                result['label'] = label
                break

    m = re.search(r'DIMENSIONS:\s*(.+?)(?=\nKEY_MODELS:|$)', output, re.DOTALL)
    if m:
        dims = [d.strip() for d in m.group(1).strip().split(',') if d.strip()]
        result['dimensions'] = dims

    m = re.search(r'KEY_MODELS:\s*(.+?)$', output, re.DOTALL)
    if m:
        models = [mod.strip() for mod in m.group(1).strip().split(',') if mod.strip()]
        result['key_models'] = models

    return result


def _run_worker_process(group_models, tasks, tmp_file, sys_prompt,
                        shared_token_counter, shared_done_counter,
                        shared_yes_counter, shared_maybe_counter,
                        shared_no_counter, shared_err_counter,
                        shared_parse_fail_counter,
                        shutdown_event):
    """Subprocess entry point: process the assigned task list."""
    import queue as _queue

    global token_tracker, _shutdown, _session

    _shutdown = shutdown_event
    _session = requests.Session()

    total_workers = sum(w for _, w in group_models)
    token_tracker = TokenTracker(window=10.0, shared_counter=shared_token_counter)
    _configure_session_pool(total_workers)

    model_queue = _queue.Queue()
    for name, w in group_models:
        for _ in range(w):
            model_queue.put(name)

    model_info = ', '.join(f'{name}({w})' for name, w in group_models)
    print(f'[PID {os.getpid()}] Subprocess started: {model_info} → workers={total_workers}, tasks={len(tasks)}')

    write_buffer = _queue.Queue()
    local_success = 0
    local_fail = 0
    count_lock = threading.Lock()

    def _process_one(paper):
        nonlocal local_success, local_fail
        if _shutdown.is_set():
            return

        paper_id = paper['paper_id']
        pdf_path = paper.get('pdf_path', '')

        # Step 1: extract PDF body text
        pdf_text, ref_page, total_pages, parse_err = extract_pre_reference_text(pdf_path)

        if parse_err or not pdf_text.strip():
            with shared_parse_fail_counter.get_lock():
                shared_parse_fail_counter.value += 1
            with shared_done_counter.get_lock():
                shared_done_counter.value += 1

            output_record = {
                'paper_id': paper_id,
                'title': html.unescape(paper.get('title', '')),
                'conference': paper.get('conference', ''),
                'year': paper.get('year', 0),
                'label': 'PARSE_ERROR',
                'reasoning': parse_err or 'empty_text',
                'dimensions': [],
                'key_models': [],
                'ref_page': ref_page,
                'total_pages': total_pages,
                'text_chars': len(pdf_text),
            }
            line = json.dumps(output_record, ensure_ascii=False) + '\n'
            write_buffer.put(line)
            return

        # Step 2: build prompt and call the LLM
        user_content = build_deep_screen_input(paper, pdf_text)

        result_text = None
        thinking = None
        model_used = None

        for attempt in range(MAX_RETRIES):
            if _shutdown.is_set():
                return
            model_name = model_queue.get()
            model_used = model_name
            try:
                result_text, thinking = call_llm(sys_prompt, user_content, model_name)
            finally:
                model_queue.put(model_name)

            if result_text is not None:
                break
            if attempt < MAX_RETRIES - 1:
                backoff = RETRY_DELAY * (2 ** attempt) + random.uniform(0, RETRY_DELAY)
                if _shutdown.wait(backoff):
                    return

        if result_text is None:
            with count_lock:
                local_fail += 1
            with shared_done_counter.get_lock():
                shared_done_counter.value += 1
            with shared_err_counter.get_lock():
                shared_err_counter.value += 1
            return

        parsed = parse_deep_screen_output(result_text)

        output_record = {
            'paper_id': paper_id,
            'title': html.unescape(paper.get('title', '')),
            'conference': paper.get('conference', ''),
            'year': paper.get('year', 0),
            'label': parsed['label'],
            'reasoning': parsed['reasoning'],
            'dimensions': parsed['dimensions'],
            'key_models': parsed['key_models'],
            'model': model_used,
            'ref_page': ref_page,
            'total_pages': total_pages,
            'text_chars': len(pdf_text),
        }

        line = json.dumps(output_record, ensure_ascii=False) + '\n'
        write_buffer.put(line)

        with count_lock:
            local_success += 1
        with shared_done_counter.get_lock():
            shared_done_counter.value += 1

        label = parsed['label']
        if label == 'YES':
            with shared_yes_counter.get_lock():
                shared_yes_counter.value += 1
        elif label == 'MAYBE':
            with shared_maybe_counter.get_lock():
                shared_maybe_counter.value += 1
        elif label == 'NO':
            with shared_no_counter.get_lock():
                shared_no_counter.value += 1
        else:
            with shared_err_counter.get_lock():
                shared_err_counter.value += 1

    # Writer thread
    FLUSH_INTERVAL = 2
    write_done = threading.Event()

    def _writer_thread():
        with open(tmp_file, 'a', encoding='utf-8') as f_out:
            while not write_done.is_set() or not write_buffer.empty():
                lines = []
                try:
                    lines.append(write_buffer.get(timeout=FLUSH_INTERVAL))
                except _queue.Empty:
                    continue
                while not write_buffer.empty():
                    try:
                        lines.append(write_buffer.get_nowait())
                    except _queue.Empty:
                        break
                if lines:
                    f_out.writelines(lines)
                    f_out.flush()

    writer = threading.Thread(target=_writer_thread, daemon=True)
    writer.start()

    # Sliding window submission
    WINDOW_SIZE = total_workers * 3
    task_iter = iter(tasks)

    executor = ThreadPoolExecutor(max_workers=total_workers)
    try:
        pending = set()
        for task in itertools.islice(task_iter, WINDOW_SIZE):
            pending.add(executor.submit(_process_one, task))

        while pending:
            if _shutdown.is_set():
                break
            done, pending = wait(pending, return_when=FIRST_COMPLETED)
            for future in done:
                try:
                    future.result()
                except Exception as e:
                    print(f'[PID {os.getpid()}] Unexpected error: {e}')
            for task in itertools.islice(task_iter, len(done)):
                pending.add(executor.submit(_process_one, task))
    except KeyboardInterrupt:
        _shutdown.set()
        for future in pending:
            future.cancel()
        executor.shutdown(wait=False, cancel_futures=True)
        write_done.set()
        writer.join(timeout=5)
        print(f'[PID {os.getpid()}] Interrupted. success: {local_success}, fail: {local_fail}')
        return

    executor.shutdown(wait=True)
    write_done.set()
    writer.join()

    print(f'[PID {os.getpid()}] Done. success: {local_success}, fail: {local_fail}')


def _merge_tmp_files_to_venue(tmp_files, output_dir):
    """Dispatch subprocess temp files into per venue-year deep_screen_XXX.YYYY.jsonl files."""
    merged = 0
    for tmp_file in tmp_files:
        if not os.path.exists(tmp_file):
            continue
        venue_lines = {}
        with open(tmp_file, 'r', encoding='utf-8') as f_in:
            for line in f_in:
                if not line.strip():
                    continue
                rec = json.loads(line)
                venue_key = f"{rec['conference']}.{rec['year']}"
                if venue_key not in venue_lines:
                    venue_lines[venue_key] = []
                venue_lines[venue_key].append(line)
                merged += 1
        for venue_key, lines in venue_lines.items():
            fpath = output_dir / f'deep_screen_{venue_key}.jsonl'
            with open(fpath, 'a', encoding='utf-8') as f_out:
                f_out.writelines(lines)
        os.remove(tmp_file)
    if merged > 0:
        print(f'Merged {merged} records into venue-year files')
    return merged


def run_deep_screening(
    papers: list,
    output_dir: Path,
    models: list,
    force: bool = False,
):
    """Multi-process deep screening pipeline."""
    total_workers = sum(w for _, w in models)
    model_info = ', '.join(f'{name}({w})' for name, w in models)

    # Checkpoint
    completed = set()
    if not force:
        completed = load_completed(output_dir)

    pending = [p for p in papers if p['paper_id'] not in completed]

    if not pending:
        print('All papers have already been deep-screened!')
        return

    # Split into subprocesses by WORKERS_PER_PROCESS
    process_groups = []
    for model_name, total_w in models:
        remaining = total_w
        while remaining > 0:
            chunk = min(remaining, WORKERS_PER_PROCESS)
            process_groups.append([(model_name, chunk)])
            remaining -= chunk

    print(f'\n{"=" * 60}')
    print(f'Deep Screening (PDF-based)')
    print(f'Models: {model_info} → Total workers: {total_workers}')
    print(f'Thinking: {USE_THINKING}')
    print(f'Workers per process: {WORKERS_PER_PROCESS} → {len(process_groups)} processes')
    print(f'Total papers with PDF: {len(papers)}')
    print(f'Completed: {len(completed)}, Pending: {len(pending)}')
    if force:
        print(f'  ⚠️  Force mode: ignoring existing results, re-screening all')
    print(f'Output dir: {output_dir}')
    print(f'{"=" * 60}\n')

    if force:
        for f in output_dir.glob('deep_screen_*.jsonl'):
            f.unlink()
            print(f'  Deleted: {f.name}')

    # Distribute tasks proportionally to worker counts
    group_worker_counts = [sum(w for _, w in grp) for grp in process_groups]
    total_group_workers = sum(group_worker_counts)

    random.shuffle(pending)
    group_tasks = []
    start = 0
    for gi, gwc in enumerate(group_worker_counts):
        if gi == len(group_worker_counts) - 1:
            group_tasks.append(pending[start:])
        else:
            count = int(len(pending) * gwc / total_group_workers)
            group_tasks.append(pending[start:start + count])
            start += count

    tmp_files = [str(output_dir / f'.tmp_deep_group{gi}') for gi in range(len(process_groups))]

    ctx = mp.get_context('spawn')

    shared_token_counter = ctx.Value('q', 0)
    shared_done_counter = ctx.Value('q', 0)
    shared_yes_counter = ctx.Value('q', 0)
    shared_maybe_counter = ctx.Value('q', 0)
    shared_no_counter = ctx.Value('q', 0)
    shared_err_counter = ctx.Value('q', 0)
    shared_parse_fail_counter = ctx.Value('q', 0)
    shutdown_event = ctx.Event()

    processes = []
    for gi, (grp_models, grp_tasks) in enumerate(zip(process_groups, group_tasks)):
        if not grp_tasks:
            continue
        p = ctx.Process(
            target=_run_worker_process,
            args=(grp_models, grp_tasks, tmp_files[gi], DEEP_SCREEN_SYSTEM_PROMPT,
                  shared_token_counter, shared_done_counter,
                  shared_yes_counter, shared_maybe_counter,
                  shared_no_counter, shared_err_counter,
                  shared_parse_fail_counter,
                  shutdown_event),
            name=f'DeepScreenProc{gi}',
        )
        p.start()
        processes.append(p)

    # Main process: progress bar
    total_tasks = len(pending)
    pbar = tqdm(total=total_tasks, desc=f'Deep Screening ({total_workers}w, {len(processes)}p)')

    start_time = time.time()
    token_history = []
    WINDOW = 10.0

    try:
        while any(p.is_alive() for p in processes):
            time.sleep(1.0)
            current_done = shared_done_counter.value
            pbar.n = min(current_done, total_tasks)

            now = time.time()
            current_tokens = shared_token_counter.value
            token_history.append((now, current_tokens))
            cutoff = now - WINDOW
            while token_history and token_history[0][0] < cutoff:
                token_history.pop(0)

            elapsed = now - start_time
            avg_tps = current_tokens / elapsed if elapsed > 0 else 0

            if len(token_history) >= 2:
                dt = token_history[-1][0] - token_history[0][0]
                dtok = token_history[-1][1] - token_history[0][1]
                cur_tps = dtok / dt if dt > 0 else 0
            else:
                cur_tps = 0

            y = shared_yes_counter.value
            m = shared_maybe_counter.value
            n = shared_no_counter.value
            e = shared_err_counter.value
            pf = shared_parse_fail_counter.value

            pbar.set_postfix_str(
                f'Y={y} M={m} N={n} ERR={e} PF={pf} | tok={current_tokens:,} avg={avg_tps:,.0f}t/s cur={cur_tps:,.0f}t/s',
                refresh=True,
            )
            pbar.refresh()
    except KeyboardInterrupt:
        print('\n\n⚡ Ctrl+C detected, shutting down all processes...')
        shutdown_event.set()
        for p in processes:
            p.join(timeout=10)
            if p.is_alive():
                p.terminate()
        pbar.close()
        _merge_tmp_files_to_venue(tmp_files, output_dir)
        print(f'Progress saved to: {output_dir}')
        return

    for p in processes:
        p.join()

    pbar.n = total_tasks
    pbar.refresh()
    pbar.close()

    _merge_tmp_files_to_venue(tmp_files, output_dir)

    final_done = shared_done_counter.value
    final_tokens = shared_token_counter.value
    final_yes = shared_yes_counter.value
    final_maybe = shared_maybe_counter.value
    final_no = shared_no_counter.value
    final_err = shared_err_counter.value
    final_pf = shared_parse_fail_counter.value
    elapsed = time.time() - start_time

    print(f'\n{"=" * 60}')
    print(f'Deep Screening complete!')
    print(f'  YES   (directly relevant):   {final_yes}')
    print(f'  MAYBE (partially relevant):  {final_maybe}')
    print(f'  NO    (not relevant):         {final_no}')
    print(f'  ERR   (LLM failed):           {final_err}')
    print(f'  PARSE_FAIL (PDF parse fail):  {final_pf}')
    print(f'  Total:                        {final_done}')
    print(f'  Tokens: {final_tokens:,} | Elapsed: {elapsed:.1f}s | Avg: {final_tokens/elapsed:,.0f} t/s')
    print(f'Output dir: {output_dir}')
    print(f'{"=" * 60}')


def print_summary(output_dir: Path):
    """Print deep-screening result statistics."""
    if not output_dir.exists():
        print("No deep screening results found.")
        return

    total_y = total_m = total_n = total_e = total_pf = 0

    print(f'\n{"=" * 80}')
    print(f'{"Conference":<15} {"Year":<6} {"YES":<8} {"MAYBE":<8} {"NO":<8} {"ERR":<8} {"PF":<8} {"Total":<8}')
    print(f'{"-" * 80}')

    for f in sorted(output_dir.glob('deep_screen_*.jsonl')):
        key = f.stem.replace('deep_screen_', '')
        y = m = n = e = pf = 0
        with open(f, 'r') as fh:
            for line in fh:
                if line.strip():
                    rec = json.loads(line)
                    lbl = rec.get('label', '')
                    if lbl == 'YES': y += 1
                    elif lbl == 'MAYBE': m += 1
                    elif lbl == 'NO': n += 1
                    elif lbl == 'PARSE_ERROR': pf += 1
                    else: e += 1
        total_y += y; total_m += m; total_n += n; total_e += e; total_pf += pf
        parts = key.rsplit('.', 1)
        conf = parts[0] if parts else key
        yr = parts[1] if len(parts) > 1 else ''
        total = y + m + n + e + pf
        print(f'{conf:<15} {yr:<6} {y:<8} {m:<8} {n:<8} {e:<8} {pf:<8} {total:<8}')

    print(f'{"-" * 80}')
    grand = total_y + total_m + total_n + total_e + total_pf
    print(f'{"TOTAL":<15} {"":<6} {total_y:<8} {total_m:<8} {total_n:<8} {total_e:<8} {total_pf:<8} {grand:<8}')
    if grand > 0:
        pct_y = total_y / grand * 100
        pct_m = total_m / grand * 100
        print(f'\nYES rate: {pct_y:.1f}% ({total_y}/{grand})')
        print(f'MAYBE rate: {pct_m:.1f}% ({total_m}/{grand})')
        print(f'YES + MAYBE: {pct_y + pct_m:.1f}% ({total_y + total_m}/{grand})')
    print(f'{"=" * 80}')


def main():
    parser = argparse.ArgumentParser(
        description='PDF-based deep screening for Multi-Model Agent Survey'
    )
    parser.add_argument(
        '--phase', choices=['screen', 'summary'], default='screen',
        help='screen: run deep screening | summary: view result stats',
    )
    parser.add_argument(
        '--conferences', '-c', nargs='+', default=None,
        help='Only process these conferences (e.g. NeurIPS ICLR CVPR)',
    )
    parser.add_argument(
        '--years', '-y', nargs='+', type=int, default=None,
        help='Only process these years (e.g. 2023 2024 2025)',
    )
    parser.add_argument(
        '--model', nargs='+', default=None,
        help='Model name(s)',
    )
    parser.add_argument(
        '--workers', nargs='+', type=int, default=None,
        help='Workers per model',
    )
    parser.add_argument(
        '--no-thinking', action='store_true',
        help='Disable thinking mode',
    )
    parser.add_argument(
        '--force', action='store_true',
        help='Force re-screening',
    )

    args = parser.parse_args()

    global USE_THINKING, SCREEN_MODELS

    if args.no_thinking:
        USE_THINKING = False

    if args.model is not None:
        workers_list = args.workers if args.workers else []
        default_w = SCREEN_MODELS[0][1] if SCREEN_MODELS else 40
        SCREEN_MODELS = []
        for i, m in enumerate(args.model):
            w = workers_list[i] if i < len(workers_list) else default_w
            SCREEN_MODELS.append((m, w))
    elif args.workers is not None:
        default_w = args.workers[0] if args.workers else 40
        SCREEN_MODELS = [(m, args.workers[i] if i < len(args.workers) else default_w)
                         for i, (m, _) in enumerate(SCREEN_MODELS)]

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    if args.phase == 'summary':
        print_summary(OUTPUT_DIR)
        return

    # Phase: screen
    print('Loading papers with downloaded PDFs...')
    all_papers = load_papers_with_pdfs()

    if not all_papers:
        print('No papers with PDFs found. Run 1_crawl_papers.py first.')
        return

    if args.conferences:
        all_papers = [p for p in all_papers if p['conference'] in args.conferences]
    if args.years:
        all_papers = [p for p in all_papers if p['year'] in args.years]

    from collections import defaultdict
    grouped = defaultdict(list)
    for p in all_papers:
        key = f"{p['conference']}.{p['year']}"
        grouped[key].append(p)

    print(f'Found {len(all_papers)} papers with PDFs across {len(grouped)} venue-years:')
    for key in sorted(grouped.keys()):
        print(f'  [{key}] {len(grouped[key])} papers')

    run_deep_screening(all_papers, OUTPUT_DIR, SCREEN_MODELS, force=args.force)

    print_summary(OUTPUT_DIR)


if __name__ == '__main__':
    main()
