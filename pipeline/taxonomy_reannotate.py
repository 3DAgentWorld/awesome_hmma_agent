#!/usr/bin/env python3
"""
Taxonomy Re-Annotation Pipeline (Strict HMMA Definition)

Re-annotates papers flagged as suspicious in the original taxonomy pass
(papers where deterministic tools were mislabeled as execution models, and
papers labeled LLM as Orchestrator), using a stricter HMMA definition:
a heterogeneous model must be a *learned* parametric model. Deterministic
programs (Python interpreter, PyAutoGUI, A*/FMM planners, search engines,
calculators, symbolic solvers, browser automation libraries, file system APIs,
etc.) do NOT count as execution models. Papers whose only "action" mechanism is
such tools are re-classified accordingly; papers with no learned heterogeneous
model at all become LLM as Orchestrator and are later removed from the corpus.

Inputs:
  - papers_data/taxonomy_reannotate_suspects.jsonl  (suspect papers)
  - papers_data/taxonomy/taxonomy_*.jsonl           (original annotations, as context)
Outputs:
  - papers_data/taxonomy_v2/taxonomy_*.jsonl        (new annotations, separate directory)

Reuses the same infrastructure as taxonomy_annotate.py: multiprocessing, token
tracker, PDF parsing, API calling, checkpoint resume, graceful Ctrl+C exit.
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
from collections import Counter, defaultdict

import fitz  # PyMuPDF
import requests
from tqdm import tqdm
from paper_utils import paper_key, pdf_path, valid_pdf

BASE_DIR = Path(__file__).parent
DEEP_SCREENING_DIR = BASE_DIR / "papers_data" / "deep_screening"
ANNOTATION_DIR = BASE_DIR / "papers_data" / "annotations"
PDF_DIR = BASE_DIR / "papers_data" / "pdfs"
METADATA_DIR = BASE_DIR / "papers_data" / "metadata"
ORIGINAL_TAXONOMY_DIR = BASE_DIR / "papers_data" / "taxonomy"
SUSPECT_LIST_FILE = BASE_DIR / "papers_data" / "taxonomy_reannotate_suspects.jsonl"
OUTPUT_DIR = BASE_DIR / "papers_data" / "taxonomy_v2"

SCREEN_MODELS = [
    ('kimi-k2.6-0507', 30),
]

WORKERS_PER_PROCESS = 50

LOCAL_API_URL = 'http://YOUR_API_HOST:PORT/v1/chat/completions'
LOCAL_API_TOKEN = 'Bearer YOUR_API_KEY'
LOCAL_API_WSID = 'YOUR_WSID'

GEN_PARAMS = {
    'temperature': 0.1,
    'top_p': 0.9,
    'top_k': 20,
    'repetition_penalty': 1.0,
    'output_seq_len': 3072,
    'max_input_seq_len': 65536,
}

USE_THINKING = True
MAX_RETRIES = 3
RETRY_DELAY = 1
MAX_PRE_REF_PAGES = 13
MAX_TEXT_CHARS = 80000


TAXONOMY_SYSTEM_PROMPT = """You are an expert AI researcher performing TAXONOMY ANNOTATION for a survey on "Heterogeneous Multi-Model Agents (HMMA)".

This is a RE-ANNOTATION task: an earlier annotation pass was too permissive (it counted Python interpreters, PyAutoGUI, A*/FMM path planners, browser-automation libraries, web-search APIs, calculators, and other deterministic programs as "execution models"). You will re-classify the paper under a STRICT definition that is consistent with the survey's scope.

================================================================
SCOPE: WHAT COUNTS AS A "HETEROGENEOUS MODEL" IN THIS SURVEY
================================================================
A heterogeneous model in HMMA must be a **learned, parametric model** distinct from the orchestrating LLM. We classify these into three functional roles:

