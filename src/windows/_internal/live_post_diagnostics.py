"""Event-level live vs FFV1 post reconciliation with reproducible CSV evidence."""
from __future__ import annotations

import bisect
import csv
import json
from collections import defaultdict
from pathlib import Path


def _rows(path):
    try:
        with Path(path).open('r', encoding='utf-8', newline='') as f:
            return list(csv.DictReader(f))
    except (OSError, ValueError, csv.Error):
        return []


def _float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def reconcile(session, *, source='ffv1_post', wrap_stats=None, guard_stats=None):
    session = Path(session)
    live_rows = _rows(session / 'live_damage_events.csv')
    post_rows = ([r for r in _rows(session / 'damage_events.csv')
                  if r.get('event') == 'damage_dealt']
                 if source == 'ffv1_post' else [])
    ocr_rows = _rows(session / 'live_ocr_lines.csv')
    post_ocr_rows = (_rows(session / 'post_ocr_lines.csv') if source == 'ffv1_post' else [])
    try:
        ocr_meta = json.loads((session / 'live_ocr_complete.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        ocr_meta = {}

    live = [{'time':_float(r.get('event_capture_elapsed_seconds')),
             'amount':_int(r.get('final_damage')), 'text':r.get('batch_ocr_text',''),
             'recheck':r.get('numeric_recheck_result','')} for r in live_rows]
    post = [{'time':_float(r.get('elapsed_seconds')),
             'amount':_int(r.get('amount')), 'text':r.get('text',''),
             'line':r.get('line','')} for r in post_rows]
    diff_rows = []
    matched_live = set()
    matched_post = set()
    counts = defaultdict(int)
    indexed_times = sorted((x['time'], j) for j,x in enumerate(post) if x['time'] is not None)
    time_keys = [x[0] for x in indexed_times]
    def possible_matches(t, radius):
        lo = bisect.bisect_left(time_keys, t-radius)
        hi = bisect.bisect_right(time_keys, t+radius)
        return (indexed_times[k][1] for k in range(lo,hi))
    # Match exact amounts first to avoid repeated same-time damage procs being
    # accidentally paired with different-sized events. Pair each event once.
    def assign(i, j, classification):
        a, b = live[i], post[j]
        matched_live.add(i); matched_post.add(j)
        counts[classification] += 1
        if classification != 'exact':
            diff_rows.append({
                'classification':classification,
                'live_time':a['time'], 'post_time':b['time'],
                'live_amount':a['amount'], 'post_amount':b['amount'],
                'delta_live_minus_post':a['amount'] - b['amount'],
                'time_offset':round(a['time'] - b['time'], 4),
                'live_text':a['text'], 'post_text':b['text'],
            })

    for i, item in enumerate(live):
        if item['time'] is None:
            continue
        choices = [(abs(item['time']-post[j]['time']), j)
                   for j in possible_matches(item['time'],1.5)
                   if j not in matched_post and post[j]['amount'] == item['amount']]
        if choices:
            _, j = min(choices)
            assign(i,j,'exact')
    for i, item in enumerate(live):
        if i in matched_live or item['time'] is None:
            continue
        choices = [(abs(item['time']-post[j]['time']), j)
                   for j in possible_matches(item['time'],.45)
                   if j not in matched_post]
        if choices:
            _,j = min(choices)
            assign(i,j,'amount_mismatch')
    for i,item in enumerate(live):
        if i not in matched_live:
            counts['live_only'] += 1
            diff_rows.append({
                'classification':'live_only', 'live_time':item['time'], 'post_time':'',
                'live_amount':item['amount'], 'post_amount':'', 'delta_live_minus_post':'',
                'time_offset':'', 'live_text':item['text'], 'post_text':'',
            })
    for j,item in enumerate(post):
        if j not in matched_post:
            counts['post_only'] += 1
            diff_rows.append({
                'classification':'post_only', 'live_time':'', 'post_time':item['time'],
                'live_amount':'', 'post_amount':item['amount'], 'delta_live_minus_post':'',
                'time_offset':'', 'live_text':'', 'post_text':item['text'],
            })
    diff_file=session/'live_post_event_diff.csv'
    cols=['classification','live_time','post_time','live_amount','post_amount',
          'delta_live_minus_post','time_offset','live_text','post_text']
    with diff_file.open('w', newline='', encoding='utf-8') as f:
        w=csv.DictWriter(f,fieldnames=cols)
        w.writeheader(); w.writerows(diff_rows)

    # Bin all damage to expose time periods with missing lines or OCR failures.
    buckets=defaultdict(lambda: {'live_events':0,'live_damage':0,'post_events':0,'post_damage':0})
    for name, stream in [('live',live),('post',post)]:
        for x in stream:
            if x['time'] is not None and x['time'] >= 0:
                b = int(x['time']//15)*15
                buckets[b][name+'_events'] += 1
                buckets[b][name+'_damage'] += x['amount']
    for stream, ocr in [('live', ocr_rows), ('post', post_ocr_rows)]:
        for r in ocr:
            t=_float(r.get('capture_elapsed_seconds'))
            if t is not None and t >= 0:
                buckets[int(t//15)*15][stream+'_ocr_lines'] = (
                    buckets[int(t//15)*15].get(stream+'_ocr_lines',0)+1)
    with (session/'live_post_15s_buckets.csv').open('w',newline='',encoding='utf-8') as f:
        w=csv.writer(f)
        w.writerow(['start_second','live_ocr_lines','post_ocr_lines','live_events',
                    'post_events','live_damage','post_damage','difference_live_minus_post'])
        for b, x in sorted(buckets.items()):
            w.writerow([b,x.get('live_ocr_lines',0),x.get('post_ocr_lines',0),
                        x['live_events'],x['post_events'],x['live_damage'],
                        x['post_damage'],x['live_damage']-x['post_damage']])
    samples=_rows(session/'live_dps.csv')
    max_dropped=max((_int(x.get('dropped_lines_total')) for x in samples),default=0)
    max_qdepth=max((_int(x.get('ocr_queue_depth')) for x in samples),default=0)
    live_total=sum(x['amount'] for x in live)
    post_total=sum(x['amount'] for x in post)
    live_last_time = max((r['time'] for r in live if r['time'] is not None), default=None)
    live_last_ocr = max((_float(r.get('capture_elapsed_seconds')) for r in ocr_rows
                         if _float(r.get('capture_elapsed_seconds')) is not None), default=None)
    later_post = (sum(1 for x in post if x['time'] is not None and
                      live_last_ocr is not None and x['time'] > live_last_ocr + 2.0)
                  if source == 'ffv1_post' else None)
    probable_tracking_gap = bool(source == 'ffv1_post' and
                                 live_last_ocr is not None and
                                 len(post_ocr_rows) >= 2 * max(1, len(ocr_rows)) and
                                 later_post is not None and later_post >= max(10, int(len(post) * .3)))
    try:
        tracking_quality = json.loads((session / 'live_tracking_quality.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        tracking_quality = {}
    result={
        'analysis_source':source,
        'post_ffv1_available':source == 'ffv1_post',
        'live_events':len(live), 'live_total_damage':live_total,
        'post_events':len(post) if source=='ffv1_post' else None,
        'post_total_damage':post_total if source=='ffv1_post' else None,
        'difference_live_minus_post':live_total-post_total if source=='ffv1_post' else None,
        'difference_percent_of_post': (100*(live_total-post_total)/post_total
                                       if source=='ffv1_post' and post_total else None),
        'matched_exact':counts['exact'] if source=='ffv1_post' else None,
        'matched_amount_mismatch':counts['amount_mismatch'] if source=='ffv1_post' else None,
        'live_only':counts['live_only'] if source=='ffv1_post' else None,
        'post_only':counts['post_only'] if source=='ffv1_post' else None,
        'live_ocr_lines':len(ocr_rows),
        'post_ocr_lines':len(post_ocr_rows) if source == 'ffv1_post' else None,
        'live_ocr_complete':ocr_meta.get('capture_ocr_complete'),
        'live_dropped_crops':ocr_meta.get('dropped_line_crops',max_dropped),
        'max_live_ocr_queue_depth':max_qdepth,
        'last_live_ocr_at_seconds':live_last_ocr,
        'post_damage_events_after_last_live_ocr_plus_2s':later_post,
        'probable_live_visual_tracking_gap':probable_tracking_gap,
        'live_tracking_quality':tracking_quality,
        'ocr_queue_drained_does_not_guarantee_visual_coverage':True,
        'wrapped_lines':wrap_stats or {}, 'numeric_validation':guard_stats or {},
        'comparison_note':('FFV1 post results compared with actual live DPS events. '
                           'Exact event matches use a 1.5s time window; amount '
                           'mismatches use a 0.45s window, so repeated equal hits '
                           'near one another may be matched ambiguously.'
                           if source=='ffv1_post' else
                           'Fast report is from live OCR. Run Reanalyse for independent FFV1 post comparison.'),
    }
    (session/'live_post_diagnostics.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    with (session/'live_post_diagnostics.txt').open('w',encoding='utf-8') as f:
        f.write('DDO Combat Analyzer - live / post diagnostics\n')
        for k,v in result.items():
            f.write(f'{k}: {v}\n')
        f.write('\nDetailed event differences: live_post_event_diff.csv\n')
        f.write('Where differences occurred: live_post_15s_buckets.csv\n')
        f.write('Every recognized live OCR row: live_ocr_lines.csv\n')
        f.write('Numeric validation decisions: post_numeric_validation.csv\n')
    return result
