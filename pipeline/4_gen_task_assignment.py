#!/usr/bin/env python3
"""
Three-person task assignment based on annotation results (v2).

Loads papers_data/annotations/annotate_*.jsonl and splits papers among three
writers by application_domain, emitting per-person TSV work lists.
"""

import json, glob, os, csv, sys
from collections import defaultdict, Counter

def main():
    papers = []
    for f in sorted(glob.glob('papers_data/annotations/annotate_*.jsonl')):
        for line in open(f):
            if line.strip():
                papers.append(json.loads(line))

    print(f"Total annotated papers: {len(papers)}")

    # ====== Three-person assignment (exact split by application_domain) ======
    #
    # Person A (Understanding+Reasoning): writes Ch1 + Ch2 + Ch3 + Ch5 understanding sub-chapters
    #   Visual_QA(179) + Multimodal_Reasoning(79) + Document_Understanding(40)
    #   + Video_Understanding(90) + Benchmark(48) = 436
    #
    # Person B (Embodied+Interactive environments): writes Ch5 interactive-environment sub-chapters + Ch7 + Ch8 + Ch9
    #   Embodied(210) + Robotics_Manipulation(41) + Autonomous_Driving(33)
    #   + GUI_Agent(40) + Gaming(24) + Scientific(41) + Medical(48) = 437
    #
    # Person C (Generation+Systems): writes Ch5 generation sub-chapters + Ch4 + Ch6
    #   Generation(260) + Audio_Speech(54) + Retrieval(41) + Code_Agent(13) + Other(55) = 423

    person_a_domains = {'Visual_QA', 'Multimodal_Reasoning', 'Document_Understanding', 'Video_Understanding', 'Benchmark'}
    person_b_domains = {'Embodied', 'Robotics_Manipulation', 'Autonomous_Driving', 'GUI_Agent', 'Gaming', 'Scientific', 'Medical'}
    person_c_domains = {'Generation', 'Audio_Speech', 'Retrieval', 'Code_Agent', 'Other'}

    pa, pb, pc = [], [], []
    for p in papers:
        domain = p.get('application_domain', 'Other')
        if domain in person_a_domains:
            pa.append(p)
        elif domain in person_b_domains:
            pb.append(p)
        else:
            pc.append(p)

    for name, plist, domains, chapters in [
        ('A (Understanding+Reasoning)', pa, person_a_domains,
         'Ch1 Introduction + Ch2 Concept & Scope + Ch3 Architecture & Interface + Ch5: Visual_QA / Video / Document / MultimodalReasoning / Benchmark'),
        ('B (Embodied+Interactive)', pb, person_b_domains,
         'Ch5: Embodied / Robotics / Driving / GUI / Gaming / Scientific / Medical + Ch7 Discussion + Ch8 Future + Ch9 Conclusion'),
        ('C (Generation+System)', pc, person_c_domains,
         'Ch5: Generation / Audio / Retrieval / Code + Ch4 System Dynamics + Ch6 Training Paradigms'),
    ]:
        dc = Counter(p['application_domain'] for p in plist)
        yes_c = len([p for p in plist if p['screening_label'] == 'YES'])
        maybe_c = len([p for p in plist if p['screening_label'] == 'MAYBE'])
        print(f"\n{'='*60}")
        print(f"Person {name}: {len(plist)} papers (YES={yes_c}, MAYBE={maybe_c})")
        print(f"Writing chapters: {chapters}")
        print(f"{'='*60}")
        for d, c in dc.most_common():
            yes_d = len([p for p in plist if p['application_domain'] == d and p['screening_label'] == 'YES'])
            maybe_d = c - yes_d
            print(f"  {d}: {c} (YES={yes_d}, MAYBE={maybe_d})")

        tc = Counter(p['topology'] for p in plist)
        print(f"\n  Topology distribution:")
        for t, c in tc.most_common():
            print(f"    {t}: {c} ({c/len(plist)*100:.1f}%)")

        ic = Counter(p['interface_type'] for p in plist)
        print(f"\n  Interface distribution:")
        for i, c in ic.most_common():
            print(f"    {i}: {c} ({c/len(plist)*100:.1f}%)")

        trc = Counter(p['training_paradigm'] for p in plist)
        print(f"\n  Training distribution:")
        for t, c in trc.most_common():
            print(f"    {t}: {c} ({c/len(plist)*100:.1f}%)")

        ec = Counter(p['has_error_correction'] for p in plist)
        print(f"\n  Error correction: True={ec.get(True,0)}, False={ec.get(False,0)}")

    # Generate TSV files
    outdir = 'papers_data/task_assignment_v2'
    os.makedirs(outdir, exist_ok=True)

    header = [
        'paper_id', 'screening_label', 'conference', 'year', 'title',
        'application_domain', 'application_domain_secondary',
        'topology', 'topology_details',
        'interface_type', 'interface_details',
        'training_paradigm', 'training_details',
        'has_error_correction', 'error_correction_details',
        'model_roles', 'brief_pipeline',
        'survey_chapters',
    ]

    for fname, plist in [
        ('PersonA_Understanding', pa),
        ('PersonB_Embodied_Interactive', pb),
        ('PersonC_Generation_System', pc),
    ]:
        # Sort: YES first -> grouped by domain -> year descending -> conference
        plist_sorted = sorted(plist, key=lambda x: (
            0 if x['screening_label'] == 'YES' else 1,
            x.get('application_domain', ''),
            -x.get('year', 0),
            x.get('conference', ''),
        ))

        outfile = os.path.join(outdir, f'{fname}.tsv')
        with open(outfile, 'w', newline='', encoding='utf-8') as fout:
            writer = csv.writer(fout, delimiter='\t')
            writer.writerow(header)
            for p in plist_sorted:
                model_roles = p.get('model_roles', {})
                roles_str = '; '.join(
                    f"{role}: {', '.join(models)}"
                    for role, models in model_roles.items() if models
                )
                chapters_str = '; '.join(p.get('survey_chapters', []))
                writer.writerow([
                    p.get('paper_id', ''),
                    p.get('screening_label', ''),
                    p.get('conference', ''),
                    p.get('year', ''),
                    p.get('title', ''),
                    p.get('application_domain', ''),
                    p.get('application_domain_secondary', '') or '',
                    p.get('topology', ''),
                    p.get('topology_details', '') or '',
                    p.get('interface_type', ''),
                    p.get('interface_details', '') or '',
                    p.get('training_paradigm', ''),
                    p.get('training_details', '') or '',
                    p.get('has_error_correction', False),
                    p.get('error_correction_details', '') or '',
                    roles_str,
                    p.get('brief_pipeline', '') or '',
                    chapters_str,
                ])

        print(f"\n✅ {outfile}: {len(plist_sorted)} papers")

    print(f"\n{'='*60}")
    print(f"Assignment overview: A={len(pa)} + B={len(pb)} + C={len(pc)} = {len(pa)+len(pb)+len(pc)}")
    print(f"✅ All task assignment files generated in {outdir}/")

if __name__ == '__main__':
    main()
