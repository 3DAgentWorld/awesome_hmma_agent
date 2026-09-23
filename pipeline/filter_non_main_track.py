#!/usr/bin/env python3
"""
Filter out demo/workshop and other non-main-track papers.

Two filter rules:
  1. *CL venues (ACL/EMNLP/NAACL): filter by the track marker in paper_id
     - Exclude: demo, srw, tutorial
     - Keep: long, short, main, findings, industry
  2. Other venues: filter by PDF page count
     - Exclude: papers of <= 4 pages (usually demo/system descriptions)

Usage:
    python filter_non_main_track.py --dry-run   # preview which papers would be filtered
    python filter_non_main_track.py              # apply the filter (modifies JSONL in place)
    python filter_non_main_track.py --restore    # undo the filter, restoring original labels
"""

import json
import os
import re
import sys
from pathlib import Path
from collections import Counter
from paper_utils import paper_key, pdf_path

try:
    import fitz  # PyMuPDF
except ImportError:
    fitz = None

BASE_DIR = Path(__file__).parent / "papers_data"
DEEP_SCREENING_DIR = BASE_DIR / "deep_screening"
ANNOTATIONS_DIR = BASE_DIR / "annotations"
PDF_DIR = BASE_DIR / "pdfs"
METADATA_DIR = BASE_DIR / 'metadata'

# Track keywords to exclude for *CL venues
EXCLUDED_TRACK_KEYWORDS = ['demo', 'srw', 'tutorial']

# Only *CL venues carry a track marker in paper_id
CL_CONFERENCES = {'ACL', 'EMNLP', 'NAACL'}

# Non-CL venues: page-count threshold (papers with <= this many pages are filtered)
MAX_DEMO_PAGES = 4


def extract_track(paper_id: str) -> str | None:
    """
    Extract the track from a paper_id.
    E.g. '2024.acl-demo.15' -> 'acl-demo'
         '2024.findings-acl.123' -> 'findings-acl'
    """
    m = re.search(r'\d{4}\.([\w-]+)\.', paper_id)
    return m.group(1) if m else None


def is_excluded_track(track: str) -> bool:
    """Whether the track should be excluded (*CL venues only)."""
    return any(kw in track for kw in EXCLUDED_TRACK_KEYWORDS)


def build_pdf_page_cache() -> dict[str, int]:
    """Count pages of non-CL papers by full identity."""
    if fitz is None:
        print("⚠️  PyMuPDF (fitz) not installed; cannot page-count-filter non-CL papers")
        return {}

    cache = {}
    if not PDF_DIR.exists():
        return cache

    for metadata in sorted(METADATA_DIR.glob('*.json')):
        for paper in json.loads(metadata.read_text()):
            if paper['conference'] in CL_CONFERENCES:
                continue
            try:
                with fitz.open(pdf_path(PDF_DIR, paper)) as doc:
                    cache[paper_key(paper)] = doc.page_count
            except Exception:
                pass
    return cache


def should_filter_cl(record: dict, file_type: str) -> tuple[bool, str]:
    """
    Whether a *CL paper should be filtered.
    Returns (should_filter, reason).
    """
    paper_id = record.get('paper_id', '')
    track = extract_track(paper_id)
    if not track or not is_excluded_track(track):
        return False, ''

    if file_type == 'deep_screening':
        label = record.get('label', '').upper()
        if label in ('YES', 'MAYBE'):
            return True, f'non_main_track:{track}'
    elif file_type == 'annotations':
        return True, f'non_main_track:{track}'

    return False, ''


def should_filter_non_cl(record: dict, file_type: str, pdf_pages: dict) -> tuple[bool, str]:
    """
    Whether a non-CL paper should be filtered (by page count).
    Returns (should_filter, reason).
    """
    pages = pdf_pages.get(paper_key(record))

    if pages is None:
        return False, ''  # PDF not found; do not filter

    if pages > MAX_DEMO_PAGES:
        return False, ''

    if file_type == 'deep_screening':
        label = record.get('label', '').upper()
        if label in ('YES', 'MAYBE'):
            return True, f'short_paper:{pages}p'
    elif file_type == 'annotations':
        return True, f'short_paper:{pages}p'

    return False, ''