(P) PERCEPTION model: extracts structured information from raw sensory input (vision / audio / 3D / sensor signals).
    Examples: CLIP, BLIP-2, SAM, GroundingDINO, YOLO, Whisper, depth estimators, OCR neural models, VLM perception modules, scene-graph predictors.

(G) GENERATION model: produces new media content (image / video / 3D / audio).
    Examples: Stable Diffusion, DALL-E, ControlNet, Flux, Sora, AudioLDM, neural TTS, music generators, neural 3D generators (e.g. DreamFusion-style).

(A) ACTION (execution) model: a *learned* policy / controller that maps observations to actions in some environment.
    Examples: RL policies (PPO/SAC/DQN actor networks), VLA models (RT-2, OpenVLA), diffusion policies, CLIPort, learned navigation policies (HLSM, VLFM), pretrained manipulation policies, learned game-playing agents (VPT), GUI grounding models that produce learned action distributions.

================================================================
WHAT IS **NOT** A HETEROGENEOUS MODEL (CRITICAL)
================================================================
The following are DETERMINISTIC TOOLS / SOFTWARE PROGRAMS, not models. They must NEVER be listed in `non_llm_models.execution` (or perception/generation):

  - Code execution engines: Python interpreter, IPython server, bash shell, Jupyter, code-as-policy executors, Graph executor, code interpreter, code executor.
  - GUI / desktop automation libraries: PyAutoGUI, Selenium, Playwright, Puppeteer, computer-use APIs (when used as low-level mouse/keyboard drivers; the *grounding model* that decides where to click IS a model and goes under perception or action).
  - Classical path planners / search algorithms: A* planner, FMM (Fast Marching Method), Dijkstra, BFS, DFS, frontier-based exploration, MCTS, rule-based planners, deterministic local policies.
  - External information / utility APIs (treated as tools, NOT models): Google/Bing/DuckDuckGo Web Search, Wolfram Alpha, calculators, Wikipedia API, ArXiv API, weather/map/stock APIs, file-system APIs, GPS API, Uber API.
  - Symbolic solvers: Z3, SymPy, constraint solvers, rule-based engines, regex.
  - Software APIs of authoring tools: Blender Python API, FreeCAD API, Matplotlib (these are deterministic libraries; only count any *neural* sub-module they wrap as a model, not the API itself).
  - Static analysis / compiler tools: tree-sitter, BM25 retrievers, lexical retrievers.

If a paper's "action capability" comes ONLY from such deterministic tools, the paper has **no Action model**; do NOT use a pattern that contains "Action".

================================================================
PRIMARY AXIS: Collaboration Pattern (each paper → EXACTLY ONE)
================================================================

**1. LLM + Perception**
   LLM + Perception model(s) only. Output is text (answers, descriptions, analyses). The system may invoke deterministic tools (Python, calculator, web search) but those do not count.

**2. LLM + Perception + Generation**
   LLM + Perception model(s) + Generation model(s). Pipeline both understands existing media and produces new media.

**3. LLM + Perception + Action**
   LLM + Perception model(s) + at least one **learned** Action model (RL policy, VLA, diffusion policy, learned controller, etc.). Deterministic path planners/PyAutoGUI alone DO NOT qualify.

**4. LLM + Perception + Generation + Action**
   All three roles are present, with at least one *learned* model in each role.

**5. LLM + Generation**
   LLM + Generation model(s) only (no separate perception model; input is text-dominant).

**6. LLM as Orchestrator** (= compound-AI / pure tool-augmented LLM, OUT-OF-SCOPE for HMMA)
   LLM has NO learned heterogeneous model collaborator. Every "model" it accesses is either (a) another LLM/VLM accessed as a high-level service with no specialized role, or (b) deterministic tools (code interpreter, search engine, calculator, browser automation), or (c) the paper is purely a benchmark / API selection / tool-routing study without a concrete heterogeneous learned-model pipeline.
   IMPORTANT: papers in this category will be REMOVED from the survey corpus, so use this label only when the paper genuinely has no learned heterogeneous model.

