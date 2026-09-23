# Paper Collection & Annotation Pipeline

The LLM-assisted pipeline used to build the survey's literature base.

```
Download ALL PDFs ──► Fine screening (full text) ──► Structured annotation ──► Task assignment
(crawl metadata,      (YES / MAYBE / NO)             (domain, topology,
 resolve links,                                        interface, roles, ...)
 download PDFs)                                              │
                                                              ▼
                                                   Taxonomy labeling (+ re-annotation)
```

The pipeline has a single screening gate: every paper whose PDF could be
downloaded goes straight to full-text fine screening — there is no separate
coarse (title + abstract) pass and no separate strict final pass.

## Setup

```bash
pip install -r requirements.txt
```

Scripts that call an LLM expect an OpenAI-compatible chat-completions endpoint.
Edit the configuration constants near the top of each script:

```python
LOCAL_API_URL   = 'http://YOUR_API_HOST:PORT/v1/chat/completions'
LOCAL_API_TOKEN = 'Bearer YOUR_API_KEY'
```

Context requirements: full-text screening and annotation need 64K+ context.
Reasoning-oriented models (e.g. DeepSeek-R1, Qwen3, Kimi-K2.5) give noticeably
better screening quality.

## Stages

| # | Script | What it does | Data source |
|---|--------|--------------|-------------|
| 1 | `1_crawl_papers.py` | Collect metadata and download publisher PDFs | papers.cool, DBLP, Crossref, official proceedings |
| 2 | `2_deep_screen_papers.py` | Fine screening on full text (YES / MAYBE / NO) | LLM API |
| 3 | `3_annotate_papers.py` | Structured annotation (domain, topology, interface, roles, ...) | LLM API |
| 4 | `4_gen_task_assignment.py` | Split annotated papers into per-person reading lists | local |
| · | `filter_non_main_track.py` | Remove demo/workshop/short non-main-track papers | local |
| · | `taxonomy_annotate.py` | Taxonomy labels: interaction pattern, application environment, LLM roles, information flow, feedback, uncertainty, coupling | LLM API |
| · | `taxonomy_reannotate.py` | Re-annotate entries flagged as inconsistent | LLM API |
| · | `annotate_interface_supplement.py` | Fill in missing `interface_type` labels | LLM API |
| · | `annotate_failure_modes_supplement.py` | Failure-mode annotations | LLM API |
| · | `annotate_evaluation_deployment_supplement.py` | Evaluation & deployment annotations | LLM API |

Every script resumes from cached results when re-run (`--phase summary` shows progress
for the staged scripts).

## Downloading

```bash
python 1_crawl_papers.py --phase metadata --source robotics
python 1_crawl_papers.py --phase download --conferences CoRL ICRA IROS RSS --years 2023 2024 2025 --proxy http://127.0.0.1:6890
python 1_crawl_papers.py --phase summary
python 1_crawl_papers.py --help
```

## Adapting to another survey topic

1. Replace the screening prompt in `2_deep_screen_papers.py` with your
   inclusion criteria.
2. Replace the annotation prompt and output schema in `3_annotate_papers.py` and
   `taxonomy_annotate.py` with your taxonomy dimensions.
3. Adjust the venue/year configuration in `1_crawl_papers.py`
   (`PAPERS_COOL_VENUES`, `DBLP_VENUES`, and `venue_sources.ROBOTICS_YEARS`).
