"""Snapshot attack/critical/rolling-APM event streams per independent mode.

The ordinary attack_events.csv and crit_by_target.csv remain the GUI's current
report. Mode-specific files are immutable snapshots until that mode runs again.
"""
from __future__ import annotations
import csv
import json
from collections import defaultdict
from pathlib import Path

FIELDS = ['line','elapsed_seconds','event','target','roll','bonus','total',
          'outcome','confirmed','linked_attack_line','text']
SUMMARY_FIELDS = ['target','attacks','hits','grazing_hits','misses',
                  'critical_threats','confirm_attempts','confirmed_crits','failed_confirms']


def save(session, mode, attacks, confirms):
    from process_latest import summarize_attack_crit, build_apm_rows
    session = Path(session)
    if mode not in ('fast_live','ffv1_post','replay_live'):
        raise ValueError('Unrecognized independent source')
    rows = sorted(list(attacks)+list(confirms), key=lambda r: int(r['line']))
    out = session / f'{mode}_attack_events.csv'
    with out.open('w',encoding='utf-8',newline='') as f:
        w = csv.DictWriter(f,fieldnames=FIELDS,extrasaction='ignore')
        w.writeheader()
        w.writerows(rows)
    stats = summarize_attack_crit(attacks, confirms)
    with (session / f'{mode}_crit_summary.csv').open('w',encoding='utf-8',newline='') as f:
        w = csv.DictWriter(f,fieldnames=SUMMARY_FIELDS)
        w.writeheader()
        for target, d in sorted(stats.items()):
            w.writerow(dict(target=target,attacks=d['attacks'],hits=d['hits'],
                            grazing_hits=d['grazes'],misses=d['misses'],
                            critical_threats=d['threats'],
                            confirm_attempts=d['all_confirm_attempts'],
                            confirmed_crits=d['confirmed'],failed_confirms=d['all_failed']))
    cfg = {}
    try: cfg = json.loads((session/'config_used.json').read_text(encoding='utf-8'))
    except (OSError,ValueError): pass
    window = max(1.0,float(cfg.get('live_dps',{}).get('rolling_window_seconds',15)))
    with (session / f'{mode}_apm_rolling.csv').open('w',encoding='utf-8',newline='') as f:
        w=csv.writer(f)
        w.writerow(['elapsed_seconds','capture_elapsed_seconds','rolling_apm','window_seconds','window_attacks'])
        for t, capture_t, apm, count in build_apm_rows(attacks,window):
            w.writerow([f'{t:.3f}',f'{capture_t:.6f}',f'{apm:.3f}',f'{window:g}',count])
    return out


def save_from_replay_ocr(session):
    from process_latest import reassemble_wrapped_combat_messages,parse_attack_crit_events,normalize_text
    session=Path(session)
    with (session/'replay_live_ocr_lines.csv').open('r',encoding='utf-8',newline='') as f:
        data=list(csv.DictReader(f))
    texts=[]
    times=[]
    for r in data:
        try: t=float(r.get('capture_elapsed_seconds',''))
        except (ValueError,TypeError): continue
        texts.append(normalize_text(r.get('effective_ocr_text') or r.get('raw_ocr_text') or ''))
        times.append(t)
    logical,lt,ll=reassemble_wrapped_combat_messages(texts,times)
    attacks,confirms=parse_attack_crit_events(logical,lt,ll)
    save(session,'replay_live',attacks,confirms)
    return len(attacks),len(confirms)
