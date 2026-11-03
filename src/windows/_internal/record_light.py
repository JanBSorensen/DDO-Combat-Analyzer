"""Minimal Light Mode session recorder: no capture loop, FFmpeg or video."""
from __future__ import annotations

import csv
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from performance_mode import mode_from_config, scan_rate

HERE = Path(__file__).resolve().parent


def create_session(config, now=None):
    now = now or datetime.now()
    stem = 'combat_' + now.strftime('%Y%m%d_%H%M%S')
    session = HERE / stem
    n = 2
    while session.exists():
        session = HERE / f'{stem}_{n}'
        n += 1
    session.mkdir(parents=True, exist_ok=False)
    (session / 'config_used.json').write_text(json.dumps(config, indent=2), encoding='utf-8')
    return session


def main():
    cfg = json.loads((HERE / 'config.json').read_text(encoding='utf-8'))
    if mode_from_config(cfg) != 'light':
        print('ERROR: This recorder is only for Light Mode.', flush=True)
        return 2
    session = create_session(cfg)
    print(f'SESSION_DIR|{session}', flush=True)
    print('RECORDER_READY', flush=True)
    cmd = sys.stdin.readline().strip().upper()
    if cmd != 'START':
        for f in session.iterdir():
            f.unlink()
        session.rmdir()
        return 0
    start_epoch = time.time()
    start_mono = time.perf_counter()
    clock = session / 'recording_clock.json'
    clock.write_text(json.dumps({
        'start_epoch': start_epoch,
        'start_wall_time': datetime.now().isoformat(timespec='milliseconds'),
        'recording_mode': 'light',
        'video_recording': False,
    }, indent=2), encoding='utf-8')
    print('CAPTURE_STARTED', flush=True)
    while True:
        command = sys.stdin.readline()
        if not command or command.strip().upper() == 'STOP':
            break
    end_epoch = time.time()
    duration = max(0.001, time.perf_counter() - start_mono)
    data = json.loads(clock.read_text(encoding='utf-8'))
    data.update({'end_epoch': end_epoch, 'capture_elapsed_seconds': duration})
    clock.write_text(json.dumps(data, indent=2), encoding='utf-8')
    with (session / 'focus_interruptions.csv').open('w', newline='', encoding='utf-8') as out:
        csv.writer(out).writerow(['start_elapsed_seconds', 'end_elapsed_seconds', 'duration_seconds', 'resume_saved_frame'])
    (session / 'recording_summary.txt').write_text(
        'DDO Combat Analyzer - Light Mode (live OCR only)\n'
        f'Session: {session.name}\n'
        f'Capture area: {cfg["capture"]}\n'
        f'Target live tracking FPS: {scan_rate(cfg)}\n'
        f'Elapsed seconds: {duration:.3f}\n'
        'Video size: 0.0 MB\n'
        'FFv1 video: disabled\n'
        'Reanalyze (FFV1) and Replay Live are unavailable for this session.\n',
        encoding='utf-8')
    (HERE / 'LATEST_SESSION.txt').write_text(str(session.resolve()), encoding='utf-8')
    print(f'Recording saved: {session}', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
