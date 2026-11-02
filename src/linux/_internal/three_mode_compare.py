"""Auditable comparison of the three independent outgoing-damage pipelines.

This reports disagreement. It never copies damage between pipelines or edits
any session evidence to make the results appear closer.
"""
from __future__ import annotations

import csv
import difflib
import json
from pathlib import Path


def read_events(path, kind):
    path = Path(path)
    if not path.is_file():
        return None
    with path.open('r', encoding='utf-8-sig', newline='') as f:
        rows = list(csv.DictReader(f))
    result = []
    for r in rows:
        if kind == 'parser':
            if r.get('event') != 'damage_dealt':
                continue
            value = r.get('amount', '')
            at = r.get('elapsed_seconds', '')
            line = r.get('text', '')
        else:
            value = r.get('final_damage', '')
            at = r.get('event_capture_elapsed_seconds', '')
            line = r.get('batch_ocr_text', '')
        try:
            amount = int(value)
        except (ValueError, TypeError):
            continue
        result.append({'amount': amount, 'at': at, 'text': line})
    return result


def differences(reference, other, name):
    """Align chronological damage amounts without requiring identical timestamps."""
    sm = difflib.SequenceMatcher(a=[e['amount'] for e in reference],
                                 b=[e['amount'] for e in other], autojunk=False)
    mismatches = []
    for op, a0, a1, b0, b1 in sm.get_opcodes():
        if op == 'equal':
            continue
        size = max(a1-a0, b1-b0)
        for j in range(size):
            refidx = a0+j if a0+j < a1 else None
            otheridx = b0+j if b0+j < b1 else None
            ref = reference[refidx] if refidx is not None else None
            candidate = other[otheridx] if otheridx is not None else None
            mismatches.append({
                'comparison':name,
                'difference_type':op,
                'post_index':refidx+1 if refidx is not None else '',
                'post_elapsed':ref['at'] if ref else '',
                'post_damage':ref['amount'] if ref else '',
                'post_ocr':ref['text'] if ref else '',
                'other_index':otheridx+1 if otheridx is not None else '',
                'other_elapsed':candidate['at'] if candidate else '',
                'other_damage':candidate['amount'] if candidate else '',
                'other_ocr':candidate['text'] if candidate else '',
            })
    return mismatches


def read_attacks(path):
    """Mode-specific raw attack/confirmation records, NOT the last GUI report."""
    path = Path(path)
    if not path.is_file():
        return None
    with path.open('r', encoding='utf-8-sig', newline='') as f:
        return list(csv.DictReader(f))


def attack_signature(r):
    return (r.get('event',''), r.get('target',''), r.get('roll',''),
            r.get('bonus',''), r.get('outcome',''), r.get('confirmed',''))


def attack_differences(reference, other, name):
    left = [attack_signature(x) for x in reference]
    right = [attack_signature(x) for x in other]
    sm = difflib.SequenceMatcher(a=left,b=right,autojunk=False)
    result = []
    for op, a0, a1, b0, b1 in sm.get_opcodes():
        if op == 'equal':
            continue
        for j in range(max(a1-a0,b1-b0)):
            ai = a0+j if a0+j<a1 else None
            bi = b0+j if b0+j<b1 else None
            a = reference[ai] if ai is not None else None
            b = other[bi] if bi is not None else None
            result.append({
                'comparison': name, 'difference_type':op,
                'post_index':ai+1 if ai is not None else '',
                'post_source_line':a.get('line','') if a else '',
                'post_event':str(attack_signature(a)) if a else '',
                'other_index':bi+1 if bi is not None else '',
                'other_source_line':b.get('line','') if b else '',
                'other_event':str(attack_signature(b)) if b else '',
            })
    return result


def attack_comparison(session):
    names = {
        'original_live': 'fast_live_attack_events.csv',
        'replay_live': 'replay_live_attack_events.csv',
        'ffv1_reanalyze': 'ffv1_post_attack_events.csv',
    }
    data = {key:read_attacks(session / path) for key,path in names.items()}
    results = {}
    for name,rows in data.items():
        if rows is None:
            results[name]={'available':False,'source_file':None}
            continue
        results[name] = {
            'available':True, 'source_file':names[name],
            'attacks':sum(r.get('event')=='attack' for r in rows),
            'critical_threats':sum(r.get('event')=='attack' and
                                   r.get('outcome')=='critical_threat' for r in rows),
            'confirm_attempts':sum(r.get('event')=='confirm' for r in rows),
            'confirmed_crits':sum(r.get('event')=='confirm' and
                                  r.get('outcome')=='confirmed' for r in rows),
            'failed_confirms':sum(r.get('event')=='confirm' and
                                  r.get('outcome')=='failed' for r in rows),
        }
    available = all(x is not None for x in data.values())
    differences = []
    if available:
        ref = data['ffv1_reanalyze']
        for name in ('original_live','replay_live'):
            differences.extend(attack_differences(ref,data[name],name))
    details = {
        'modes':results,
        'all_three_attack_sequences_equal':
            (all([attack_signature(x) for x in data[n]] ==
                 [attack_signature(x) for x in data['ffv1_reanalyze']]
                 for n in ('original_live','replay_live')) if available else None),
        'unmatched_attack_or_confirm_rows':len(differences) if available else None,
    }
    columns=['comparison','difference_type','post_index','post_source_line',
             'post_event','other_index','other_source_line','other_event']
    with (session/'three_mode_attack_mismatches.csv').open('w',encoding='utf-8',newline='') as f:
        w=csv.DictWriter(f,fieldnames=columns)
        w.writeheader()
        w.writerows(differences)
    return details


