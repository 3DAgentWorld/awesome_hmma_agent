#!/usr/bin/env python3
"""
Structured Annotation Pipeline — outline-aligned paper labeling.

For the YES+MAYBE papers from deep screening, runs an LLM pass to extract
structured labels aligned with the survey outline: application domain,
topology, interface type, training paradigm, model roles, error correction
mechanism, and a brief pipeline description.

Input:  papers_data/deep_screening/deep_screen_*.jsonl + papers_data/pdfs/
Output: papers_data/annotations/annotate_<venue>.<year>.jsonl

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
DEEP_SCREENING_DIR = BASE_DIR / "papers_data" / "deep_screening"
PDF_DIR = BASE_DIR / "papers_data" / "pdfs"
METADATA_DIR = BASE_DIR / "papers_data" / "metadata"
OUTPUT_DIR = BASE_DIR / "papers_data" / "annotations"

# Annotation models: (model_name, max_concurrent_workers).
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
    'output_seq_len': 2048,          # Annotation output is more structured, needs space
    'max_input_seq_len': 65536,
}

USE_THINKING = True

MAX_RETRIES = 3
RETRY_DELAY = 1

# PDF parsing params (reused from deep_screen)
MAX_PRE_REF_PAGES = 13
MAX_TEXT_CHARS = 80000


ANNOTATION_SYSTEM_PROMPT = """You are an expert AI researcher performing STRUCTURED ANNOTATION for a survey on "Heterogeneous Multi-Model Agents (HMMA)".

You will receive the full text of a paper (before references) that has already been screened as relevant (YES or MAYBE). Your task is to extract structured metadata aligned with the survey's outline.

## Survey Outline Summary
The survey covers systems that combine an LLM/foundation model as "brain" with other heterogeneous neural models (vision, audio, 3D, grounding, segmentation, detection, generation, etc.) as "eyes, ears, hands". Key chapters:
- Ch3: Architecture topology (Pipeline / Star / Closed-loop) and inter-model interface design (Symbolic / Continuous)
- Ch4: Error propagation and efficiency in multi-model systems
- Ch5: Application domains (Embodied, GUI, Gaming, Scientific, Generation, etc.)
- Ch6: Training paradigms (Zero-shot composition / Modular fine-tuning / Joint optimization)

## Output Format (strict JSON):
```json
{
  "application_domain": "<primary domain, ONE of: Embodied, GUI_Agent, Gaming, Scientific, Generation, Visual_QA, Audio_Speech, Retrieval, Medical, Autonomous_Driving, Robotics_Manipulation, Video_Understanding, Document_Understanding, Code_Agent, Multimodal_Reasoning, Benchmark, Other>",
  "application_domain_secondary": "<optional second domain if paper spans two, or null>",
  "topology": "<ONE of: Pipeline, Star, ClosedLoop, Hybrid, Unclear>",
  "topology_details": "<1 sentence: how models are connected, e.g. 'LLM calls CLIP and SAM in parallel, then aggregates results'>",
  "interface_type": "<ONE of: Symbolic, Continuous, Mixed>",
  "interface_details": "<1 sentence: what is passed between models, e.g. 'bounding box coordinates and natural language descriptions'>",
  "training_paradigm": "<ONE of: ZeroShot, ModularFinetune, JointOptimization, Mixed>",
  "training_details": "<1 sentence: how the system is trained/assembled, e.g. 'All models are frozen, composed via prompting'>",
  "has_error_correction": <true or false>,
  "error_correction_details": "<1 sentence if true, e.g. 'Uses confidence thresholding and LLM self-verification loop', or null>",
  "model_roles": {
    "orchestrator": ["<model names, e.g. GPT-4, LLaMA-2>"],
    "perceiver": ["<e.g. CLIP, GroundingDINO, SAM, Whisper>"],
    "generator": ["<e.g. Stable Diffusion, DALL-E>"],
    "executor": ["<e.g. VPT, RT-2, low-level RL policy>"],
    "other": ["<any other models>"]
  },
  "brief_pipeline": "<1-2 sentences describing the complete model composition flow, e.g. 'User query → GPT-4 (plan decomposition) → GroundingDINO (object localization) → SAM (segmentation) → GPT-4 (answer generation)'>",
  "survey_chapters": ["<list of most relevant chapter numbers from: Ch2_Scope, Ch3_Architecture, Ch4_ErrorEfficiency, Ch5_Embodied, Ch5_GUI, Ch5_Gaming, Ch5_Scientific, Ch5_Generation, Ch5_AudioSpeech, Ch5_Medical, Ch5_Other, Ch6_Training, Ch7_Discussion>"]
}
```