================================================================
DECISION ALGORITHM (follow strictly)
================================================================
Step 1. Read the paper. Enumerate every non-LLM neural/learned model it uses.
Step 2. For each, decide its functional role: P / G / A. Discard everything that is a deterministic tool (per the list above).
Step 3. Form the multiset {P?, G?, A?} of present roles.
Step 4. Map to a Collaboration Pattern:
   - {P}                → "LLM + Perception"
   - {G}                → "LLM + Generation"
   - {P, G}             → "LLM + Perception + Generation"
   - {P, A}             → "LLM + Perception + Action"
   - {P, G, A}          → "LLM + Perception + Generation + Action"
   - {} (empty)         → "LLM as Orchestrator"
   - {A} only or {G, A} only without P → these are extremely rare; in practice the system will have some perception. If you see this, double-check; if truly no perception model, choose the closest match and explain in justification.

Step 5. Populate `non_llm_models`:
   - `perception`: list ONLY learned perception models (e.g. "CLIP ViT-L/14", "GroundingDINO", "Whisper").
   - `generation`: list ONLY learned generation models.
   - `execution`: list ONLY learned action/policy models. **DO NOT** include Python interpreters, PyAutoGUI, FMM, A*, search APIs, calculators, browser automation libraries, file APIs, etc.
   - If a category has no learned model, leave its list empty `[]`.

Step 6. Mention any deterministic tools used by the system in `pipeline_summary` (so the information is not lost), but do NOT put them in `non_llm_models`.

================================================================
SECONDARY AXIS: Application Environment (choose ONE)
================================================================
Embodied_Navigation, Robot_Manipulation, Autonomous_Driving, GUI_Web_Agent, Gaming, Image_Generation, Video_Generation, 3D_Generation, Audio_Speech, Visual_QA_Reasoning, Document_Understanding, Medical_Analysis, Scientific_Discovery, Retrieval_Augmented, Code_Development, Benchmark_Evaluation, Multi_Domain, Other.

================================================================
ORTHOGONAL DIMENSIONS
================================================================
LLM Role Types (select ALL that apply): Planner, Reasoner, Router, Verifier, Coordinator, Reflector, Translator.
Feedback Structure: None | Self_Correction | Cross_Model_Feedback | Iterative_Refinement | Human_in_Loop.
Information Flow: Sequential | Broadcast | Converge | Star | Iterative | DAG.
Uncertainty Handling: None | Confidence_Threshold | Voting_Ensemble | LLM_Verification | Retry_Fallback | Cascaded_Filtering.
Model Coupling: Loose | Medium | Tight.

================================================================
OUTPUT FORMAT (strict JSON, no extra commentary)
================================================================
```json
{
  "collaboration_pattern": "<one of the 6 patterns>",
  "collaboration_justification": "<1-2 sentences. Explicitly identify the LEARNED P/G/A models. If no learned models, explain why this is LLM as Orchestrator.>",
  "application_environment": "<one of the L2 categories>",
  "application_justification": "<1 sentence>",
  "llm_role_types": ["..."],
  "feedback_structure": "...",
  "feedback_details": "..." or null,
  "information_flow": "...",
  "uncertainty_handling": "...",
  "uncertainty_details": "..." or null,
  "model_coupling": "Loose|Medium|Tight",
  "non_llm_models": {
    "perception": ["<learned perception models only>"],
    "generation": ["<learned generation models only>"],
    "execution": ["<learned action/policy models only — NO Python, NO PyAutoGUI, NO A*/FMM, NO search APIs, NO calculator, NO browser libs>"]
  },
  "deterministic_tools_used": ["<optional: list of deterministic tools the paper uses, for transparency>"],
  "llm_models": ["<LLMs used as orchestrator/brain>"],
  "pipeline_summary": "<2-3 sentences describing the full pipeline, mentioning both learned models and deterministic tools used>",
  "is_pure_compound_ai": <true if the paper is LLM + only deterministic tools / pure tool-use, false otherwise>
}
```