def generate(session):
    session = Path(session)
    sources = {
        'original_live': ('fast_live_damage_events.csv', 'parser'),
        'replay_live': ('replay_live_damage_events.csv', 'replay'),
        'ffv1_reanalyze': ('ffv1_post_damage_events.csv', 'parser'),
    }
    # Compatibility with older recordings that used the other filenames.
    alternative = {
        'original_live': ('live_damage_events.csv', 'replay'),
        'ffv1_reanalyze': ('damage_events.csv', 'parser'),
    }
    records = {}
    for name, (filename, kind) in sources.items():
        data = read_events(session / filename, kind)
        if data is None and name in alternative:
            filename, kind = alternative[name]
            data = read_events(session / filename, kind)
        records[name] = {'file':filename if data is not None else None,
                         'events':data}

    summary = {
        'purpose':'Independent verification: no totals are replaced or normalized.',
        'modes':{}, 'all_three_totals_equal':None,
        'all_three_damage_sequences_equal':None,
        'missing_files':[],
    }
    for name, info in records.items():
        data = info['events']
        if data is None:
            summary['missing_files'].append(sources[name][0])
            summary['modes'][name] = {'available':False, 'source_file':None}
        else:
            summary['modes'][name] = {
                'available':True, 'source_file':info['file'],
                'damage_total':sum(v['amount'] for v in data),
                'damage_events':len(data),
            }

    all_available = all(info['events'] is not None for info in records.values())
    diff_rows = []
    if all_available:
        base = records['ffv1_reanalyze']['events']
        for name in ('original_live', 'replay_live'):
            diff_rows.extend(differences(base, records[name]['events'], name))
        totals = [summary['modes'][name]['damage_total'] for name in sources]
        summary['all_three_totals_equal'] = len(set(totals)) == 1
        amounts = [[ev['amount'] for ev in records[name]['events']] for name in sources]
        summary['all_three_damage_sequences_equal'] = amounts[0] == amounts[1] == amounts[2]
        summary['original_live_minus_post'] = totals[0]-totals[2]
        summary['replay_live_minus_post'] = totals[1]-totals[2]
        summary['unmatched_event_rows'] = len(diff_rows)

    summary['attack_and_crit'] = attack_comparison(session)

    (session / 'three_mode_comparison.json').write_text(
        json.dumps(summary,indent=2,ensure_ascii=True),encoding='utf-8')

    columns = ['comparison','difference_type','post_index','post_elapsed',
               'post_damage','post_ocr','other_index','other_elapsed',
               'other_damage','other_ocr']
    with (session / 'three_mode_mismatches.csv').open('w',encoding='utf-8',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=columns)
        writer.writeheader()
        writer.writerows(diff_rows)

    lines=['DDO Combat Analyzer - THREE INDEPENDENT DAMAGE MODES',
           '===================================================',
           'Damage is not copied between modes. These are the observed results.', '']
    for name in ('original_live','replay_live','ffv1_reanalyze'):
        info=summary['modes'][name]
        if info['available']:
            lines.append(f"{name:17s} {info['damage_total']:>14,d} damage   "
                         f"{info['damage_events']:>6,d} outgoing damage events")
        else:
            lines.append(f'{name:17s} unavailable (missing source file)')
    lines.extend(['', f"Totals identical: {summary['all_three_totals_equal']}",
                  f"Event order and amounts identical: {summary['all_three_damage_sequences_equal']}",
                  f"Unmatched event rows: {len(diff_rows)}", '',
                  'Differences by event: three_mode_mismatches.csv',
                  '',
                  'ATTACK/CRIT VERIFICATION (independent per-mode files)',
                  '----------------------------------------------------'])
    for mode in ('original_live','replay_live','ffv1_reanalyze'):
        d = summary['attack_and_crit']['modes'][mode]
        if d['available']:
            lines.append(f"{mode:17s} {d['attacks']:>5,d} attacks  "
                         f"{d['confirm_attempts']:>4,d} confirms  "
                         f"{d['confirmed_crits']:>4,d} confirmed crits")
        else:
            lines.append(f'{mode:17s} unavailable (attack snapshot not generated)')
    lines.extend([
        f"Same attack/confirm events in all modes: "
        f"{summary['attack_and_crit']['all_three_attack_sequences_equal']}",
        'Attack/confirm mismatches: three_mode_attack_mismatches.csv','',
        'Source details: three_mode_comparison.json', '',
        'In-game verification (manual independent source):',
        '  Target HP start: __________     HP end: __________',
        '  Target healing during recording: __________',
        '  Damage on other targets should NOT enter this target HP check.',
        '  The reference must come from the game, not Analyzer results.',
        '  Combat Log lines lost to scrolling/focus cannot be checked.',
        '',
    ])

    (session / 'three_mode_comparison.txt').write_text(
        '\n'.join(lines),encoding='utf-8')
    return summary


if __name__ == '__main__':
    import argparse
    p=argparse.ArgumentParser()
    p.add_argument('--session',required=True)
    args=p.parse_args()
    print(json.dumps(generate(args.session),indent=2))