def filter_jsonl_file(filepath: Path, file_type: str, is_cl: bool,
                      pdf_pages: dict, dry_run: bool = False) -> dict:
    """
    Filter a single JSONL file.
    Returns stats: {reason: count_filtered}.
    """
    filtered_reasons = Counter()
    kept_lines = []

    with open(filepath, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue

            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                kept_lines.append(line)
                continue

            if is_cl:
                do_filter, reason = should_filter_cl(record, file_type)
            else:
                do_filter, reason = should_filter_non_cl(record, file_type, pdf_pages)

            if do_filter:
                filtered_reasons[reason] += 1
                if not dry_run:
                    if file_type == 'deep_screening':
                        record['original_label'] = record['label']
                        record['label'] = 'FILTERED_OUT'
                    elif file_type == 'annotations':
                        record['original_annotation_status'] = record.get('annotation_status', '')
                        record['annotation_status'] = 'FILTERED_OUT'
                    record['filter_reason'] = reason
                    kept_lines.append(json.dumps(record, ensure_ascii=False))
                else:
                    kept_lines.append(line)
            else:
                kept_lines.append(line)

    if not dry_run and filtered_reasons:
        with open(filepath, 'w', encoding='utf-8') as f:
            for line in kept_lines:
                f.write(line + '\n')

    return dict(filtered_reasons)


def restore_all():
    """Restore all filtered papers."""
    for dir_path in [DEEP_SCREENING_DIR, ANNOTATIONS_DIR]:
        if not dir_path.exists():
            continue
        for f in sorted(dir_path.glob('*.jsonl')):
            restored = 0
            lines = []
            with open(f, 'r', encoding='utf-8') as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    record = None
                    try:
                        record = json.loads(line)
                        if record.get('label') == 'FILTERED_OUT' and 'original_label' in record:
                            record['label'] = record.pop('original_label')
                            record.pop('filter_reason', None)
                            restored += 1
                        if record.get('annotation_status') == 'FILTERED_OUT' and 'original_annotation_status' in record:
                            record['annotation_status'] = record.pop('original_annotation_status')
                            record.pop('filter_reason', None)
                            restored += 1
                    except json.JSONDecodeError:
                        pass
                    lines.append(json.dumps(record, ensure_ascii=False) if isinstance(record, dict) else line)

            if restored > 0:
                with open(f, 'w', encoding='utf-8') as fh:
                    for line in lines:
                        fh.write(line + '\n')
                print(f'  Restored {f.name}: {restored} papers')

    print('\n✅ Restore complete')


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Filter out demo/workshop and other non-main-track papers")
    parser.add_argument('--dry-run', action='store_true', help='Preview only, no modifications')
    parser.add_argument('--restore', action='store_true', help='Restore filtered papers (undo the filter)')
    args = parser.parse_args()

    if args.restore:
        restore_all()
        return

    print("Scanning PDF page counts for non-CL papers...")
    pdf_pages = build_pdf_page_cache()
    print(f"Cached page counts for {len(pdf_pages)} PDFs\n")

    total_filtered = Counter()

    print(f"{'=' * 70}")
    print(f"Filter rules:")
    print(f"  1. *CL venues: exclude demo/srw/tutorial tracks (keep industry/findings)")
    print(f"  2. Other venues: exclude papers with PDF <= {MAX_DEMO_PAGES} pages")
    print(f"{'=' * 70}")

    if args.dry_run:
        print("⚠️  DRY RUN mode: preview only, files will not be modified\n")

    for dir_name, dir_path in [
        ("Deep screening results (deep_screening)", DEEP_SCREENING_DIR),
        ("Annotation results (annotations)", ANNOTATIONS_DIR),
    ]:
        if not dir_path.exists():
            print(f"\n{dir_name}: directory does not exist, skipped")
            continue

        print(f"\n--- {dir_name} ---")
        dir_total = Counter()
        file_type = 'annotations' if 'annotation' in dir_path.name else 'deep_screening'

        for f in sorted(dir_path.glob('*.jsonl')):
            # Extract the conference name from the file name
            conf = f.stem.split('_')[-1].split('.')[0]
            is_cl = conf in CL_CONFERENCES

            filtered = filter_jsonl_file(
                f, file_type=file_type, is_cl=is_cl,
                pdf_pages=pdf_pages, dry_run=args.dry_run
            )
            if filtered:
                dir_total.update(filtered)
                n = sum(filtered.values())
                reasons_str = ', '.join(f'{r}:{c}' for r, c in sorted(filtered.items()))
                action = "would filter" if args.dry_run else "filtered"
                print(f"  {f.name}: {action} {n} papers ({reasons_str})")

        if dir_total:
            total_filtered.update(dir_total)
            print(f"  Subtotal: {sum(dir_total.values())} papers")
        else:
            print(f"  Nothing to filter")

    print(f"\n{'=' * 70}")
    print(f"Total: {sum(total_filtered.values())} papers filtered")
    print(f"Breakdown by reason:")
    for reason, count in total_filtered.most_common():
        print(f"  {reason}: {count}")

    if args.dry_run:
        print(f"\n💡 If this looks correct, re-run without --dry-run to apply the filter")
    else:
        print(f"\n✅ Filtering complete!")
        print(f"   Deep screening: label → FILTERED_OUT (original saved in original_label)")
        print(f"   Annotations: annotation_status → FILTERED_OUT (original saved in original_annotation_status)")
        print(f"   Use --restore to undo")
        print(f"\n⚠️  Please re-run the task assignment script (gen_task_assignment_v2.py) to update the assignment tables")


if __name__ == '__main__':
    main()
