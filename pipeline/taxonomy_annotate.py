#!/usr/bin/env python3
"""
Taxonomy-Oriented Annotation Pipeline

Performs deep, taxonomy-oriented annotation of papers that passed fine
deep screening. Each paper is assigned to exactly one leaf node of a tree-shaped
taxonomy: Level 1 is the Collaboration Pattern (how the LLM collaborates with
heterogeneous models), Level 2 is the Application Environment. Orthogonal
dimensions (LLM role, feedback structure, information flow, uncertainty
handling, model coupling) are also labeled for cross-cutting analysis.

Inputs: papers_data/deep_screening, papers_data/annotations, papers_data/pdfs,
papers_data/metadata.
Outputs: papers_data/taxonomy/taxonomy_<venue>.<year>.jsonl.

Features: multiprocess + multithread execution, checkpoint/resume, and graceful
Ctrl+C shutdown with progress saving.
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
OUTPUT_DIR = BASE_DIR / "papers_data" / "taxonomy"

SCREEN_MODELS = [
    ('kimi-k2.5-0313', 90),
    ('kimi-k2.5-0315', 90),
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
    'output_seq_len': 3072,          # taxonomy annotation needs more detailed output
    'max_input_seq_len': 65536,
}

USE_THINKING = True

MAX_RETRIES = 3
RETRY_DELAY = 1

MAX_PRE_REF_PAGES = 13
MAX_TEXT_CHARS = 80000


TAXONOMY_SYSTEM_PROMPT = """You are an expert AI researcher performing TAXONOMY ANNOTATION for a survey on "Heterogeneous Multi-Model Agents (HMMA)".

You will receive the full text of a paper that has been confirmed as a relevant HMMA paper (passed fine deep screening). Your task is to classify it along multiple dimensions designed for a tree-shaped taxonomy figure.

## PRIMARY AXIS: Collaboration Pattern (Level 1 of Taxonomy Tree)
This dimension classifies HOW the LLM collaborates with heterogeneous models. Each paper belongs to EXACTLY ONE category.

### Categories:

**1. LLM + Perception**
LLM + Perception Models ONLY (no generation or execution models).
The non-LLM models extract information from sensory input (images, audio, video, 3D), and the LLM reasons over the extracted information to produce text outputs (answers, analyses, descriptions).
- Typical models: CLIP, BLIP, SAM, GroundingDINO, Whisper, OCR, depth estimator, object detector
- Typical tasks: VQA, visual reasoning, multimodal understanding, document understanding, medical image analysis
- Key indicator: The system's final output is TEXT (answers, descriptions, analyses), not generated media or physical actions

**2. LLM + Perception + Generation**
LLM + Perception Models + Generation Models.
Perception models extract information, the LLM plans/orchestrates, and generation models produce new content (images, audio, video, 3D assets).
- Typical generation models: Stable Diffusion, DALL-E, ControlNet, Flux, TTS, music generators, video generators
- Typical tasks: image editing guided by visual understanding, multimodal content creation, scene generation from understanding
- Key indicator: The pipeline includes both understanding existing content AND generating new content

**3. LLM + Perception + Action**
LLM + Perception Models + Execution/Action Models.
Perception models understand the environment, the LLM plans actions, and execution models carry out physical or digital actions.
- Typical execution models: RL policies (PPO, SAC), robot controllers (RT-2, diffusion policy), game agents (VPT), browser automation, code executors
- Typical tasks: embodied navigation, robot manipulation, game playing, GUI automation, autonomous driving
- Key indicator: The system produces ACTIONS that modify an environment state (physical or digital)

**4. LLM + Perception + Generation + Action**
LLM + Perception + Generation + Action (all three types of non-LLM models).
The most complex systems that understand, create, and act.
- Typical tasks: creative game agents that perceive, plan, generate assets AND execute actions; scientific discovery systems that analyze, generate hypotheses, AND run experiments
- Key indicator: Must have models from ALL three categories (perception, generation, action)

**5. LLM + Generation**
LLM + Generation Models ONLY (no independent perception models).
The LLM receives text input and directly orchestrates generation models to produce content.
- Typical tasks: text-to-image/video/3D/audio generation pipelines, layout-guided generation, multi-step creative generation
- Key indicator: Input is primarily text (not multimodal), output is generated media. No separate perception models are used.