GUIDELINES:
1. Use the strict definitions above. If borderline, default to the more conservative choice (do NOT add "Action" unless a learned policy is explicitly described).
2. A VLM (LLaVA, Qwen-VL, GPT-4V, etc.) used as the ORCHESTRATOR brain counts as the LLM, not as a perception model. Only count VLMs that serve as a separate perception MODULE feeding into a different LLM.
3. Be precise with model names; use actual names when given.
4. Focus on the paper's PROPOSED SYSTEM, not baselines."""

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
        'query_id': 'taxo_reannot_' + str(uuid.uuid4()),
        'messages': messages,
        'stream': use_stream,
        'random_seed': 42,
        'openai_infer': True,
        'thinking': USE_THINKING,
        'chat_template_kwargs': {'thinking': USE_THINKING, 'enable_thinking': USE_THINKING},
        **GEN_PARAMS,
    }
    try:
        req_timeout = (30, 180) if use_stream else timeout
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


def extract_pre_reference_text(pdf_path):
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


def _build_pdf_map():
    pdf_map = {}
    for meta_file in sorted(METADATA_DIR.glob('*.json')):
        stem = meta_file.stem
        parts = stem.rsplit('.', 1)
        if len(parts) != 2:
            continue
        venue, year_str = parts
        try:
            data = json.loads(meta_file.read_text())
        except Exception:
            continue
        for p in data:
            p = {**p, 'conference': venue, 'year': int(year_str)}
            path = pdf_path(PDF_DIR, p)
            if valid_pdf(path):
                pdf_map[paper_key(p)] = str(path)
    return pdf_map


def _load_original_taxonomy_records():
    """Load original taxonomy annotations indexed by paper_id, as re-annotation context."""
    out = {}
    for f in sorted(ORIGINAL_TAXONOMY_DIR.glob('taxonomy_*.jsonl')):
        with open(f) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                if rec.get('taxonomy_status') == 'OK':
                    out[paper_key(rec)] = rec
    return out


def _load_deep_screening_yes():
    yes_papers = {}
    for f in sorted(DEEP_SCREENING_DIR.glob('deep_screen_*.jsonl')):
        with open(f, 'r') as fh:
            for line in fh:
                if line.strip():
                    rec = json.loads(line)
                    if rec.get('label') == 'YES':
                        yes_papers[paper_key(rec)] = rec
    return yes_papers


def load_suspect_papers():
    """Load the suspect papers along with PDF paths and original annotation context."""
    if not SUSPECT_LIST_FILE.exists():
        print(f'ERROR: Suspect list not found: {SUSPECT_LIST_FILE}')
        return []

    suspects = []
    with open(SUSPECT_LIST_FILE) as f:
        for line in f:
            if line.strip():
                suspects.append(json.loads(line))

    pdf_map = _build_pdf_map()
    original_taxo = _load_original_taxonomy_records()
    deep_yes = _load_deep_screening_yes()

    result = []
    no_pdf = 0
    for s in suspects:
        pid = paper_key(s)
        if pid not in pdf_map:
            no_pdf += 1
            continue
        screen_rec = deep_yes.get(pid, {})
        orig = original_taxo.get(pid, {})
        paper = {
            'paper_id': s['paper_id'],
            'title': s.get('title') or screen_rec.get('title', ''),
            'conference': s.get('conference') or screen_rec.get('conference', ''),
            'year': s.get('year') or screen_rec.get('year', 0),
            'pdf_path': pdf_map[pid],
            'key_models': screen_rec.get('key_models', []),
            'suspect_reasons': s.get('reasons', []),
            'original_pattern': orig.get('collaboration_pattern', ''),
            'original_non_llm_models': orig.get('non_llm_models', {}),
            'original_pipeline_summary': orig.get('pipeline_summary', ''),
        }
        result.append(paper)
    if no_pdf:
        print(f'  WARNING: {no_pdf} suspect papers have no PDF, skipped')
    return result


