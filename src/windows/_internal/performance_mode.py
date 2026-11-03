"""Full and Light profiles. Do not crop OCR input or change parsing rules."""
from __future__ import annotations

import ctypes
import csv
import json
import os
import time
from pathlib import Path

LIGHT_FPS = 20
MAX_LIGHT_OCR_THREADS = 2
MODES = ('full', 'light')


def mode_from_config(cfg):
    value = str((cfg or {}).get('performance', {}).get('mode', 'full')).lower()
    return value if value in MODES else 'full'


def scan_rate(cfg):
    normal = max(10, int(cfg.get('live_dps', {}).get('tracking_fps', cfg.get('capture_fps', 30))))
    return LIGHT_FPS if mode_from_config(cfg) == 'light' else normal


def ocr_threads(cfg, resolver):
    normal = int(resolver(cfg.get('live_dps', {}).get('indra_threads', 'auto')))
    return min(MAX_LIGHT_OCR_THREADS, normal) if mode_from_config(cfg) == 'light' else normal


def current_rss_bytes():
    if os.name == 'nt':
        try:
            class PMC(ctypes.Structure):
                _fields_ = [
                    ('cb', ctypes.c_ulong), ('PageFaultCount', ctypes.c_ulong),
                    ('PeakWorkingSetSize', ctypes.c_size_t), ('WorkingSetSize', ctypes.c_size_t),
                    ('QuotaPeakPagedPoolUsage', ctypes.c_size_t),
                    ('QuotaPagedPoolUsage', ctypes.c_size_t),
                    ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t),
                    ('QuotaNonPagedPoolUsage', ctypes.c_size_t),
                    ('PagefileUsage', ctypes.c_size_t), ('PeakPagefileUsage', ctypes.c_size_t),
                ]
            mem = PMC()
            mem.cb = ctypes.sizeof(mem)
            handle = ctypes.windll.kernel32.GetCurrentProcess()
            if ctypes.windll.psapi.GetProcessMemoryInfo(handle, ctypes.byref(mem), mem.cb):
                return int(mem.WorkingSetSize)
        except (AttributeError, OSError, ValueError):
            return None
    try:
        pages = int(Path('/proc/self/statm').read_text().split()[1])
        return pages * os.sysconf('SC_PAGE_SIZE')
    except (OSError, ValueError, IndexError):
        return None


class LiveResourceTracker:
    """Measure only this Live OCR helper; DDO and FFmpeg are not measured."""
    def __init__(self):
        self.cpu_start = time.process_time()
        self.wall_start = time.monotonic()
        self.samples = 0
        self.peak_rss = 0
        self.sample()

    def sample(self):
        rss = current_rss_bytes()
        if rss is not None:
            self.peak_rss = max(self.peak_rss, rss)
        self.samples += 1

    def finish(self, *, mode, fps, threads, dropped_crops, ocr_rows, drained):
        self.sample()
        elapsed = max(0.001, time.monotonic() - self.wall_start)
        cpu = max(0.0, time.process_time() - self.cpu_start)
        return {
            'scope': 'Live OCR helper only; excludes DDO, GUI, FFmpeg and OS activity',
            'mode': mode,
            'wall_seconds': round(elapsed, 4),
            'process_cpu_seconds': round(cpu, 4),
            'average_cpu_one_core_percent': round(100.0 * cpu / elapsed, 2),
            'sampled_peak_working_set_mb': round(self.peak_rss / (1024 ** 2), 3) if self.peak_rss else None,
            'rss_samples': self.samples,
            'target_tracking_fps': fps,
            'ocr_threads': threads,
            'dropped_ocr_crops': dropped_crops,
            'recognized_ocr_rows': ocr_rows,
            'pre_stop_ocr_queue_drained': bool(drained),
        }


def comparison_summary(session):
    session = Path(session)
    def load_json(name):
        try:
            return json.loads((session / name).read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return {}
    cfg = load_json('config_used.json')
    analysis = load_json('analysis_summary.json')
    live = load_json('live_resource_usage.json')
    clock = load_json('recording_clock.json')
    meta = load_json('live_ocr_complete.json')
    quality = load_json('live_tracking_quality.json')
    path = session / 'combat_changed_gray_ffv1.mkv'
    total_bytes = 0
    for p in session.rglob('*'):
        if p.is_file():
            try:
                total_bytes += p.stat().st_size
            except OSError:
                pass
    try:
        video_bytes = path.stat().st_size
    except OSError:
        video_bytes = 0

    def count_csv(name, predicate=None):
        try:
            with (session / name).open('r', encoding='utf-8', newline='') as stream:
                rows = csv.DictReader(stream)
                return sum(1 for item in rows if predicate is None or predicate(item))
        except (OSError, csv.Error):
            return None

    outgoing_events = count_csv('damage_events.csv', lambda r: r.get('event') == 'damage_dealt')
    attacks_count = count_csv('attack_events.csv', lambda r: r.get('event') == 'attack')
    confirms_count = count_csv('attack_events.csv', lambda r: r.get('event') != 'attack')
    data = {
        'mode': mode_from_config(cfg),
        'source': analysis.get('analysis_source', ''),
        'session_seconds': clock.get('capture_elapsed_seconds', analysis.get('recording_duration_seconds')),
        'total_damage': analysis.get('total_damage'),
        'damage_events': outgoing_events,
        'attacks': attacks_count,
        'confirmations': confirms_count,
        'recorded_video_bytes': video_bytes,
        'recorded_video_mb': round(video_bytes / (1024 ** 2), 3),
        'total_session_bytes': total_bytes,
        'total_session_mb': round(total_bytes / (1024 ** 2), 3),
        'ocr_rows': meta.get('recognized_lines'),
        'dropped_line_crops': meta.get('dropped_line_crops'),
        'ocr_queue_drained': meta.get('ocr_queue_drained'),
        'observed_tracking_frames': quality.get('tracking_frames'),
        'focus_interruptions': quality.get('focus_interruptions',
                                           analysis.get('data_quality', {}).get('focus_interruptions')),
        'focus_interruption_seconds': quality.get('focus_interruption_seconds'),
        'live_target_fps': live.get('target_tracking_fps', scan_rate(cfg)),
        'live_ocr_threads': live.get('ocr_threads'),
        'live_cpu_seconds': live.get('process_cpu_seconds'),
        'live_average_cpu_one_core_percent': live.get('average_cpu_one_core_percent'),
        'live_sampled_peak_working_set_mb': live.get('sampled_peak_working_set_mb'),
        'note': 'Metrics measure OCR helper only. Compare similar-length fights and check target HP.',
    }
    (session / 'performance_comparison.json').write_text(json.dumps(data, indent=2), encoding='utf-8')
    return data