**6. LLM as Orchestrator**
LLM primarily coordinates models through high-level API/tool calls, where the "models" are accessed as services rather than being tightly integrated components. This is the most loosely-coupled pattern.
- Typical tasks: model marketplace orchestration (HuggingGPT-style), benchmark evaluation systems, model selection/routing
- Key indicator: The LLM's main role is selecting and dispatching to models, with minimal custom integration

## SECONDARY AXIS: Application Environment (Level 2 of Taxonomy Tree)
Within each Collaboration Pattern, classify the APPLICATION ENVIRONMENT. Choose ONE:

- **Embodied_Navigation**: Navigation in 3D environments (Habitat, AI2-THOR, R2R, REVERIE)
- **Robot_Manipulation**: Physical robot grasping, manipulation, assembly
- **Autonomous_Driving**: Self-driving cars, traffic scene understanding with action
- **GUI_Web_Agent**: Web browsing, desktop/mobile UI automation
- **Gaming**: Game playing (Minecraft, Atari, etc.) with visual input
- **Image_Generation**: Image creation, editing, inpainting, style transfer pipelines
- **Video_Generation**: Video creation, editing, generation pipelines
- **3D_Generation**: 3D asset/scene generation
- **Audio_Speech**: Speech/audio processing, TTS, music, ASR-driven systems
- **Visual_QA_Reasoning**: VQA, visual grounding, visual reasoning, chart understanding
- **Document_Understanding**: OCR-based document parsing, table extraction, form understanding
- **Medical_Analysis**: Medical imaging, clinical reasoning, pathology
- **Scientific_Discovery**: Molecule design, materials science, scientific simulation
- **Retrieval_Augmented**: Multimodal retrieval, RAG with vision/audio models
- **Code_Development**: Code generation/debugging with neural model tools
- **Benchmark_Evaluation**: Primarily evaluates/benchmarks multi-model systems
- **Multi_Domain**: Genuinely spans multiple domains as its core contribution
- **Other**: Does not fit any above

## ORTHOGONAL DIMENSIONS (not part of tree, for cross-cutting analysis):

### LLM Role Types (select ALL that apply):
- **Planner**: Decomposes tasks into sub-tasks, creates execution plans
- **Reasoner**: Performs complex reasoning over multi-model outputs
- **Router**: Dynamically selects which model(s) to invoke
- **Verifier**: Checks/validates outputs from other models
- **Coordinator**: Manages information flow between models
- **Reflector**: Self-reflects on results and adjusts strategy
- **Translator**: Converts between modalities (e.g., text descriptions of visual content for downstream models)

### Feedback Structure:
- **None**: Pure feedforward, no feedback loops
- **Self_Correction**: LLM checks its own output and retries
- **Cross_Model_Feedback**: Output of one model feeds back to improve another model's input
- **Iterative_Refinement**: Multiple rounds of model interaction to progressively improve results
- **Human_in_Loop**: Human feedback is incorporated in the loop

### Information Flow Pattern:
- **Sequential**: A→B→C linear chain
- **Broadcast**: LLM sends to multiple models simultaneously (fan-out)
- **Converge**: Multiple model outputs are aggregated by LLM (fan-in)
- **Star**: Central LLM hub with bidirectional communication to spoke models
- **Iterative**: Cyclic flow with repeated interactions
- **DAG**: Directed acyclic graph with complex dependencies

### Uncertainty Handling (how does the system deal with errors/uncertainty from non-LLM models?):
- **None**: No explicit uncertainty handling
- **Confidence_Threshold**: Filters outputs based on confidence scores
- **Voting_Ensemble**: Multiple models vote or ensemble for robustness
- **LLM_Verification**: LLM uses common sense/reasoning to verify model outputs
- **Retry_Fallback**: Retries with different parameters or falls back to alternative models
- **Cascaded_Filtering**: Progressive filtering through multiple validation stages

### Model Coupling Tightness:
- **Loose**: Models communicate via text/API calls only, easily swappable
- **Medium**: Some shared representations or adapters between models, but models can be replaced
- **Tight**: Joint training, shared embeddings, or hard-wired continuous interfaces