def load_completed(output_dir):
    completed = set()
    for f in output_dir.glob('taxonomy_*.jsonl'):
        with open(f, 'r') as fh:
            for line in fh:
                if line.strip():
                    rec = json.loads(line)
                    if rec.get('taxonomy_status') == 'OK':
                        completed.add(paper_key(rec))
    return completed


def build_taxonomy_input(paper, pdf_text):
    title = html.unescape(paper.get('title', ''))
    conference = paper.get('conference', '')
    year = paper.get('year', '')
    key_models = paper.get('key_models', [])

    parts = [
        f"**Title**: {title}",
        f"**Venue**: {conference} {year}",
        f"**Key Models Identified (from earlier screening)**: "
        f"{', '.join(key_models) if isinstance(key_models, list) else key_models}",
        "",
        "**Why this paper is being re-annotated**:",
        f"  Reasons: {', '.join(paper.get('suspect_reasons', []))}",
        f"  Original (suspect) pattern: {paper.get('original_pattern', '')}",
        f"  Original non_llm_models: {json.dumps(paper.get('original_non_llm_models', {}), ensure_ascii=False)}",
        f"  Original pipeline summary: {paper.get('original_pipeline_summary', '')}",
        "",
        "The earlier annotation may have incorrectly listed deterministic tools "
        "(Python interpreter, PyAutoGUI, A*/FMM path planners, web search APIs, "
        "calculators, browser-automation libraries, etc.) as 'execution models'. "
        "Please re-annotate strictly according to the rules in the system prompt.",
        "",
        f"**Full Paper Text (before references)**:\n{pdf_text}",
        "",
        "Now produce the re-annotation in the JSON format specified in the system prompt.",
    ]
    return '\n'.join(parts)


def parse_taxonomy_output(output):
    if not output:
        return {'parse_error': 'empty output'}

    output = output.strip()
    json_match = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', output, re.DOTALL)
    if json_match:
        json_str = json_match.group(1).strip()
    else:
        brace_start = output.find('{')
        brace_end = output.rfind('}')
        if brace_start != -1 and brace_end > brace_start:
            json_str = output[brace_start:brace_end + 1]
        else:
            return {'parse_error': 'no JSON found', 'raw_output': output[:500]}

    try:
        return json.loads(json_str)
    except json.JSONDecodeError as e:
        fixed = re.sub(r',\s*}', '}', json_str)
        fixed = re.sub(r',\s*]', ']', fixed)
        try:
            return json.loads(fixed)
        except json.JSONDecodeError:
            return {'parse_error': f'JSON decode error: {e}', 'raw_output': json_str[:500]}


VALID_COLLAB_PATTERNS = {
    'LLM + Perception', 'LLM + Perception + Generation', 'LLM + Perception + Action',
    'LLM + Perception + Generation + Action', 'LLM + Generation', 'LLM as Orchestrator',
}

VALID_APP_ENVS = {
    'Embodied_Navigation', 'Robot_Manipulation', 'Autonomous_Driving',
    'GUI_Web_Agent', 'Gaming', 'Image_Generation', 'Video_Generation',
    '3D_Generation', 'Audio_Speech', 'Visual_QA_Reasoning',
    'Document_Understanding', 'Medical_Analysis', 'Scientific_Discovery',
    'Retrieval_Augmented', 'Code_Development', 'Benchmark_Evaluation',
    'Multi_Domain', 'Other',
}


