# Data

This directory contains the curated literature base and annotations released with the survey
**"Orchestrating LLMs with Specialized Models: A Survey on Heterogeneous Multi-Model Agents"**.

## Files

| File | Records | Description |
|------|---------|-------------|
| `papers.json` | 572 | The final surveyed paper set: bibliographic metadata + taxonomy labels for every paper. |
| `candidates.json` | 591 | All candidate papers identified by the automated screening; `in_final_set` marks the 572 that survived manual verification (19 were filtered out). |
| `robotics_papers.json` | 110 | Additional robotics papers (CoRL/ICRA/IROS/RSS, 2023–2025) screened after the main set, in the same schema as `papers.json`. |
| `robotics_candidates.json` | 118 | Robotics screening candidates; `in_final_set` marks the 110 kept (8 non-learned-action papers were filtered out). |
| `annotations_failure_modes.jsonl` | 572 | Failure-mode annotations per paper (one JSON object per line). |
| `annotations_evaluation_deployment.jsonl` | 572 | Evaluation- and deployment-oriented annotations per paper. |
| `statistics.json` | 1 | Precomputed aggregate statistics used in the paper's tables and figures. |

The two `robotics_*` files are disjoint from the 572-paper main set and are not
counted in `statistics.json`.

## `papers.json` schema

Each entry corresponds to one surveyed paper:

| Field | Description |
|-------|-------------|
| `paper_id` | Identifier from the source platform (papers.cool / DBLP). |
| `title`, `authors`, `conference`, `year`, `abstract` | Bibliographic metadata. |
| `pdf_url` | Open-access URL of the paper. |
| `code_url` | Official open-source repository (GitHub). |
| `source` | Metadata source (`papers_cool` or `dblp`). |
| `collaboration_pattern` | Interaction pattern: `LLM + Perception`, `LLM + Generation`, `LLM + Perception + Generation`, `LLM + Perception + Action`, `LLM + Perception + Generation + Action`. |
| `application_environment` | One of 18 application domains (see the paper, Table "Application domains"). |
| `llm_role_types` | LLM roles: `Planner`, `Coordinator`, `Reasoner`, `Reflector`, `Verifier`, `Translator`, `Router`. |
| `information_flow` | `Sequential`, `Star`, `DAG`, `Iterative`, `Converge`. |
| `interface_type` | `Symbolic`, `Mixed`, `Continuous`. |
| `feedback_structure` | `None`, `Iterative_Refinement`, `Cross_Model_Feedback`, `Self_Correction`, `Human_in_Loop`. |
| `uncertainty_handling` | `None`, `LLM_Verification`, `Confidence_Threshold`, `Retry_Fallback`, `Cascaded_Filtering`, `Voting_Ensemble`, `Self_Correction`. |
| `model_coupling` | `Loose`, `Medium`, `Tight`. |
| `non_llm_models` | Specialized models used, grouped into `perception` / `generation` / `execution`. |
| `llm_models` | LLMs used in the system. |
| `pipeline_summary` | One-paragraph summary of how the models are composed. |

## Supplementary annotations

`annotations_failure_modes.jsonl`: per-paper fields:
`concrete_failure_modes`, `evaluation_gap`, `uncertainty_signal`, `interface_bottleneck`,
`useful_evidence_passage`, `survey_use`.

`annotations_evaluation_deployment.jsonl`: per-paper fields include:
`evaluation_target`, `benchmark_type`, `benchmark_or_dataset_names`, `main_metrics`,
`has_module_ablation` / `has_feedback_ablation` / `has_interface_ablation` (+ details),
`has_failure_analysis`, `deployment_setting`, `human_role`,
`reports_latency_or_cost`, `releases_code_or_data`, `evaluation_summary`, `evaluation_gap`.

All annotation fields were produced by an LLM (Kimi-K2.5) from full paper text and
spot-checked by the authors; see the paper's Survey Protocol section for details.

## Reproducing the numbers

All counts in the paper's tables can be recomputed from `papers.json`, e.g.:

```python
import json, collections
papers = json.load(open('papers.json', encoding='utf-8'))
print(collections.Counter(p['collaboration_pattern'] for p in papers))
```