## Annotation Guidelines:

### application_domain:
- Choose the PRIMARY domain based on what task/environment the system targets
- Embodied = physical world navigation, manipulation (Habitat, AI2-THOR, real robots)
- GUI_Agent = web/desktop/mobile UI interaction
- Gaming = Minecraft, game playing with visual input
- Scientific = molecule, protein, materials science
- Generation = image/video/audio generation pipelines
- Visual_QA = VQA, visual reasoning, visual grounding (not embodied)
- Audio_Speech = TTS, ASR, speech-based assistants
- Medical = clinical, pathology, radiology
- Benchmark = primarily proposes an evaluation benchmark for multi-model systems
- Document_Understanding = OCR, document parsing, table extraction
- Code_Agent = code generation with tool use involving neural models

### topology:
- Pipeline: strictly sequential flow A→B→C
- Star: central LLM hub that dispatches to multiple models on demand
- ClosedLoop: output feeds back to perception for re-verification/retry
- Hybrid: combines multiple patterns (e.g., star dispatch with closed-loop verification)
- Unclear: architecture not clearly described or too simple to classify

### interface_type:
- Symbolic: models communicate via text, bounding boxes, labels, coordinates, structured data
- Continuous: models pass embeddings, feature vectors, soft prompts, hidden states directly
- Mixed: both symbolic and continuous interfaces are used

### training_paradigm:
- ZeroShot: all models are off-the-shelf, composed via prompting/API calls only
- ModularFinetune: some lightweight adapter/projector is trained at model boundaries
- JointOptimization: end-to-end or multi-stage joint training of multiple components
- Mixed: combination of the above

### model_roles:
- Only list roles that actually appear. Omit empty roles entirely.
- "orchestrator" = the central brain/planner (usually LLM)
- "perceiver" = sensory input processing (vision, audio, etc.)
- "generator" = content generation (images, audio, video)
- "executor" = action execution (robot control, game actions)
- Use actual model names when mentioned; use generic descriptions (e.g., "object detector", "depth estimator") when specific names aren't given.

### survey_chapters:
- List ALL chapters this paper is relevant to (can be multiple)
- Most papers will be relevant to Ch3 (architecture) + one Ch5 sub-domain + possibly Ch6 (training)