## Output Format (strict JSON):
```json
{
  "collaboration_pattern": "<ONE of: LLM + Perception, LLM + Perception + Generation, LLM + Perception + Action, LLM + Perception + Generation + Action, LLM + Generation, LLM as Orchestrator>",
  "collaboration_justification": "<1-2 sentences: what perception/generation/execution models are used and how>",
  "application_environment": "<ONE of the Level 2 categories listed above>",
  "application_justification": "<1 sentence: what task/environment>",
  "llm_role_types": ["<list of applicable roles from: Planner, Reasoner, Router, Verifier, Coordinator, Reflector, Translator>"],
  "feedback_structure": "<ONE of: None, Self_Correction, Cross_Model_Feedback, Iterative_Refinement, Human_in_Loop>",
  "feedback_details": "<1 sentence describing the feedback mechanism, or null if None>",
  "information_flow": "<ONE of: Sequential, Broadcast, Converge, Star, Iterative, DAG>",
  "uncertainty_handling": "<ONE of: None, Confidence_Threshold, Voting_Ensemble, LLM_Verification, Retry_Fallback, Cascaded_Filtering>",
  "uncertainty_details": "<1 sentence, or null if None>",
  "model_coupling": "<ONE of: Loose, Medium, Tight>",
  "non_llm_models": {
    "perception": ["<list of perception model names used>"],
    "generation": ["<list of generation model names used>"],
    "execution": ["<list of execution/action model names used>"]
  },
  "llm_models": ["<list of LLM names used as orchestrator/brain>"],
  "pipeline_summary": "<2-3 sentences: complete data flow from input to output, mentioning all models and their interactions>"
}
```

## Important Guidelines:
1. **collaboration_pattern must be mutually exclusive** — each paper belongs to exactly ONE category. When borderline, look at what the NOVEL CONTRIBUTION of the paper emphasizes.
2. For the non_llm_models field, categorize each model into exactly one role (perception/generation/execution). If a model serves dual purposes, categorize by its PRIMARY use in this paper.
3. Be precise about model names. Use actual names when mentioned (e.g., "CLIP ViT-L/14", "Stable Diffusion v1.5"), use generic descriptions otherwise.
4. For collaboration_pattern classification:
   - If a VLM (e.g., LLaVA, Qwen-VL) is used as the MAIN orchestrator LLM, it counts as the LLM, not as a separate perception model
   - If a VLM is used as a PERCEPTION MODULE feeding into a separate text LLM, it counts as perception
   - Code execution engines and Python interpreters count as execution models
   - Web browsers / GUI automation tools are execution when the system takes actions through them
5. Focus on the paper's PROPOSED SYSTEM, not baselines or comparisons."""


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
    """Call the LLM API (streaming supported)."""
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
        'query_id': 'taxonomy_' + str(uuid.uuid4()),
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