def validate_taxonomy(parsed):
    if 'parse_error' in parsed:
        return parsed

    cp = parsed.get('collaboration_pattern', '')
    if cp not in VALID_COLLAB_PATTERNS:
        for valid in VALID_COLLAB_PATTERNS:
            if cp.lower().replace(' ', '').replace('_', '') in valid.lower().replace('-', ''):
                parsed['collaboration_pattern'] = valid
                break
        else:
            parsed['collaboration_pattern_warning'] = f'Unknown pattern: {cp}'

    ae = parsed.get('application_environment', '')
    if ae not in VALID_APP_ENVS:
        for valid in VALID_APP_ENVS:
            if ae.lower().replace(' ', '').replace('_', '') in valid.lower().replace('_', ''):
                parsed['application_environment'] = valid
                break
        else:
            parsed['application_environment_warning'] = f'Unknown env: {ae}'

    return parsed


def _run_worker_process(group_models, tasks, tmp_file, sys_prompt,
                        shared_token_counter, shared_done_counter,
                        shared_ok_counter, shared_err_counter,
                        shared_parse_fail_counter,
                        shutdown_event):
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
    print(f'[PID {os.getpid()}] worker started: {model_info} → workers={total_workers}, tasks={len(tasks)}')

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
                'taxonomy_status': 'PDF_PARSE_ERROR',
                'parse_error': parse_err or 'empty_text',
            }
            write_buffer.put(json.dumps(output_record, ensure_ascii=False) + '\n')
            return

        user_content = build_taxonomy_input(paper, pdf_text)

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

        annotation = parse_taxonomy_output(result_text)
        annotation = validate_taxonomy(annotation)

        output_record = {
            'paper_id': paper_id,
            'title': html.unescape(paper.get('title', '')),
            'conference': paper.get('conference', ''),
            'year': paper.get('year', 0),
            'taxonomy_status': 'ERROR' if 'parse_error' in annotation else 'OK',
            'model': model_used,
            # primary axes
            'collaboration_pattern': annotation.get('collaboration_pattern', ''),
            'collaboration_justification': annotation.get('collaboration_justification', ''),
            'application_environment': annotation.get('application_environment', ''),
            'application_justification': annotation.get('application_justification', ''),
            # orthogonal dimensions
            'llm_role_types': annotation.get('llm_role_types', []),
            'feedback_structure': annotation.get('feedback_structure', ''),
            'feedback_details': annotation.get('feedback_details'),
            'information_flow': annotation.get('information_flow', ''),
            'uncertainty_handling': annotation.get('uncertainty_handling', ''),
            'uncertainty_details': annotation.get('uncertainty_details'),
            'model_coupling': annotation.get('model_coupling', ''),
            # model inventory
            'non_llm_models': annotation.get('non_llm_models', {}),
            'deterministic_tools_used': annotation.get('deterministic_tools_used', []),
            'llm_models': annotation.get('llm_models', []),
            'pipeline_summary': annotation.get('pipeline_summary', ''),
            'is_pure_compound_ai': annotation.get('is_pure_compound_ai', False),
            # re-annotation metadata
            'reannotated': True,
            'suspect_reasons': paper.get('suspect_reasons', []),
            'original_pattern': paper.get('original_pattern', ''),
            'original_non_llm_models': paper.get('original_non_llm_models', {}),
        }

        if 'parse_error' in annotation:
            output_record['parse_error'] = annotation['parse_error']
            if 'raw_output' in annotation:
                output_record['raw_output'] = annotation['raw_output']
        for warn_key in ['collaboration_pattern_warning', 'application_environment_warning']:
            if warn_key in annotation:
                output_record[warn_key] = annotation[warn_key]

        write_buffer.put(json.dumps(output_record, ensure_ascii=False) + '\n')

        with count_lock:
            local_success += 1
        with shared_done_counter.get_lock():
            shared_done_counter.value += 1
        if output_record['taxonomy_status'] == 'OK':
            with shared_ok_counter.get_lock():
                shared_ok_counter.value += 1
        else:
            with shared_err_counter.get_lock():
                shared_err_counter.value += 1

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
                    print(f'[PID {os.getpid()}] error: {e}')
            for task in itertools.islice(task_iter, len(done)):
                pending.add(executor.submit(_process_one, task))
    except KeyboardInterrupt:
        _shutdown.set()
        for future in pending:
            future.cancel()
        executor.shutdown(wait=False, cancel_futures=True)
        write_done.set()
        writer.join(timeout=5)
        print(f'[PID {os.getpid()}] interrupted. ok={local_success}, fail={local_fail}')
        return

    executor.shutdown(wait=True)
    write_done.set()
    writer.join()
    print(f'[PID {os.getpid()}] done. ok={local_success}, fail={local_fail}')