Be precise and factual. Only annotate what the paper actually describes."""


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
    """Call LLM API with streaming support."""
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
        'query_id': 'annotate_' + str(uuid.uuid4()),
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


_REF_PATTERNS = [
    re.compile(r'(?:^|\n)\s*(References|REFERENCES|Bibliography|BIBLIOGRAPHY)\s*(?:\n|$)', re.MULTILINE),
    re.compile(r'(?:^|\n)\s*\d+[\.\s]+(References|REFERENCES)\s*(?:\n|$)', re.MULTILINE),
]


def extract_pre_reference_text(pdf_path: str) -> tuple:
    """
    Extract text before References section from a PDF.

    Returns: (text, ref_page, total_pages, error)
    """
    try:
        doc = fitz.open(pdf_path)
    except Exception as e:
        return '', None, 0, f'fitz_open_error: {e}'

    total_pages = doc.page_count

    ref_page_idx = None
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
        for pn in range(ref_page_idx + 1):
            page_text = doc[pn].get_text()
            if pn == ref_page_idx:
                pre_ref_text += page_text[:ref_char_offset]
            else:
                pre_ref_text += page_text
    else:
        for pn in range(min(MAX_PRE_REF_PAGES, total_pages)):
            pre_ref_text += doc[pn].get_text()

    doc.close()

    if len(pre_ref_text) > MAX_TEXT_CHARS:
        pre_ref_text = pre_ref_text[:MAX_TEXT_CHARS] + '\n\n[... text truncated ...]'

    ref_page = (ref_page_idx + 1) if ref_page_idx is not None else None
    return pre_ref_text, ref_page, total_pages, None


def load_papers_to_annotate() -> list:
    """
    Load YES+MAYBE papers from deep screening results.
    Also resolve their PDF paths from metadata.
    """
    papers = []
    for f in sorted(DEEP_SCREENING_DIR.glob('deep_screen_*.jsonl')):
        with open(f, 'r') as fh:
            for line in fh:
                if line.strip():
                    rec = json.loads(line)
                    if rec.get('label') in ('YES', 'MAYBE'):
                        papers.append(rec)

    # Build paper_id -> pdf_path mapping from metadata
    pdf_map = {}
    for meta_file in sorted(METADATA_DIR.glob('*.json')):
        stem = meta_file.stem
        parts = stem.rsplit('.', 1)
        if len(parts) != 2:
            continue
        venue, year_str = parts
        data = json.loads(meta_file.read_text())
        for p in data:
            safe_title = re.sub(r'[^\w\s-]', '', html.unescape(p.get('title', '')))[:80].strip()
            safe_title = re.sub(r'\s+', '_', safe_title)
            pdf_path = PDF_DIR / venue / year_str / f"{p['paper_id']}_{safe_title}.pdf"
            if pdf_path.exists():
                pdf_map[p['paper_id']] = str(pdf_path)

    result = []
    no_pdf = 0
    for p in papers:
        pid = p['paper_id']
        if pid in pdf_map:
            p['pdf_path'] = pdf_map[pid]
            result.append(p)
        else:
            no_pdf += 1

    if no_pdf > 0:
        print(f'  ⚠️  {no_pdf} papers have no PDF, skipped')

    return result


def load_completed(output_dir: Path) -> set:
    """Load already-annotated paper_ids for checkpoint/resume."""
    completed = set()
    for f in output_dir.glob('annotate_*.jsonl'):
        with open(f, 'r') as fh:
            for line in fh:
                if line.strip():
                    rec = json.loads(line)
                    completed.add(rec['paper_id'])
    return completed


def build_annotation_input(paper: dict, pdf_text: str) -> str:
    """Build user prompt for annotation: paper metadata + full text."""
    title = html.unescape(paper.get('title', ''))
    conference = paper.get('conference', '')
    year = paper.get('year', '')
    # Include the deep screening reasoning as context for the annotator
    screening_label = paper.get('label', '')
    screening_reasoning = paper.get('reasoning', '')
    screening_dims = paper.get('dimensions', [])
    screening_models = paper.get('key_models', [])

    parts = [
        f"**Title**: {title}",
        f"**Venue**: {conference} {year}",
        f"**Previous Screening**: {screening_label}",
        f"**Screening Reasoning**: {screening_reasoning}",
        f"**Screening Dimensions**: {', '.join(screening_dims) if isinstance(screening_dims, list) else screening_dims}",
        f"**Key Models Identified**: {', '.join(screening_models) if isinstance(screening_models, list) else screening_models}",
        f"\n**Full Paper Text (before references)**:\n{pdf_text}",
        "\nBased on the full paper content above, provide the structured annotation in the JSON format specified."
    ]
    return '\n'.join(parts)


def parse_annotation_output(output: str) -> dict:
    """
    Parse LLM annotation output. Expects a JSON block.
    Robust extraction: find the first { ... } JSON block.
    """
    if not output:
        return {'parse_error': 'empty output'}

    output = output.strip()

    # Try to extract JSON from markdown code block
    json_match = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', output, re.DOTALL)
    if json_match:
        json_str = json_match.group(1).strip()
    else:
        # Try to find raw JSON object
        brace_start = output.find('{')
        brace_end = output.rfind('}')
        if brace_start != -1 and brace_end > brace_start:
            json_str = output[brace_start:brace_end + 1]
        else:
            return {'parse_error': 'no JSON found', 'raw_output': output[:500]}

    try:
        parsed = json.loads(json_str)
        return parsed
    except json.JSONDecodeError as e:
        # Try fixing common issues: trailing commas, single quotes
        fixed = re.sub(r',\s*}', '}', json_str)
        fixed = re.sub(r',\s*]', ']', fixed)
        try:
            parsed = json.loads(fixed)
            return parsed
        except json.JSONDecodeError:
            return {'parse_error': f'JSON decode error: {e}', 'raw_output': json_str[:500]}


def _run_worker_process(group_models, tasks, tmp_file, sys_prompt,
                        shared_token_counter, shared_done_counter,
                        shared_ok_counter, shared_err_counter,
                        shared_parse_fail_counter,
                        shutdown_event):
    """Subprocess entry: process assigned annotation tasks."""
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

        # Step 1: Extract PDF text
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
                'screening_label': paper.get('label', ''),
                'annotation_status': 'PDF_PARSE_ERROR',
                'parse_error': parse_err or 'empty_text',
            }
            line = json.dumps(output_record, ensure_ascii=False) + '\n'
            write_buffer.put(line)
            return

        # Step 2: Build prompt and call LLM
        user_content = build_annotation_input(paper, pdf_text)

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

        # Step 3: Parse annotation
        annotation = parse_annotation_output(result_text)

        output_record = {
            'paper_id': paper_id,
            'title': html.unescape(paper.get('title', '')),
            'conference': paper.get('conference', ''),
            'year': paper.get('year', 0),
            'screening_label': paper.get('label', ''),
            'annotation_status': 'ERROR' if 'parse_error' in annotation else 'OK',
            'model': model_used,
            # Flatten annotation fields
            'application_domain': annotation.get('application_domain', ''),
            'application_domain_secondary': annotation.get('application_domain_secondary'),
            'topology': annotation.get('topology', ''),
            'topology_details': annotation.get('topology_details', ''),
            'interface_type': annotation.get('interface_type', ''),
            'interface_details': annotation.get('interface_details', ''),
            'training_paradigm': annotation.get('training_paradigm', ''),
            'training_details': annotation.get('training_details', ''),
            'has_error_correction': annotation.get('has_error_correction', False),
            'error_correction_details': annotation.get('error_correction_details'),
            'model_roles': annotation.get('model_roles', {}),
            'brief_pipeline': annotation.get('brief_pipeline', ''),
            'survey_chapters': annotation.get('survey_chapters', []),
        }

        # If there was a parse error, preserve it
        if 'parse_error' in annotation:
            output_record['parse_error'] = annotation['parse_error']
            if 'raw_output' in annotation:
                output_record['raw_output'] = annotation['raw_output']

        line = json.dumps(output_record, ensure_ascii=False) + '\n'
        write_buffer.put(line)

        with count_lock:
            local_success += 1
        with shared_done_counter.get_lock():
            shared_done_counter.value += 1

        status = output_record['annotation_status']
        if status == 'OK':
            with shared_ok_counter.get_lock():
                shared_ok_counter.value += 1
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
        print(f'[PID {os.getpid()}] Interrupted. ok: {local_success}, fail: {local_fail}')
        return

    executor.shutdown(wait=True)
    write_done.set()
    writer.join()

    print(f'[PID {os.getpid()}] Done. ok: {local_success}, fail: {local_fail}')


def _merge_tmp_files_to_venue(tmp_files, output_dir):
    """Merge temp files into venue-year annotate_XXX.YYYY.jsonl files."""
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
            fpath = output_dir / f'annotate_{venue_key}.jsonl'
            with open(fpath, 'a', encoding='utf-8') as f_out:
                f_out.writelines(lines)
        os.remove(tmp_file)
    if merged > 0:
        print(f'Merged {merged} records to venue-year files')
    return merged


def run_annotation(
    papers: list,
    output_dir: Path,
    models: list,
    force: bool = False,
):
    """Multi-process annotation pipeline."""
    total_workers = sum(w for _, w in models)
    model_info = ', '.join(f'{name}({w})' for name, w in models)

    # Checkpoint
    completed = set()
    if not force:
        completed = load_completed(output_dir)

    pending = [p for p in papers if p['paper_id'] not in completed]

    if not pending:
        print('All papers already annotated!')
        return

    # Split into sub-processes by WORKERS_PER_PROCESS
    process_groups = []
    for model_name, total_w in models:
        remaining = total_w
        while remaining > 0:
            chunk = min(remaining, WORKERS_PER_PROCESS)
            process_groups.append([(model_name, chunk)])
            remaining -= chunk

    print(f'\n{"=" * 60}')
    print(f'Structured Annotation (Outline-Aligned)')
    print(f'Models: {model_info} → Total workers: {total_workers}')
    print(f'Thinking: {USE_THINKING}')
    print(f'Workers per process: {WORKERS_PER_PROCESS} → {len(process_groups)} processes')
    print(f'Total YES+MAYBE papers: {len(papers)}')
    print(f'Completed: {len(completed)}, Pending: {len(pending)}')
    if force:
        print(f'  ⚠️  Force mode: ignoring existing results, re-annotate all')
    print(f'Output dir: {output_dir}')
    print(f'{"=" * 60}\n')

    if force:
        for f in output_dir.glob('annotate_*.jsonl'):
            f.unlink()
            print(f'  Deleted: {f.name}')

    # Distribute tasks proportionally
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

    tmp_files = [str(output_dir / f'.tmp_anno_group{gi}') for gi in range(len(process_groups))]

    ctx = mp.get_context('spawn')

    shared_token_counter = ctx.Value('q', 0)
    shared_done_counter = ctx.Value('q', 0)
    shared_ok_counter = ctx.Value('q', 0)
    shared_err_counter = ctx.Value('q', 0)
    shared_parse_fail_counter = ctx.Value('q', 0)
    shutdown_event = ctx.Event()

    processes = []
    for gi, (grp_models, grp_tasks) in enumerate(zip(process_groups, group_tasks)):
        if not grp_tasks:
            continue
        p = ctx.Process(
            target=_run_worker_process,
            args=(grp_models, grp_tasks, tmp_files[gi], ANNOTATION_SYSTEM_PROMPT,
                  shared_token_counter, shared_done_counter,
                  shared_ok_counter, shared_err_counter,
                  shared_parse_fail_counter,
                  shutdown_event),
            name=f'AnnotateProc{gi}',
        )
        p.start()
        processes.append(p)

    # Main process: progress bar
    total_tasks = len(pending)
    pbar = tqdm(total=total_tasks, desc=f'Annotating ({total_workers}w, {len(processes)}p)')

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

            ok = shared_ok_counter.value
            e = shared_err_counter.value
            pf = shared_parse_fail_counter.value

            pbar.set_postfix_str(
                f'OK={ok} ERR={e} PF={pf} | tok={current_tokens:,} avg={avg_tps:,.0f}t/s cur={cur_tps:,.0f}t/s',
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
    final_ok = shared_ok_counter.value
    final_err = shared_err_counter.value
    final_pf = shared_parse_fail_counter.value
    elapsed = time.time() - start_time

    print(f'\n{"=" * 60}')
    print(f'Annotation complete!')
    print(f'  OK   (parsed successfully):   {final_ok}')
    print(f'  ERR  (LLM / parse failed):    {final_err}')
    print(f'  PF   (PDF parse failed):      {final_pf}')
    print(f'  Total:                        {final_done}')
    print(f'  Tokens: {final_tokens:,} | Elapsed: {elapsed:.1f}s | Avg: {final_tokens/elapsed:,.0f} t/s')
    print(f'Output dir: {output_dir}')
    print(f'{"=" * 60}')


def print_summary(output_dir: Path):
    """Print annotation result summary with distribution stats."""
    if not output_dir.exists():
        print("No annotation results found.")
        return

    from collections import Counter

    all_records = []
    for f in sorted(output_dir.glob('annotate_*.jsonl')):
        with open(f, 'r') as fh:
            for line in fh:
                if line.strip():
                    all_records.append(json.loads(line))

    if not all_records:
        print("No annotation records found.")
        return

    ok_records = [r for r in all_records if r.get('annotation_status') == 'OK']
    err_records = [r for r in all_records if r.get('annotation_status') == 'ERROR']
    pf_records = [r for r in all_records if r.get('annotation_status') == 'PDF_PARSE_ERROR']

    print(f'\n{"=" * 70}')
    print(f'Annotation Summary')
    print(f'  Total: {len(all_records)} | OK: {len(ok_records)} | ERR: {len(err_records)} | PDF_FAIL: {len(pf_records)}')
    print(f'{"=" * 70}')

    if not ok_records:
        return

    # Application domain distribution
    domain_counter = Counter(r.get('application_domain', 'Unknown') for r in ok_records)
    print(f'\n📊 Application Domain Distribution:')
    for domain, cnt in domain_counter.most_common():
        bar = '█' * (cnt // 2)
        print(f'  {domain:<30} {cnt:>4}  {bar}')

    # Topology distribution
    topo_counter = Counter(r.get('topology', 'Unknown') for r in ok_records)
    print(f'\n📊 Topology Distribution:')
    for topo, cnt in topo_counter.most_common():
        bar = '█' * (cnt // 2)
        print(f'  {topo:<20} {cnt:>4}  {bar}')

    # Interface type distribution
    iface_counter = Counter(r.get('interface_type', 'Unknown') for r in ok_records)
    print(f'\n📊 Interface Type Distribution:')
    for iface, cnt in iface_counter.most_common():
        bar = '█' * (cnt // 2)
        print(f'  {iface:<20} {cnt:>4}  {bar}')

    # Training paradigm distribution
    train_counter = Counter(r.get('training_paradigm', 'Unknown') for r in ok_records)
    print(f'\n📊 Training Paradigm Distribution:')
    for tp, cnt in train_counter.most_common():
        bar = '█' * (cnt // 2)
        print(f'  {tp:<20} {cnt:>4}  {bar}')

    # Error correction
    ec_counter = Counter(bool(r.get('has_error_correction')) for r in ok_records)
    print(f'\n📊 Error Correction:')
    print(f'  Has error correction:    {ec_counter.get(True, 0)}')
    print(f'  No error correction:     {ec_counter.get(False, 0)}')

    # Survey chapters coverage
    ch_counter = Counter()
    for r in ok_records:
        for ch in r.get('survey_chapters', []):
            ch_counter[ch] += 1
    print(f'\n📊 Survey Chapter Coverage:')
    for ch, cnt in ch_counter.most_common():
        bar = '█' * (cnt // 3)
        print(f'  {ch:<25} {cnt:>4}  {bar}')

    # Per-venue breakdown
    print(f'\n📊 Per-Venue Breakdown:')
    venue_counter = Counter(f"{r['conference']}.{r['year']}" for r in ok_records)
    print(f'  {"Venue":<20} {"Count":<8}')
    print(f'  {"-" * 28}')
    for venue, cnt in sorted(venue_counter.items()):
        print(f'  {venue:<20} {cnt:<8}')

    print(f'\n{"=" * 70}')


def main():
    parser = argparse.ArgumentParser(
        description='Structured annotation for Multi-Model Agent Survey (outline-aligned)'
    )
    parser.add_argument(
        '--phase', choices=['annotate', 'summary'], default='annotate',
        help='annotate: run annotation | summary: view stats',
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
        help='Force re-annotation (ignore existing results)',
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

    # Phase: annotate
    print('Loading YES+MAYBE papers from deep screening...')
    all_papers = load_papers_to_annotate()

    if not all_papers:
        print('No papers to annotate. Run deep screening first.')
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

    label_counts = Counter(p.get('label', '?') for p in all_papers)
    print(f'Found {len(all_papers)} papers to annotate (YES: {label_counts.get("YES", 0)}, MAYBE: {label_counts.get("MAYBE", 0)})')
    print(f'Distributed across {len(grouped)} venue-years:')
    for key in sorted(grouped.keys()):
        print(f'  [{key}] {len(grouped[key])}')

    run_annotation(all_papers, OUTPUT_DIR, SCREEN_MODELS, force=args.force)
    print_summary(OUTPUT_DIR)


if __name__ == '__main__':
    from collections import Counter
    main()