def extract_pre_reference_text(pdf_path: str) -> tuple:
    """Extract PDF text before the References section."""
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
    Load papers labeled YES by fine deep screening and resolve their PDF paths.
    Also load prior annotation data as context.
    """
    keep_papers = {}
    for f in sorted(DEEP_SCREENING_DIR.glob('deep_screen_*.jsonl')):
        with open(f, 'r') as fh:
            for line in fh:
                if line.strip():
                    rec = json.loads(line)
                    if rec.get('label') == 'YES':
                        keep_papers[paper_key(rec)] = rec

    if not keep_papers:
        return []

    prior_annotations = {}
    for f in sorted(ANNOTATION_DIR.glob('annotate_*.jsonl')):
        with open(f, 'r') as fh:
            for line in fh:
                if line.strip():
                    rec = json.loads(line)
                    if rec.get('annotation_status') == 'OK':
                        prior_annotations[paper_key(rec)] = rec

    # Build paper_id -> pdf_path mapping from metadata files
    pdf_map = {}
    for meta_file in sorted(METADATA_DIR.glob('*.json')):
        stem = meta_file.stem
        parts = stem.rsplit('.', 1)
        if len(parts) != 2:
            continue
        venue, year_str = parts
        data = json.loads(meta_file.read_text())
        for p in data:
            p = {**p, 'conference': venue, 'year': int(year_str)}
            path = pdf_path(PDF_DIR, p)
            if valid_pdf(path):
                pdf_map[paper_key(p)] = str(path)

    result = []
    no_pdf = 0
    for pid, screen_rec in keep_papers.items():
        if pid in pdf_map:
            paper = {
                'paper_id': screen_rec['paper_id'],
                'title': screen_rec.get('title', ''),
                'conference': screen_rec.get('conference', ''),
                'year': screen_rec.get('year', 0),
                'pdf_path': pdf_map[pid],
                'key_models': screen_rec.get('key_models', []),
                'prior_annotation': prior_annotations.get(pid, {}),
            }
            result.append(paper)
        else:
            no_pdf += 1

    if no_pdf > 0:
        print(f'  WARNING: {no_pdf} papers have no PDF, skipped')

    return result


def load_completed(output_dir: Path) -> set:
    """Load completed paper_ids for checkpoint resume."""
    completed = set()
    for f in output_dir.glob('taxonomy_*.jsonl'):
        with open(f, 'r') as fh:
            for line in fh:
                if line.strip():
                    rec = json.loads(line)
                    # only successfully parsed records count as completed
                    if rec.get('taxonomy_status') == 'OK':
                        completed.add(paper_key(rec))
    return completed


def build_taxonomy_input(paper: dict, pdf_text: str) -> str:
    """Build the user prompt for taxonomy annotation."""
    title = html.unescape(paper.get('title', ''))
    conference = paper.get('conference', '')
    year = paper.get('year', '')
    key_models = paper.get('key_models', [])

    prior = paper.get('prior_annotation', {})
    prior_domain = prior.get('application_domain', '')
    prior_topology = prior.get('topology', '')
    prior_interface = prior.get('interface_type', '')
    prior_training = prior.get('training_paradigm', '')
    prior_roles = prior.get('model_roles', {})
    prior_pipeline = prior.get('brief_pipeline', '')

    parts = [
        f"**Title**: {title}",
        f"**Venue**: {conference} {year}",
        f"**Key Models Identified**: {', '.join(key_models) if isinstance(key_models, list) else key_models}",
    ]

    # Prior annotations are included as context to help the LLM understand the paper
    if prior_domain or prior_topology:
        parts.append(f"\n**Prior Annotation Context** (from earlier screening, for reference only — your taxonomy labels may differ):")
        if prior_domain:
            parts.append(f"  Domain: {prior_domain}")
        if prior_topology:
            parts.append(f"  Topology: {prior_topology}")
        if prior_interface:
            parts.append(f"  Interface: {prior_interface}")
        if prior_training:
            parts.append(f"  Training: {prior_training}")
        if prior_roles:
            roles_str = '; '.join(f'{k}: {", ".join(v)}' for k, v in prior_roles.items() if v)
            parts.append(f"  Model Roles: {roles_str}")
        if prior_pipeline:
            parts.append(f"  Pipeline: {prior_pipeline}")

    parts.append(f"\n**Full Paper Text (before references)**:\n{pdf_text}")
    parts.append("\nBased on the full paper content above, provide the taxonomy annotation in the JSON format specified.")

    return '\n'.join(parts)


def parse_taxonomy_output(output: str) -> dict:
    """Parse the LLM's taxonomy annotation output (JSON format)."""
    if not output:
        return {'parse_error': 'empty output'}

    output = output.strip()

    # Try to extract JSON from a markdown code block first
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
        parsed = json.loads(json_str)
        return parsed
    except json.JSONDecodeError as e:
        # Attempt to fix common issues (trailing commas)
        fixed = re.sub(r',\s*}', '}', json_str)
        fixed = re.sub(r',\s*]', ']', fixed)
        try:
            parsed = json.loads(fixed)
            return parsed
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


