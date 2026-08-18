#!/usr/bin/env python3
"""
Supplementary annotation script for the HMMA survey evaluation section.

Reads papers from papers_data/taxonomy_final, extracts local PDF text before
references, and asks the LLM API to label evaluation- and deployment-focused
fields supporting the ACM survey section "Evaluation and Benchmarking"
(evaluation target, benchmark types, metrics, ablations, failure analysis,
deployment setting, human role, latency/cost, code/data release).

Output: papers_data/evaluation_deployment_supplement.jsonl
Supports resume. Example:
    python annotate_evaluation_deployment_supplement.py --limit 80 --workers 8
"""

import argparse
import html
import json
import random
import re
import threading
import time
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import fitz
import requests
from tqdm import tqdm

BASE_DIR = Path(__file__).parent
TAXONOMY_DIR = BASE_DIR / 'papers_data' / 'taxonomy_final'
METADATA_DIR = BASE_DIR / 'papers_data' / 'metadata'
PDF_DIR = BASE_DIR / 'papers_data' / 'pdfs'
OUTPUT_PATH = BASE_DIR / 'papers_data' / 'evaluation_deployment_supplement.jsonl'

LOCAL_API_URL = 'http://YOUR_API_HOST:PORT/v1/chat/completions'
LOCAL_API_TOKEN = 'Bearer YOUR_API_KEY'
LOCAL_API_WSID = 'YOUR_WSID'
MODEL_NAME = 'kimi-k2.6-0507'

MAX_PRE_REF_PAGES = 13
MAX_TEXT_CHARS = 60000
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

Given a paper text and an existing taxonomy annotation, extract evaluation- and deployment-focused evidence for survey writing. Use simple, factual wording. Do not invent details. If the paper does not state something, use "not stated", false, or [] as appropriate.

Output schema:
{
  "evaluation_target": "one of: component | end_to_end | both | unclear",
  "benchmark_type": ["zero or more of: standard_benchmark | custom_dataset | simulator | real_world | human_study | expert_study | qualitative_examples | unclear"],
  "benchmark_or_dataset_names": ["names of datasets, benchmarks, simulators, or task suites explicitly used"],
  "main_metrics": ["zero or more of: accuracy | success_rate | completion_rate | task_score | human_preference | expert_judgment | quality_score | retrieval_score | localization_score | generation_metric | latency | cost | safety | robustness | ablation_delta | unclear"],
  "has_module_ablation": true or false,
  "module_ablation_details": "short sentence, or not stated",
  "has_feedback_ablation": true or false,
  "feedback_ablation_details": "short sentence, or not stated",
  "has_interface_ablation": true or false,
  "interface_ablation_details": "short sentence, or not stated",
  "has_failure_analysis": true or false,
  "failure_analysis_details": "short sentence, or not stated",
  "deployment_setting": "one of: offline | simulator | web | real_robot | mobile_or_gui | clinical_or_scientific | autonomous_driving | benchmark_only | unclear",
  "human_role": "one of: none | evaluator | in_the_loop | expert_supervisor | user_feedback | unclear",
  "reports_latency_or_cost": true or false,
  "latency_or_cost_details": "short sentence, or not stated",
  "releases_code_or_data": "one of: code | data | both | none | unclear",
  "code_or_data_details": "short sentence, or not stated",
  "evaluation_summary": "1-2 concise sentences summarizing the evaluation design",
  "evaluation_gap": "1 concise sentence about what the evaluation misses from an HMMA perspective",
  "useful_evidence_passage": "one concise passage from the paper text, at most 80 words, useful for survey writing"
}

