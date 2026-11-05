"""Low-overhead CPU/RAM samples of the analyzer's *whole* process tree.

This collector never inspects or attaches to DDO.
Sampling happens in a background thread owned by the analyzer GUI. It counts
GUI + live OCR + session recorder + FFmpeg descendants, NOT the game or OS.
"""
from __future__ import annotations

import csv
import json
import os
import statistics
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import psutil

ROLES = ('gui', 'live_ocr', 'recorder', 'ffmpeg', 'other_child')
CPU_HEADER = ['wall_utc', 'elapsed_seconds', 'sample_seconds', 'cpu_cores_total',
              'ram_mib_total', 'process_count']
CPU_HEADER += [f'cpu_{role}' for role in ROLES]
CPU_HEADER += [f'ram_mib_{role}' for role in ROLES]
DETAIL_HEADER = ['wall_utc', 'elapsed_seconds', 'pid', 'role', 'process_name',
                 'cpu_cores', 'ram_mib']


def _percentile(values, percentage):
    values = sorted(values)
    if not values:
        return 0.0
    x = (len(values) - 1) * percentage / 100.0
    low = int(x)
    high = min(low + 1, len(values) - 1)
    if high == low:
        return float(values[low])
    return values[low] * (high - x) + values[high] * (x - low)


class SessionResourceMonitor:
    """Record measured CPU core equivalents and sampled RSS at ~250 ms intervals.

    CPU is sum of *deltas* of process CPU time / elapsed wall time, so 1.0 is
    equivalent to one fully loaded logical CPU. RAM is sum of resident working
    sets, which can double-count shared pages across processes. Short-lived
    processes that die entirely between polls cannot be measured. Do not
    include process-wide cumulative CPU from before this recording.
    """

    def __init__(self, session, *, gui_pid, live_pid=None, recorder_pid=None,
                 performance_mode='full', capture_fps=None, tracking_fps=None,
                 ocr_threads=None, interval=0.25):
        self.session = Path(session)
        self.gui_pid = int(gui_pid)
        self.live_pid = int(live_pid) if live_pid else None
        self.recorder_pid = int(recorder_pid) if recorder_pid else None
        self.performance_mode = performance_mode
        self.capture_fps = capture_fps
        self.tracking_fps = tracking_fps
        self.ocr_threads = ocr_threads
        self.interval = max(0.1, float(interval))
        self.stop_event = threading.Event()
        self.worker = None
        self._thread_lock = threading.Lock()
        self.latest = None
        self.summary = None
        self.error = None
        self._previous_cpu = {}
        self._role_cache = {}
        self._rows = []
        self._measurements = []
        self._start_monotonic = None
        self._last_monotonic = None

    def start(self):
        if self.worker is not None:
            raise RuntimeError('Resource monitor has already started')
        self.session.mkdir(parents=True, exist_ok=True)
        self._start_monotonic = time.monotonic()
        self.worker = threading.Thread(target=self._run, name='session-cpu-ram-monitor', daemon=True)
        self.worker.start()

    def stop(self, timeout=3.0):
        self.stop_event.set()
        worker = self.worker
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=timeout)
        return self.summary

    def snapshot(self):
        with self._thread_lock:
            return dict(self.latest) if self.latest is not None else None

    def _role(self, pid, name, cmdline, key):
        if pid == self.gui_pid:
            return 'gui'
        if pid == self.live_pid:
            return 'live_ocr'
        if pid == self.recorder_pid:
            return 'recorder'
        if key in self._role_cache:
            return self._role_cache[key]
        name_lower = str(name).lower()
        cmd_lower = str(cmdline).lower()
        if 'ffmpeg' in name_lower or 'ffmpeg' in cmd_lower:
            role = 'ffmpeg'
        elif 'continuous_live.py' in cmd_lower or 'live_overlay.py' in cmd_lower:
            role = 'live_ocr'
        elif 'record_optimized.py' in cmd_lower or 'record_light.py' in cmd_lower:
            role = 'recorder'
        else:
            role = 'other_child'
        self._role_cache[key] = role
        return role

    def _sample(self, now, writer, detail_writer):
        timestamp = datetime.now(timezone.utc).isoformat(timespec='milliseconds')
        dt = 0.0 if self._last_monotonic is None else now - self._last_monotonic
        try:
            parent = psutil.Process(self.gui_pid)
            processes = [parent] + parent.children(recursive=True)
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            processes = []
        seen = set()
        cpu_seconds = {role: 0.0 for role in ROLES}
        ram_bytes = {role: 0 for role in ROLES}
        details = []
        for process in processes:
            try:
                # create_time makes cached CPU times safe against PID reuse.
                pid = process.pid
                key = (pid, round(process.create_time(), 3))
                if key in seen:
                    continue
                seen.add(key)
                times = process.cpu_times()
                cumulative = max(0.0, float(times.user + times.system))
                memory = max(0, int(process.memory_info().rss))
                name = process.name()
                cmdline = ''
                if key not in self._role_cache and pid not in (
                        self.gui_pid, self.live_pid, self.recorder_pid):
                    # Process name and RSS should still be counted even when
                    # Windows refuses command-line access to a child.
                    try:
                        cmdline = ' '.join(process.cmdline())
                    except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
                        pass
                role = self._role(pid, name, cmdline, key)
                prev = self._previous_cpu.get(key)
                # Do not include the GUI's earlier lifetime CPU time. New
                # processes enter at their first sample with zero delta.
                delta = max(0.0, cumulative - prev) if prev is not None else 0.0
                self._previous_cpu[key] = cumulative
                cores = delta / dt if dt >= 0.02 else 0.0
                cpu_seconds[role] += delta
                ram_bytes[role] += memory
                details.append((pid, role, name, cores, memory / (1024 ** 2)))
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess,
                    OSError, ValueError):
                continue
        self._last_monotonic = now
        if dt < 0.02:
            return
        elapsed = now - self._start_monotonic
        per_role_cpu = {role: cpu_seconds[role] / dt for role in ROLES}
        per_role_ram = {role: ram_bytes[role] / (1024 ** 2) for role in ROLES}
        total_cpu = sum(per_role_cpu.values())
        total_ram = sum(per_role_ram.values())
        row = {
            'wall_utc': timestamp,
            'elapsed_seconds': round(elapsed, 4),
            'sample_seconds': round(dt, 4),
            'cpu_cores_total': round(total_cpu, 4),
            'ram_mib_total': round(total_ram, 3),
            'process_count': len(details),
        }
        row.update({f'cpu_{role}': round(per_role_cpu[role], 4) for role in ROLES})
        row.update({f'ram_mib_{role}': round(per_role_ram[role], 3) for role in ROLES})
        writer.writerow(row)
        for pid, role, name, cores, mib in details:
            detail_writer.writerow([timestamp, round(elapsed, 4), pid, role, name,
                                    round(cores, 4), round(mib, 3)])
        self._measurements.append((dt, total_cpu, total_ram, per_role_cpu, per_role_ram))
        with self._thread_lock:
            self.latest = dict(row)

    def _create_summary(self):
        values = self._measurements
        samples = len(values)
        duration = max(0.0, (time.monotonic() - self._start_monotonic))
        weighted = sum(x[0] for x in values)
        cpu = [v[1] for v in values]
        ram = [v[2] for v in values]
        mean_cpu = (sum(dt * val for dt, val, *_ in values) / weighted) if weighted else 0.0
        by_role = {}
        for role in ROLES:
            role_cpu = [v[3][role] for v in values]
            role_ram = [v[4][role] for v in values]
            mean_role = (sum(v[0] * v[3][role] for v in values) / weighted) if weighted else 0.0
            by_role[role] = {
                'mean_cpu_cores': round(mean_role, 4),
                'peak_cpu_cores': round(max(role_cpu, default=0.0), 4),
                'peak_ram_mib': round(max(role_ram, default=0.0), 3),
            }
        return {
            'measurement_scope': 'GUI process and its recursive children; excludes DDO and unrelated processes',
            'measurement_window': 'CAPTURE_STARTED until user STOP (or recorder termination)',
            'cpu_units': 'one core equivalent = 1 CPU-second per wall-second',
            'ram_units': 'sum of per-process resident working sets in MiB; shared memory may be counted more than once',
            'sample_interval_seconds': self.interval,
            'duration_seconds': round(duration, 4),
            'sample_count': samples,
            'performance_mode': self.performance_mode,
            'requested_capture_fps': self.capture_fps,
            'requested_live_fps': self.tracking_fps,
            'actual_ocr_threads_configured': self.ocr_threads,
            'mean_cpu_cores': round(mean_cpu, 4),
            'p95_cpu_cores': round(_percentile(cpu, 95), 4),
            'peak_cpu_cores': round(max(cpu, default=0.0), 4),
            'mean_ram_mib': round(statistics.mean(ram) if ram else 0.0, 3),
            'p95_ram_mib': round(_percentile(ram, 95), 3),
            'peak_ram_mib': round(max(ram, default=0.0), 3),
            'roles': by_role,
            'note': 'Not an identical workload to .NET CPU Lab: Python Full captures FFV1 and processes richer OCR.',
        }

    def _run(self):
        samples_path = self.session / 'python_resource_samples.csv'
        process_path = self.session / 'python_resource_processes.csv'
        summary_path = self.session / 'python_resource_summary.json'
        try:
            with samples_path.open('w', newline='', encoding='utf-8') as sample_file, \
                 process_path.open('w', newline='', encoding='utf-8') as process_file:
                writer = csv.DictWriter(sample_file, fieldnames=CPU_HEADER)
                details = csv.writer(process_file)
                writer.writeheader()
                details.writerow(DETAIL_HEADER)
                self._sample(time.monotonic(), writer, details)  # Baseline (no row).
                loops = 0
                while not self.stop_event.wait(self.interval):
                    self._sample(time.monotonic(), writer, details)
                    loops += 1
                    if loops % 4 == 0:
                        sample_file.flush()
                        process_file.flush()
                # A partial final interval is useful unless it is tiny and
                # would distort core-equivalent CPU peaks.
                if time.monotonic() - self._last_monotonic >= self.interval * 0.8:
                    self._sample(time.monotonic(), writer, details)
            result = self._create_summary()
            summary_path.write_text(json.dumps(result, indent=2), encoding='utf-8')
            with self._thread_lock:
                self.summary = result
        except Exception as exc:
            self.error = f'{type(exc).__name__}: {exc}'
            try:
                (self.session / 'python_resource_monitor_error.txt').write_text(
                    self.error + '\n', encoding='utf-8')
            except OSError:
                pass