def validate_taxonomy(parsed: dict) -> dict:
    """Validate and normalize taxonomy annotation results."""
    if 'parse_error' in parsed:
        return parsed

    cp = parsed.get('collaboration_pattern', '')
    if cp not in VALID_COLLAB_PATTERNS:
        # fuzzy match against valid values
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
    """Subprocess entry point: process the assigned taxonomy annotation tasks."""
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
    print(f'[PID {os.getpid()}] worker process started: {model_info} → workers={total_workers}, tasks={len(tasks)}')

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
            line = json.dumps(output_record, ensure_ascii=False) + '\n'
            write_buffer.put(line)
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
            # primary axes (tree hierarchy)
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
            'llm_models': annotation.get('llm_models', []),
            'pipeline_summary': annotation.get('pipeline_summary', ''),
        }

        if 'parse_error' in annotation:
            output_record['parse_error'] = annotation['parse_error']
            if 'raw_output' in annotation:
                output_record['raw_output'] = annotation['raw_output']
        for warn_key in ['collaboration_pattern_warning', 'application_environment_warning']:
            if warn_key in annotation:
                output_record[warn_key] = annotation[warn_key]

        line = json.dumps(output_record, ensure_ascii=False) + '\n'
        write_buffer.put(line)

        with count_lock:
            local_success += 1
        with shared_done_counter.get_lock():
            shared_done_counter.value += 1

        status = output_record['taxonomy_status']
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

    # Sliding-window task submission
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
        print(f'[PID {os.getpid()}] interrupted. ok: {local_success}, fail: {local_fail}')
        return

    executor.shutdown(wait=True)
    write_done.set()
    writer.join()

    print(f'[PID {os.getpid()}] done. ok: {local_success}, fail: {local_fail}')


def _merge_tmp_files_to_venue(tmp_files, output_dir):
    """Merge temp files into per venue-year output files."""
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
            fpath = output_dir / f'taxonomy_{venue_key}.jsonl'
            with open(fpath, 'a', encoding='utf-8') as f_out:
                f_out.writelines(lines)
        os.remove(tmp_file)
    if merged > 0:
        print(f'Merged {merged} records into venue-year files')
    return merged


def run_taxonomy_annotation(
    papers: list,
    output_dir: Path,
    models: list,
    force: bool = False,
):
    """Multiprocess taxonomy annotation pipeline."""
    total_workers = sum(w for _, w in models)
    model_info = ', '.join(f'{name}({w})' for name, w in models)

    completed = set()
    if not force:
        completed = load_completed(output_dir)

    pending = [p for p in papers if paper_key(p) not in completed]

    if not pending:
        print('All papers have completed taxonomy annotation!')
        return

    # Split into subprocesses of at most WORKERS_PER_PROCESS workers each
    process_groups = []
    for model_name, total_w in models:
        remaining = total_w
        while remaining > 0:
            chunk = min(remaining, WORKERS_PER_PROCESS)
            process_groups.append([(model_name, chunk)])
            remaining -= chunk

    print(f'\n{"=" * 60}')
    print(f'Taxonomy Annotation (tree-shaped classification)')
    print(f'Models: {model_info} → Total workers: {total_workers}')
    print(f'Thinking: {USE_THINKING}')
    print(f'Workers per process: {WORKERS_PER_PROCESS} → {len(process_groups)} processes')
    print(f'Total KEEP papers: {len(papers)}')
    print(f'Completed: {len(completed)}, Pending: {len(pending)}')
    if force:
        print(f'  WARNING: Force mode: ignoring existing results, re-annotating everything')
    print(f'Output dir: {output_dir}')
    print(f'{"=" * 60}\n')

    if force:
        for f in output_dir.glob('taxonomy_*.jsonl'):
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

    tmp_files = [str(output_dir / f'.tmp_taxo_group{gi}') for gi in range(len(process_groups))]

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
                  shared_parse_fail_counter,
                  shutdown_event),
            name=f'TaxoProc{gi}',
        )
        p.start()
        processes.append(p)

    # Main process: progress bar
    total_tasks = len(pending)
    pbar = tqdm(total=total_tasks, desc=f'Taxonomy ({total_workers}w, {len(processes)}p)')

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
        print('\n\nCtrl+C detected, shutting down all worker processes...')
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
    print(f'Taxonomy annotation complete!')
    print(f'  OK   (parsed successfully): {final_ok}')
    print(f'  ERR  (LLM / parse failure): {final_err}')
    print(f'  PF   (PDF parse failure):   {final_pf}')
    print(f'  Total:                      {final_done}')
    print(f'  Tokens: {final_tokens:,} | Elapsed: {elapsed:.1f}s | Avg: {final_tokens/elapsed:,.0f} t/s')
    print(f'Output dir: {output_dir}')
    print(f'{"=" * 60}')