def _merge_tmp_files_to_venue(tmp_files, output_dir):
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
                venue_lines.setdefault(venue_key, []).append(line)
                merged += 1
        for venue_key, lines in venue_lines.items():
            fpath = output_dir / f'taxonomy_{venue_key}.jsonl'
            with open(fpath, 'a', encoding='utf-8') as f_out:
                f_out.writelines(lines)
        os.remove(tmp_file)
    if merged > 0:
        print(f'merged {merged} records into venue-year files')
    return merged


def run_taxonomy_reannotation(papers, output_dir, models, force=False):
    total_workers = sum(w for _, w in models)
    model_info = ', '.join(f'{name}({w})' for name, w in models)

    completed = set()
    if not force:
        completed = load_completed(output_dir)

    pending = [p for p in papers if paper_key(p) not in completed]
    if not pending:
        print('All suspect papers already re-annotated.')
        return

    process_groups = []
    for model_name, total_w in models:
        remaining = total_w
        while remaining > 0:
            chunk = min(remaining, WORKERS_PER_PROCESS)
            process_groups.append([(model_name, chunk)])
            remaining -= chunk

    print(f'\n{"=" * 60}')
    print(f'Taxonomy RE-Annotation (strict HMMA definition)')
    print(f'Models: {model_info} → workers={total_workers}, processes={len(process_groups)}')
    print(f'Thinking: {USE_THINKING}')
    print(f'Suspect papers: {len(papers)}, completed: {len(completed)}, pending: {len(pending)}')
    if force:
        print(f'  WARNING: Force mode: re-do everything')
    print(f'Output dir: {output_dir}')
    print(f'{"=" * 60}\n')

    if force:
        for f in output_dir.glob('taxonomy_*.jsonl'):
            f.unlink()
            print(f'  Deleted: {f.name}')

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

    tmp_files = [str(output_dir / f'.tmp_taxo_reannot_group{gi}') for gi in range(len(process_groups))]

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
            args=(grp_models, grp_tasks, tmp_files[gi], TAXONOMY_SYSTEM_PROMPT,
                  shared_token_counter, shared_done_counter,
                  shared_ok_counter, shared_err_counter,
                  shared_parse_fail_counter, shutdown_event),
            name=f'TaxoReProc{gi}',
        )
        p.start()
        processes.append(p)

    total_tasks = len(pending)
    pbar = tqdm(total=total_tasks, desc=f'Taxonomy-Reannot ({total_workers}w, {len(processes)}p)')

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
                f'OK={ok} ERR={e} PF={pf} | tok={current_tokens:,} avg={avg_tps:,.0f}t/s',
                refresh=True,
            )
            pbar.refresh()
    except KeyboardInterrupt:
        print('\n\nCtrl+C, shutting down workers...')
        shutdown_event.set()
        for p in processes:
            p.join(timeout=10)
            if p.is_alive():
                p.terminate()
        pbar.close()
        _merge_tmp_files_to_venue(tmp_files, output_dir)
        print(f'progress saved: {output_dir}')
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
    print(f'Re-annotation done.')
    print(f'  OK:    {final_ok}')
    print(f'  ERR:   {final_err}')
    print(f'  PF:    {final_pf}')
    print(f'  Total: {final_done}')
    print(f'  Tokens: {final_tokens:,} | Elapsed: {elapsed:.1f}s | Avg: {final_tokens / max(elapsed, 1):,.0f} t/s')
    print(f'Output dir: {output_dir}')
    print(f'{"=" * 60}')


