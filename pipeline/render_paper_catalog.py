"""Render the paper catalog in the root README from the curated JSON records."""

import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BEGIN = "<!-- BEGIN PAPER CATALOG -->"
END = "<!-- END PAPER CATALOG -->"
PATTERNS = (
    ("LLM + Perception", "Perception", "P"),
    ("LLM + Perception + Action", "Perception and action", "P+A"),
    ("LLM + Perception + Generation", "Perception and generation", "P+G"),
    ("LLM + Generation", "Generation", "G"),
    ("LLM + Perception + Generation + Action", "Perception, generation, and action", "P+G+A"),
)


def load_papers(filename):
    papers = json.loads((ROOT / "data" / filename).read_text(encoding="utf-8"))
    return sorted(papers, key=lambda paper: (
        -paper["year"], paper["conference"].casefold(), paper["title"].casefold()
    ))


def markdown_text(text):
    return re.sub(r"([\\|`*_\[\]<>])", r"\\\1", text)


def paper_table(papers, include_pattern=False):
    patterns = {pattern: short for pattern, _, short in PATTERNS}
    include_code = any(paper.get("code_url") for paper in papers)
    header = ["Paper", "Venue", "Year"]
    if include_pattern:
        header.append("Pattern")
    header.append("Domain")
    if include_code:
        header.append("Code")

    rows = [header, ["---"] * len(header)]
    for paper in papers:
        title = markdown_text(paper["title"])
        cells = [
            f"[{title}]({paper['pdf_url']})",
            paper["conference"],
            str(paper["year"]),
        ]
        if include_pattern:
            cells.append(patterns[paper["collaboration_pattern"]])
        cells.append(markdown_text(paper["application_environment"].replace("_", " ")))
        if include_code:
            code_url = paper.get("code_url")
            cells.append(f"[Code]({code_url})" if code_url else "")
        rows.append(cells)
    return "\n".join("| " + " | ".join(row) + " |" for row in rows)


def main():
    preprint = load_papers("papers.json")
    robotics = load_papers("robotics_papers.json")
    grouped = {pattern: [] for pattern, _, _ in PATTERNS}
    for paper in preprint:
        grouped[paper["collaboration_pattern"]].append(paper)

    sections = [
        "## Papers",
        f"The preprint corpus contains {len(preprint)} papers, grouped by the specialist "
        f"models used alongside the LLM. The robotics extension adds {len(robotics)} "
        "papers from CoRL, ICRA, IROS, and RSS.",
    ]
    for pattern, heading, _ in PATTERNS:
        papers = grouped[pattern]
        sections.extend([f"### {heading}", f"{len(papers)} papers", paper_table(papers)])

    sections.extend([
        "### Robotics extension",
        f"{len(robotics)} papers from CoRL, ICRA, IROS, and RSS. "
        "**P** = Perception, **G** = Generation, **A** = Action.",
        paper_table(robotics, include_pattern=True),
    ])

    readme = ROOT / "README.md"
    current = readme.read_text(encoding="utf-8")
    before, catalog = current.split(BEGIN, 1)
    _, after = catalog.split(END, 1)
    rendered = "\n\n".join(sections)
    readme.write_text(f"{before}{BEGIN}\n\n{rendered}\n\n{END}{after}", encoding="utf-8")


if __name__ == "__main__":
    main()