Annotation guidance:
- component = evaluates individual modules or local capabilities, e.g., detector, segmenter, generator, retriever, policy.
- end_to_end = evaluates final task success or final user-visible output.
- both = reports both component/local results and full-system/task results.
- module ablation = removes/replaces a model, tool, specialist, planner, retriever, verifier, or agent role.
- feedback ablation = compares with/without reflection, retry, self-correction, human feedback, verifier feedback, or closed-loop revision.
- interface ablation = compares different inter-model representations, e.g., text vs structured output, boxes vs masks, visual tokens vs captions, different prompts/adapters.
- failure analysis = explicitly categorizes errors, analyzes failed cases, or reports qualitative failure modes beyond a few cherry-picked examples.
- human_role should capture whether humans evaluate outputs, provide feedback during execution, or supervise high-stakes decisions.
- evaluation_gap should be survey-facing: mention missing trace-level diagnosis, missing ablation, missing cost/latency, weak failure analysis, lack of real deployment, or unclear benchmark comparability.
- Keep all strings short and factual.
- Respond with ONLY the JSON object. No markdown. No extra commentary."""

_REF_PATTERNS = [
    re.compile(r'(?:^|\n)\s*(References|REFERENCES|Bibliography|BIBLIOGRAPHY)\s*(?:\n|$)', re.MULTILINE),
    re.compile(r'(?:^|\n)\s*\d+[\.\s]+(References|REFERENCES)\s*(?:\n|$)', re.MULTILINE),
]

thread_local = threading.local()
write_lock = threading.Lock()
_stats_lock = threading.Lock()
_total_input_tokens = 0
_total_output_tokens = 0
_total_reasoning_tokens = 0
_total_llm_time = 0.0


def get_session():
    if not hasattr(thread_local, 'session'):
        session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(pool_connections=64, pool_maxsize=64, max_retries=3)
        session.mount('http://', adapter)
        session.mount('https://', adapter)
        thread_local.session = session
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
            match = pat.search(text)
            if match:
                ref_idx = pn
                ref_off = match.start()
                break
        if ref_idx is not None:
            break

    chunks = []
    if ref_idx is not None:
        for pn in range(ref_idx + 1):
            page_text = doc[pn].get_text()
            chunks.append(page_text[:ref_off] if pn == ref_idx else page_text)
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
        with fp.open(encoding='utf-8') as fin:
            for line in fin:
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
            data = json.loads(meta_file.read_text(encoding='utf-8'))
        except Exception:
            continue
        for paper in data:
            pid = paper.get('paper_id')
            if pid not in needed:
                continue
            pdf_path = PDF_DIR / venue / year / f"{pid}_{safe_pdf_name(paper.get('title', ''))}.pdf"
            if pdf_path.exists() and pdf_path.stat().st_size > 1000:
                pdf_map[pid] = pdf_path
    return pdf_map


def load_done():
    done = set()
    failed = set()
    if not OUTPUT_PATH.exists():
        return done, failed
    with OUTPUT_PATH.open(encoding='utf-8') as fin:
        for line in fin:
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            paper_id = rec.get('paper_id')
            if not paper_id:
                continue
            if rec.get('error'):
                failed.add(paper_id)
            else:
                done.add(paper_id)
    failed -= done
    return done, failed


def _parse_stream_response(resp) -> dict:
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
        except json.JSONDecodeError:
            continue
        if 'error' in chunk_json:
            return {'content': '', 'reasoning': '', 'usage': {}}
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
        'query_id': 'eval_deploy_' + str(uuid.uuid4()),
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
        started = time.time()
        resp = get_session().post(LOCAL_API_URL, headers=headers, json=body, stream=True, timeout=TIMEOUT)
        if resp.status_code != 200:
            return ''
        result = _parse_stream_response(resp)
        elapsed = time.time() - started
        usage = result.get('usage', {})
        with _stats_lock:
            _total_llm_time += elapsed
            if usage:
                _total_input_tokens += usage.get('prompt_tokens', 0)
                _total_output_tokens += usage.get('completion_tokens', 0)
                _total_reasoning_tokens += (
                    usage.get('reasoning_tokens', 0)
                    or usage.get('completion_tokens_details', {}).get('reasoning_tokens', 0)
                )
        return result.get('content', '') or ''
    except Exception:
        return ''


def parse_json(text: str):
    if not text:
        return None
    text = text.strip()
    text = re.sub(r'^```(?:json)?\s*', '', text)
    text = re.sub(r'\s*```$', '', text)
    match = re.search(r'\{.*\}', text, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except Exception:
        fixed = re.sub(r',\s*}', '}', match.group(0))
        fixed = re.sub(r',\s*]', ']', fixed)
        try:
            return json.loads(fixed)
        except Exception:
            return None


def normalize_annotation(parsed: dict) -> dict:
    valid_eval_targets = {'component', 'end_to_end', 'both', 'unclear'}
    valid_benchmark_types = {
        'standard_benchmark', 'custom_dataset', 'simulator', 'real_world',
        'human_study', 'expert_study', 'qualitative_examples', 'unclear'
    }
    valid_metrics = {
        'accuracy', 'success_rate', 'completion_rate', 'task_score', 'human_preference',
        'expert_judgment', 'quality_score', 'retrieval_score', 'localization_score',
        'generation_metric', 'latency', 'cost', 'safety', 'robustness', 'ablation_delta', 'unclear'
    }
    valid_deploy = {
        'offline', 'simulator', 'web', 'real_robot', 'mobile_or_gui',
        'clinical_or_scientific', 'autonomous_driving', 'benchmark_only', 'unclear'
    }
    valid_human = {'none', 'evaluator', 'in_the_loop', 'expert_supervisor', 'user_feedback', 'unclear'}
    valid_release = {'code', 'data', 'both', 'none', 'unclear'}

    def one_of(value, valid, default):
        value = str(value or '').strip()
        return value if value in valid else default

    def list_of(value, valid=None):
        if value is None:
            return []
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list):
            return []
        out = []
        for item in value:
            item = str(item).strip()
            if not item:
                continue
            if valid is None or item in valid:
                out.append(item)
        return out

    def bool_value(value):
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {'true', 'yes', '1'}
        return bool(value)

    normalized = {
        'evaluation_target': one_of(parsed.get('evaluation_target'), valid_eval_targets, 'unclear'),
        'benchmark_type': list_of(parsed.get('benchmark_type'), valid_benchmark_types),
        'benchmark_or_dataset_names': list_of(parsed.get('benchmark_or_dataset_names')),
        'main_metrics': list_of(parsed.get('main_metrics'), valid_metrics),
        'has_module_ablation': bool_value(parsed.get('has_module_ablation')),
        'module_ablation_details': str(parsed.get('module_ablation_details') or 'not stated').strip(),
        'has_feedback_ablation': bool_value(parsed.get('has_feedback_ablation')),
        'feedback_ablation_details': str(parsed.get('feedback_ablation_details') or 'not stated').strip(),
        'has_interface_ablation': bool_value(parsed.get('has_interface_ablation')),
        'interface_ablation_details': str(parsed.get('interface_ablation_details') or 'not stated').strip(),
        'has_failure_analysis': bool_value(parsed.get('has_failure_analysis')),
        'failure_analysis_details': str(parsed.get('failure_analysis_details') or 'not stated').strip(),
        'deployment_setting': one_of(parsed.get('deployment_setting'), valid_deploy, 'unclear'),
        'human_role': one_of(parsed.get('human_role'), valid_human, 'unclear'),
        'reports_latency_or_cost': bool_value(parsed.get('reports_latency_or_cost')),
        'latency_or_cost_details': str(parsed.get('latency_or_cost_details') or 'not stated').strip(),
        'releases_code_or_data': one_of(parsed.get('releases_code_or_data'), valid_release, 'unclear'),
        'code_or_data_details': str(parsed.get('code_or_data_details') or 'not stated').strip(),
        'evaluation_summary': str(parsed.get('evaluation_summary') or '').strip(),
        'evaluation_gap': str(parsed.get('evaluation_gap') or '').strip(),
        'useful_evidence_passage': str(parsed.get('useful_evidence_passage') or '').strip(),
    }
    if not normalized['benchmark_type']:
        normalized['benchmark_type'] = ['unclear']
    if not normalized['main_metrics']:
        normalized['main_metrics'] = ['unclear']
    return normalized


def build_user_prompt(rec: dict, pdf_text: str) -> str:
    compact_annotation = {
        'collaboration_pattern': rec.get('collaboration_pattern'),
        'application_environment': rec.get('application_environment'),
        'feedback_structure': rec.get('feedback_structure'),
        'uncertainty_handling': rec.get('uncertainty_handling'),
        'model_coupling': rec.get('model_coupling'),
        'non_llm_models': rec.get('non_llm_models'),
        'deterministic_tools_used': rec.get('deterministic_tools_used'),
        'pipeline_summary': rec.get('pipeline_summary'),
    }
    return (
        f"Title: {rec.get('title', '')}\n"
        f"Venue: {rec.get('conference', '')} {rec.get('year', '')}\n"
        f"Existing taxonomy annotation:\n{json.dumps(compact_annotation, ensure_ascii=False, indent=2)}\n\n"
        f"Paper text before references:\n{pdf_text}"
    )


def process_one(rec: dict, pdf_path: Path) -> dict:
    text = extract_pre_ref_text(pdf_path)
    base = {
        'paper_id': rec['paper_id'],
        'title': rec.get('title', ''),
        'conference': rec.get('conference', ''),
        'year': rec.get('year', ''),
        'application_environment': rec.get('application_environment', ''),
        'collaboration_pattern': rec.get('collaboration_pattern', ''),
    }
    if len(text) < 500:
        return {**base, 'error': 'pdf_text_too_short'}

    user_content = build_user_prompt(rec, text)
    for _ in range(3):
        output = call_llm(user_content)
        parsed = parse_json(output)
        if parsed:
            return {**base, **normalize_annotation(parsed)}
        time.sleep(1 + random.random())
    return {**base, 'error': 'llm_failed'}


def apply_filters(records, args):
    if args.domains:
        wanted = set(args.domains)
        records = [r for r in records if r.get('application_environment') in wanted]
    if args.patterns:
        wanted = set(args.patterns)
        records = [r for r in records if r.get('collaboration_pattern') in wanted]
    if args.conferences:
        wanted = set(args.conferences)
        records = [r for r in records if r.get('conference') in wanted]
    if args.years:
        wanted = set(args.years)
        records = [r for r in records if r.get('year') in wanted]
    return records


def print_summary():
    if not OUTPUT_PATH.exists():
        print('[summary] no output file found')
        return
    records = []
    with OUTPUT_PATH.open(encoding='utf-8') as fin:
        for line in fin:
            if line.strip():
                try:
                    records.append(json.loads(line))
                except Exception:
                    pass
    ok = [r for r in records if not r.get('error')]
    err = [r for r in records if r.get('error')]
    print(f'[summary] total={len(records)} ok={len(ok)} err={len(err)}')
    if not ok:
        return

    def count_list(field):
        counter = Counter()
        for rec in ok:
            values = rec.get(field, [])
            if isinstance(values, str):
                values = [values]
            for value in values:
                counter[value] += 1
        return counter

    for field in ['evaluation_target', 'deployment_setting', 'human_role', 'releases_code_or_data']:
        print(f'\n[{field}]')
        for key, val in Counter(r.get(field, 'unclear') for r in ok).most_common():
            print(f'  {key:<28} {val}')

    for field in ['benchmark_type', 'main_metrics']:
        print(f'\n[{field}]')
        for key, val in count_list(field).most_common():
            print(f'  {key:<28} {val}')

    bool_fields = [
        'has_module_ablation', 'has_feedback_ablation', 'has_interface_ablation',
        'has_failure_analysis', 'reports_latency_or_cost'
    ]
    print('\n[boolean fields]')
    for field in bool_fields:
        counter = Counter(bool(r.get(field)) for r in ok)
        print(f'  {field:<28} true={counter[True]} false={counter[False]}')


def main():
    parser = argparse.ArgumentParser(description='Evaluation/deployment supplement annotation for HMMA survey')
    parser.add_argument('--limit', type=int, default=0, help='maximum number of pending papers to annotate')
    parser.add_argument('--workers', type=int, default=20)
    parser.add_argument('--domains', nargs='*', default=None, help='optional application_environment filter')
    parser.add_argument('--patterns', nargs='*', default=None, help='optional collaboration_pattern filter')
    parser.add_argument('--conferences', nargs='*', default=None, help='optional conference filter, e.g. CVPR ICLR')
    parser.add_argument('--years', nargs='*', type=int, default=None, help='optional year filter, e.g. 2024 2025')
    parser.add_argument('--summary', action='store_true', help='print summary of existing output and exit')
    parser.add_argument('--retry-failed', action='store_true', help='retry records that previously ended with error')
    args = parser.parse_args()

    if args.summary:
        print_summary()
        return

    records = apply_filters(load_taxonomy_records(), args)
    done, failed = load_done()
    pdf_map = build_pdf_map(records)

    skip = done if not args.retry_failed else done - failed
    pending = [r for r in records if r['paper_id'] not in skip and r['paper_id'] in pdf_map]
    if args.limit and args.limit > 0:
        pending = pending[:args.limit]

    print(f'[info] taxonomy records after filters: {len(records)}')
    print(f'[info] pdf resolved: {len(pdf_map)}')
    print(f'[info] already done successfully: {len(done)}')
    print(f'[info] previous failed: {len(failed)}')
    print(f'[info] pending this run: {len(pending)}')
    if not pending:
        print_summary()
        return

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    run_start = time.time()
    completed_count = 0
    error_count = 0

    with OUTPUT_PATH.open('a', encoding='utf-8') as fout:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(process_one, rec, pdf_map[rec['paper_id']]): rec for rec in pending}
            pbar = tqdm(as_completed(futures), total=len(futures), desc='evaluation/deployment')
            for fut in pbar:
                try:
                    out_rec = fut.result()
                except Exception as exc:
                    source = futures[fut]
                    out_rec = {
                        'paper_id': source['paper_id'],
                        'title': source.get('title', ''),
                        'conference': source.get('conference', ''),
                        'year': source.get('year', ''),
                        'error': str(exc),
                    }
                with write_lock:
                    fout.write(json.dumps(out_rec, ensure_ascii=False) + '\n')
                    fout.flush()
                completed_count += 1
                if out_rec.get('error'):
                    error_count += 1
                with _stats_lock:
                    out_tokens = _total_output_tokens + _total_reasoning_tokens
                    token_speed = out_tokens / _total_llm_time if _total_llm_time > 0 else 0
                pbar.set_postfix({
                    'ok': completed_count - error_count,
                    'err': error_count,
                    'tok/s': f'{token_speed:.1f}',
                    'out_tok': out_tokens,
                }, refresh=True)
            pbar.close()

    total_elapsed = time.time() - run_start
    print(f'\n[done] {OUTPUT_PATH}')
    print(f'[stats] completed: {completed_count}, errors: {error_count}')
    print(f'[stats] total time: {total_elapsed:.1f}s')
    print(f'[stats] input tokens: {_total_input_tokens:,}, output tokens: {_total_output_tokens:,}, reasoning tokens: {_total_reasoning_tokens:,}')
    total_output = _total_output_tokens + _total_reasoning_tokens
    avg_speed = total_output / _total_llm_time if _total_llm_time > 0 else 0
    print(f'[stats] avg output speed: {avg_speed:.1f} tok/s across {args.workers} workers')
    print_summary()


if __name__ == '__main__':
    main()
