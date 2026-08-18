# Orchestrating LLMs with Specialized Models: A Survey on Heterogeneous Multi-Model Agents

[![Preprint](https://img.shields.io/badge/Preprint-preprints.org-b31b1b)](https://www.preprints.org/manuscript/202607.1041)
[![Project Page](https://img.shields.io/badge/Project-Page-blue)](https://3dagentworld.github.io/awesome_hmma_agent/)
[![Papers](https://img.shields.io/badge/Papers-572-green)](data/papers.json)
[![Venues](https://img.shields.io/badge/Venues-19-orange)](#scope)
[![Years](https://img.shields.io/badge/Years-2023--2026-red)](#scope)

This repository accompanies the survey paper
**“Orchestrating LLMs with Specialized Models: A Survey on Heterogeneous Multi-Model Agents”**.
It releases the full surveyed literature base (572 papers with structured taxonomy labels),
the LLM-assisted paper collection and annotation pipeline used to build it, and a
[project page](https://3dagentworld.github.io/awesome_hmma_agent/) with an interactive paper browser.

## What are HMMAs?

Many real tasks need abilities that a single LLM cannot provide: precise visual grounding,
high-quality image/audio generation, or executable actions in physical and digital
environments. A growing line of work therefore connects LLMs with specialized non-LLM models
(object detectors, segmentation models, diffusion generators, robot policies, ...).
We call this emerging paradigm **Heterogeneous Multi-Model Agents (HMMAs)**.

The survey organizes existing systems into **five interaction patterns** according to the
roles played by perception, generation, and action models:

| Interaction pattern | Papers | Share |
|---|---:|---:|
| LLM + Perception | 214 | 37.4% |
| LLM + Perception + Action | 166 | 29.0% |
| LLM + Perception + Generation | 136 | 23.8% |
| LLM + Generation | 30 | 5.2% |
| LLM + Perception + Generation + Action | 26 | 4.5% |
| **Total** | **572** | 100% |

The complete list of surveyed papers is in [PAPERS.md](PAPERS.md).

## Scope

- 19 major AI venues (NeurIPS, ICLR, ICML, CVPR, ICCV, ECCV, AAAI, IJCAI, ACL,
  EMNLP, NAACL, COLING, COLM, ACM MM, KDD, SIGIR, WWW, MICCAI, INTERSPEECH), 2023–2026
  (2026 covers ICLR 2026 only).

## Repository structure

```
awesome_hmma_agent/
├── PAPERS.md                   # Complete list of the 572 surveyed papers
├── data/                       # Curated literature base and annotations
│   ├── candidates.json         #   591 screening candidates (572 final + 19 filtered)
│   ├── papers.json             #   572 papers: metadata + taxonomy labels
│   ├── annotations_failure_modes.jsonl
│   ├── annotations_evaluation_deployment.jsonl
│   ├── statistics.json         #   Aggregate statistics behind the paper's tables
│   └── README.md               #   Full schema documentation
├── pipeline/                   # LLM-assisted collection & annotation pipeline
│   ├── 1_crawl_papers.py       #   Crawl metadata + download ALL PDFs
│   ├── 2_deep_screen_papers.py #   Fine LLM screening (full text)
│   ├── 3_annotate_papers.py    #   Structured LLM annotation
│   ├── 4_gen_task_assignment.py
│   ├── filter_non_main_track.py
│   ├── taxonomy_annotate.py    #   Taxonomy labeling (pattern / domain / architecture)
│   ├── taxonomy_reannotate.py  #   Re-labeling of suspect annotations
│   ├── annotate_interface_supplement.py
│   ├── annotate_failure_modes_supplement.py
│   ├── annotate_evaluation_deployment_supplement.py
│   ├── requirements.txt
│   └── README.md
└── docs/                       # Project page (GitHub Pages)
```

## Using the pipeline for your own survey

The pipeline is topic-agnostic. To reuse it for another survey topic:

1. Edit the screening prompt in `2_deep_screen_papers.py` (full text).
2. Edit the annotation prompt in `3_annotate_papers.py` to match your taxonomy dimensions.
3. Adjust the venue list in `1_crawl_papers.py` if needed.
4. Point `LOCAL_API_URL` / `LOCAL_API_TOKEN` to any OpenAI-compatible LLM API.

All scripts support resuming after interruption and are safe to re-run.
See [pipeline/README.md](pipeline/README.md) for details.

## Citation

```bibtex
@article{zhang2026hmma,
  title   = {Orchestrating LLMs with Specialized Models: A Survey on Heterogeneous Multi-Model Agents},
  author  = {Zhang, Zheng and Yao, Nanjie and Feng, Yu and Chai, Qi and Liu, Liu and Ye, Deheng and Zhao, Peilin and Zhou, Xiangxin and Wang, Hao and Xiong, Hui},
  journal = {Preprints},
  year    = {2026},
  doi     = {10.20944/preprints202607.1041.v1},
  url     = {https://www.preprints.org/manuscript/202607.1041}
}
```

## License

Code is released under the [MIT License](LICENSE). The curated data is released under
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/).