def print_summary(output_dir):
    if not output_dir.exists():
        print('No re-annotation output yet.')
        return

    all_records = []
    for f in sorted(output_dir.glob('taxonomy_*.jsonl')):
        with open(f) as fh:
            for line in fh:
                if line.strip():
                    all_records.append(json.loads(line))

    if not all_records:
        print('No records.')
        return

    ok_records = [r for r in all_records if r.get('taxonomy_status') == 'OK']
    err_records = [r for r in all_records if r.get('taxonomy_status') == 'ERROR']
    pf_records = [r for r in all_records if r.get('taxonomy_status') == 'PDF_PARSE_ERROR']

    print(f'\n{"=" * 80}')
    print(f'Re-Annotation Summary  total={len(all_records)} OK={len(ok_records)} ERR={len(err_records)} PF={len(pf_records)}')
    print(f'{"=" * 80}')

    if not ok_records:
        return

    # New pattern distribution
    new_cp = Counter(r.get('collaboration_pattern', '') for r in ok_records)
    print('\nNew Collaboration Pattern distribution:')
    for cp, c in new_cp.most_common():
        print(f'  {c:>4}  {cp}')

    # Diff: original vs new
    print('\nPattern transitions (original → new):')
    transition = Counter()
    for r in ok_records:
        old = r.get('original_pattern', '')
        new = r.get('collaboration_pattern', '')
        transition[(old, new)] += 1
    for (old, new), c in sorted(transition.items(), key=lambda x: -x[1]):
        marker = '  ' if old == new else 'CHANGED'
        print(f'  {marker} {c:>3}  {old:<45} → {new}')

    pure_compound = [r for r in ok_records if r.get('is_pure_compound_ai') or r.get('collaboration_pattern') == 'LLM as Orchestrator']
    print(f'\nPapers re-classified as pure Compound-AI / LLM as Orchestrator: {len(pure_compound)}')
    for r in pure_compound:
        print(f'  - [{r["conference"]}.{r["year"]}] {r["paper_id"]}: {r["title"][:70]}')


def main():
    parser = argparse.ArgumentParser(
        description='Strict re-annotation for suspect taxonomy papers'
    )
    parser.add_argument('--phase', choices=['annotate', 'summary'], default='annotate')
    parser.add_argument('--model', nargs='+', default=None)
    parser.add_argument('--workers', nargs='+', type=int, default=None)
    parser.add_argument('--no-thinking', action='store_true')
    parser.add_argument('--force', action='store_true')
    args = parser.parse_args()

    global USE_THINKING, SCREEN_MODELS

    if args.no_thinking:
        USE_THINKING = False

    if args.model is not None:
        workers_list = args.workers if args.workers else []
        default_w = SCREEN_MODELS[0][1] if SCREEN_MODELS else 60
        SCREEN_MODELS = []
        for i, m in enumerate(args.model):
            w = workers_list[i] if i < len(workers_list) else default_w
            SCREEN_MODELS.append((m, w))
    elif args.workers is not None:
        default_w = args.workers[0] if args.workers else 60
        SCREEN_MODELS = [(m, args.workers[i] if i < len(args.workers) else default_w)
                         for i, (m, _) in enumerate(SCREEN_MODELS)]

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    if args.phase == 'summary':
        print_summary(OUTPUT_DIR)
        return

    print('Loading suspect papers...')
    papers = load_suspect_papers()
    if not papers:
        print('No suspect papers found.')
        return
    print(f'{len(papers)} suspect papers loaded.')

    run_taxonomy_reannotation(papers, OUTPUT_DIR, SCREEN_MODELS, force=args.force)
    print_summary(OUTPUT_DIR)


if __name__ == '__main__':
    main()
