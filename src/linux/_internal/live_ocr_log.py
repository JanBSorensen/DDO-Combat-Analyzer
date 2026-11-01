"""Lossless-as-observed, timestamped live OCR evidence written atomically.

The original Indra text is kept separately from parser repairs and numeric
rechecks. OCR lines that were never detected/dropped are not fabricated.
"""
from __future__ import annotations

import csv
import json
import os
import time
from pathlib import Path

COLUMNS = [
    'line_number', 'capture_elapsed_seconds', 'capture_wall_epoch',
    'ocr_finished_wall_epoch', 'raw_ocr_text', 'effective_ocr_text',
    'parsed_outgoing_damage', 'numeric_recheck_result',
    'numeric_recheck_amount', 'event_capture_elapsed_seconds',
]


def new_row(elapsed, capture_start_epoch, raw, effective, amount, recheck, recheck_amount,
            event_elapsed=None, ocr_epoch=None):
    elapsed = float(elapsed)
    start = float(capture_start_epoch)
    return {
        'capture_elapsed_seconds': elapsed,
        'capture_wall_epoch': start + elapsed,
        'ocr_finished_wall_epoch': float(ocr_epoch or time.time()),
        'raw_ocr_text': raw or '',
        'effective_ocr_text': effective or '',
        'parsed_outgoing_damage': '' if amount is None else int(amount),
        'numeric_recheck_result': recheck or '',
        'numeric_recheck_amount': '' if recheck_amount in ('', None) else recheck_amount,
        'event_capture_elapsed_seconds': '' if event_elapsed is None else float(event_elapsed),
    }



def filter_session_to_stop(event_rows, ocr_rows, live_samples, stop_elapsed):
    """Apply the authoritative STOP timestamp at FLUSH, even if the
    helper received STOP_SESSION a few milliseconds after it was sent.

    This operates on buffered *copies* and never suppresses pre-Stop OCR
    work that finished asynchronously. Recompute event numbering/cumulative
    amounts after filtering so all exported CSV columns remain consistent.
    """
    if stop_elapsed is None:
        return event_rows, ocr_rows, live_samples, {"excluded_events": 0, "excluded_ocr_rows": 0}
    cutoff = max(0.0, float(stop_elapsed)) + 1e-6
    ev = [list(r) for r in event_rows if float(r[1]) <= cutoff]
    ocr = [r for r in ocr_rows if float(r.get('capture_elapsed_seconds') or 0) <= cutoff]
    samples = [r for r in live_samples if float(r[0]) <= cutoff]
    raw = final = 0
    for num, row in enumerate(ev, 1):
        raw += int(row[3])
        final += int(row[4])
        row[0] = num
        row[5] = raw
        row[6] = final
    return ev, ocr, samples, {
        "excluded_events": len(event_rows) - len(ev),
        "excluded_ocr_rows": len(ocr_rows) - len(ocr),
    }


def write_log(session, rows, *, complete, dropped=0, note='', started_epoch=None, stopped_epoch=None):
    session = Path(session)
    session.mkdir(parents=True, exist_ok=True)
    path = session / 'live_ocr_lines.csv'
    ready = session / 'live_ocr_complete.json'
    # Never advertise a stale previous run as completed during an atomic rewrite.
    ready.unlink(missing_ok=True)
    temp = path.with_name(path.name + '.tmp')
    sorted_rows = sorted(rows, key=lambda r: (float(r.get('capture_elapsed_seconds') or 0),
                                              float(r.get('ocr_finished_wall_epoch') or 0)))
    with temp.open('w', newline='', encoding='utf-8') as file:
        writer = csv.DictWriter(file, fieldnames=COLUMNS, extrasaction='ignore')
        writer.writeheader()
        for i, row in enumerate(sorted_rows, 1):
            writer.writerow(dict(row, line_number=i))
    os.replace(temp, path)
    metadata = {
        'format_version': 1,
        'ready': True,
        'capture_ocr_complete': bool(complete),  # queue drained, NOT verified visual coverage
        'ocr_queue_drained': bool(complete),
        'recognized_lines': len(sorted_rows),
        'dropped_line_crops': int(dropped),
        'capture_start_epoch': started_epoch,
        'capture_stop_epoch': stopped_epoch,
        'note': note,
    }
    temp_ready = ready.with_name(ready.name + '.tmp')
    temp_ready.write_text(json.dumps(metadata, indent=2), encoding='utf-8')
    os.replace(temp_ready, ready)
    return path


def read_log(session):
    session = Path(session)
    ready = session / 'live_ocr_complete.json'
    csv_path = session / 'live_ocr_lines.csv'
    if not ready.exists() or not csv_path.exists():
        return None, None
    try:
        metadata = json.loads(ready.read_text(encoding='utf-8'))
        if not metadata.get('ready'):
            return None, None
        with csv_path.open('r', newline='', encoding='utf-8') as f:
            return list(csv.DictReader(f)), metadata
    except (OSError, ValueError, csv.Error):
        return None, None