def print_summary(output_dir: Path):
    """Print a statistical summary of the taxonomy annotations."""
    if not output_dir.exists():
        print("No taxonomy annotation results found.")
        return

    all_records = []
    for f in sorted(output_dir.glob('taxonomy_*.jsonl')):
        with open(f, 'r') as fh:
            for line in fh:
                if line.strip():
                    all_records.append(json.loads(line))

    if not all_records:
        print("No taxonomy annotation records found.")
        return

    ok_records = [r for r in all_records if r.get('taxonomy_status') == 'OK']
    err_records = [r for r in all_records if r.get('taxonomy_status') == 'ERROR']
    pf_records = [r for r in all_records if r.get('taxonomy_status') == 'PDF_PARSE_ERROR']

    print(f'\n{"=" * 80}')
    print(f'Taxonomy Annotation Summary')
    print(f'  Total: {len(all_records)} | OK: {len(ok_records)} | ERR: {len(err_records)} | PDF_FAIL: {len(pf_records)}')
    print(f'{"=" * 80}')

    if not ok_records:
        return

    cp_counter = Counter(r.get('collaboration_pattern', 'Unknown') for r in ok_records)
    print(f'\nLevel 1: Collaboration Pattern (first-level branches of the tree)')
    for cp, cnt in cp_counter.most_common():
        bar = '█' * (cnt // 2)
        pct = cnt / len(ok_records) * 100
        print(f'  {cp:<25} {cnt:>4} ({pct:5.1f}%)  {bar}')

    print(f'\nLevel 2: Application Environment (sub-categories within each L1)')
    cp_env = defaultdict(Counter)
    for r in ok_records:
        cp = r.get('collaboration_pattern', 'Unknown')
        env = r.get('application_environment', 'Unknown')
        cp_env[cp][env] += 1

    for cp in [c for c, _ in cp_counter.most_common()]:
        envs = cp_env[cp]
        print(f'\n  [{cp}] ({sum(envs.values())} papers)')
        for env, cnt in envs.most_common():
            bar = '█' * cnt
            print(f'    {env:<30} {cnt:>3}  {bar}')

    print(f'\n{"─" * 80}')
    print(f'Orthogonal dimension statistics (not in the tree; shown via colors/tables)')

    # LLM Role Types
    role_counter = Counter()
    for r in ok_records:
        for role in r.get('llm_role_types', []):
            role_counter[role] += 1
    print(f'\nLLM Role Types:')
    for role, cnt in role_counter.most_common():
        bar = '█' * (cnt // 3)
        print(f'  {role:<15} {cnt:>4}  {bar}')

    # Feedback Structure
    fb_counter = Counter(r.get('feedback_structure', 'Unknown') for r in ok_records)
    print(f'\nFeedback Structure:')
    for fb, cnt in fb_counter.most_common():
        bar = '█' * (cnt // 3)
        print(f'  {fb:<25} {cnt:>4}  {bar}')

    # Information Flow
    flow_counter = Counter(r.get('information_flow', 'Unknown') for r in ok_records)
    print(f'\nInformation Flow Pattern:')
    for flow, cnt in flow_counter.most_common():
        bar = '█' * (cnt // 3)
        print(f'  {flow:<15} {cnt:>4}  {bar}')

    # Uncertainty Handling
    unc_counter = Counter(r.get('uncertainty_handling', 'Unknown') for r in ok_records)
    print(f'\nUncertainty Handling:')
    for unc, cnt in unc_counter.most_common():
        bar = '█' * (cnt // 3)
        print(f'  {unc:<25} {cnt:>4}  {bar}')

    # Model Coupling
    coupling_counter = Counter(r.get('model_coupling', 'Unknown') for r in ok_records)
    print(f'\nModel Coupling Tightness:')
    for cp, cnt in coupling_counter.most_common():
        bar = '█' * (cnt // 2)
        print(f'  {cp:<10} {cnt:>4}  {bar}')

    print(f'\n{"─" * 80}')
    print(f'Cross analysis: Collaboration Pattern × Feedback Structure')
    cp_fb = defaultdict(Counter)
    for r in ok_records:
        cp = r.get('collaboration_pattern', 'Unknown')
        fb = r.get('feedback_structure', 'Unknown')
        cp_fb[cp][fb] += 1

    all_fbs = sorted(set(fb for d in cp_fb.values() for fb in d.keys()))
    header = f'  {"Pattern":<25}' + ''.join(f'{fb:<20}' for fb in all_fbs)
    print(header)
    for cp in [c for c, _ in cp_counter.most_common()]:
        row = f'  {cp:<25}'
        for fb in all_fbs:
            row += f'{cp_fb[cp].get(fb, 0):<20}'
        print(row)

    print(f'\nCross analysis: Collaboration Pattern × Model Coupling')
    cp_mc = defaultdict(Counter)
    for r in ok_records:
        cp = r.get('collaboration_pattern', 'Unknown')
        mc = r.get('model_coupling', 'Unknown')
        cp_mc[cp][mc] += 1

    all_mcs = sorted(set(mc for d in cp_mc.values() for mc in d.keys()))
    header = f'  {"Pattern":<25}' + ''.join(f'{mc:<12}' for mc in all_mcs)
    print(header)
    for cp in [c for c, _ in cp_counter.most_common()]:
        row = f'  {cp:<25}'
        for mc in all_mcs:
            row += f'{cp_mc[cp].get(mc, 0):<12}'
        print(row)

    print(f'\n{"─" * 80}')
    venue_counter = Counter(f"{r['conference']}.{r['year']}" for r in ok_records)
    print(f'Per-Venue Breakdown:')
    print(f'  {"Venue":<20} {"Count":<8}')
    print(f'  {"-" * 28}')
    for venue, cnt in sorted(venue_counter.items()):
        print(f'  {venue:<20} {cnt:<8}')

    print(f'\n{"=" * 80}')


def main():
    parser = argparse.ArgumentParser(
        description='Taxonomy-oriented annotation for Multi-Model Agent Survey'
    )
    parser.add_argument(
        '--phase', choices=['annotate', 'summary'], default='annotate',
        help='annotate: run annotation | summary: view statistics',
    )
    parser.add_argument(
        '--conferences', '-c', nargs='+', default=None,
        help='only process the given conferences (e.g. NeurIPS ICLR CVPR)',
    )
    parser.add_argument(
        '--years', '-y', nargs='+', type=int, default=None,
        help='only process the given years (e.g. 2023 2024 2025)',
    )
    parser.add_argument(
        '--model', nargs='+', default=None,
        help='model name(s)',
    )
    parser.add_argument(
        '--workers', nargs='+', type=int, default=None,
        help='concurrency per model',
    )
    parser.add_argument(
        '--no-thinking', action='store_true',
        help='disable thinking mode',
    )
    parser.add_argument(
        '--force', action='store_true',
        help='force re-annotation (ignore existing results)',
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
    print('Loading YES papers from deep screening...')
    all_papers = load_papers_to_annotate()

    if not all_papers:
        print('No YES papers found. Please run deep screening (2_deep_screen_papers.py) first.')
        return

    if args.conferences:
        all_papers = [p for p in all_papers if p['conference'] in args.conferences]
    if args.years:
        all_papers = [p for p in all_papers if p['year'] in args.years]

    grouped = defaultdict(list)
    for p in all_papers:
        key = f"{p['conference']}.{p['year']}"
        grouped[key].append(p)

    print(f'Found {len(all_papers)} KEEP papers to annotate')
    print(f'Spread across {len(grouped)} venue-years:')
    for key in sorted(grouped.keys()):
        print(f'  [{key}] {len(grouped[key])}')

    run_taxonomy_annotation(all_papers, OUTPUT_DIR, SCREEN_MODELS, force=args.force)
    print_summary(OUTPUT_DIR)


if __name__ == '__main__':
    main()
