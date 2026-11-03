from __future__ import annotations

import argparse
import csv
import ctypes
from ctypes import wintypes
import json
import os
import queue
import sys
import threading
import time
from collections import deque
from pathlib import Path

import mss
import tkinter as tk

from live_overlay import LiveEstimator, make_clickthrough, is_ddo_foreground
from live_input_diagnostics import LiveInputDiagnostics

HERE = Path(__file__).resolve().parent
CONFIG = HERE / 'config.json'


def find_ddo_client_rect():
    if os.name != 'nt':
        return None
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.FindWindowW('Turbine Device Class', None)
        if not hwnd:
            hwnd = user32.FindWindowW('Turbine Device Class Name', None)
        if not hwnd:
            return None
        rect = wintypes.RECT()
        if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
            return None
        pt = wintypes.POINT(0, 0)
        if not user32.ClientToScreen(hwnd, ctypes.byref(pt)):
            return None
        return {
            'left': int(pt.x), 'top': int(pt.y),
            'width': int(rect.right - rect.left),
            'height': int(rect.bottom - rect.top),
        }
    except Exception:
        return None


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def overlay_xy(cfg, width, height, fallback_mon):
    live = cfg.get('live_dps', {})
    pos = str(live.get('overlay_position', 'top_center')).strip().lower()
    rect = find_ddo_client_rect()
    if rect is None:
        rect = {
            'left': int(fallback_mon['left']), 'top': int(fallback_mon['top']),
            'width': int(fallback_mon['width']), 'height': int(fallback_mon['height']),
        }
    margin = int(live.get('overlay_margin', 20))
    l, t, w, h = rect['left'], rect['top'], rect['width'], rect['height']
    if pos == 'custom':
        rx = float(live.get('overlay_x_ratio', 0.5))
        ry = float(live.get('overlay_y_ratio', 0.12))
        x = int(l + rx * w - width / 2)
        y = int(t + ry * h - height / 2)
    else:
        mapping = {
            'top_left': (l + margin, t + margin),
            'top_center': (l + (w - width) // 2, t + margin),
            'top_right': (l + w - width - margin, t + margin),
            'bottom_left': (l + margin, t + h - height - margin),
            'bottom_center': (l + (w - width) // 2, t + h - height - margin),
            'bottom_right': (l + w - width - margin, t + h - height - margin),
        }
        x, y = mapping.get(pos, mapping['top_center'])
    x = clamp(int(x), l, max(l, l + w - width))
    y = clamp(int(y), t, max(t, t + h - height))
    return x, y


from live_ocr_log import new_row, write_log, filter_session_to_stop
from performance_mode import LiveResourceTracker
from capture_geometry import rectangle, session_rectangle, write_comparison

class ContinuousLiveEstimator(LiveEstimator):
    """Persistent live OCR/DPS monitor.

    Nothing is persisted while idle. When the GUI sends START_SESSION, per-event
    diagnostics and live-DPS samples are buffered in RAM for that recording only.
    STOP_SESSION freezes the recording boundary; FLUSH_SESSION writes the buffers.
    """

    def __init__(self, no_overlay=False, parent_pid=None):
        # Use HERE only as a harmless placeholder; all base diagnostic paths are
        # redirected to the OS null device below.
        super().__init__(HERE, no_overlay=no_overlay)
        null = Path(os.devnull)
        self.clock_path = HERE / '_continuous_clock_unused.json'
        self.stop_path = HERE / '_continuous_stop_unused.flag'
        try:
            self.stop_path.unlink(missing_ok=True)
        except Exception:
            pass
        self.live_path = null
        self.scan_path = null
        self.track_path = null
        self.overlay_error_path = null
        self.live_event_path = null

        self.session_lock = threading.RLock()
        self.session_path = None
        self.session_start_epoch = None
        self.session_start_elapsed = None
        self.session_stop_elapsed = None
        self.session_collecting = False
        self.session_live_event_rows = []
        self.session_live_samples = []
        self.session_ocr_rows = []
        # A short pre-session ring prevents lost OCR lines around START_SESSION.
        self.ocr_recent_rows = deque(maxlen=1024 if self.performance_mode == 'light' else 20000)
        self.session_resource_tracker = None
        self.focus_pause_started_epoch = None
        self.session_focus_intervals = []
        self.session_ocr_dropped_at_start = 0
        self.session_events = deque()  # (relative elapsed, amount)
        self.session_raw_total = 0
        self.session_final_total = 0
        self.session_snapshot = {}
        self.last_session_path = None
        self.last_ocr_elapsed_processed = -1e30
        # OCR results older than this boundary are allowed to finish but may not
        # enter the rolling live metrics. This prevents pre-recording backlog from
        # appearing as a sudden DPS/APM spike when Pause/Break is pressed.
        self.live_accept_after_elapsed = -1e30

        self.result_dps = None
        self.result_remaining_seconds = 0.0
        self.result_last_visible_tick = None
        self.last_position_check = 0.0
        self.result_display_seconds = float(self.live_cfg.get('result_display_seconds', 10.0))
        self.control_thread = None
        self.parent_pid = int(parent_pid) if parent_pid else None
        self.parent_watch_thread = None
        self.session_geometry_written_for = None
        self.session_geometry_observed = None
        self.session_capture_invalid = False
        self.capture_change_during_session = False
        self.zone_log_hidden = False
        self.zone_hidden_count = 0
        self.zone_resume_count = 0
        # A bounded metadata-only file remains available even when no
        # session is being recorded (the original zoning bug occurs idle).
        self.zone_event_path = HERE / 'live_zone_diagnostics.jsonl'
        self.zone_event_lock = threading.Lock()
        self.zone_recent_events = deque(maxlen=256)

    def on_log_visibility_transition(self, event, elapsed, details):
        epoch = time.time()
        self.zone_log_hidden = event == 'hidden'
        if event == 'hidden':
            self.zone_hidden_count += 1
        elif event == 'resumed':
            self.zone_resume_count += 1
        dps, window_damage, _apm, _attacks = self.current_metrics(elapsed)
        row = {
            'utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(epoch)),
            'wall_epoch': round(epoch, 6),
            'elapsed_seconds': round(float(elapsed), 6),
            'event': str(event),
            'reason': str(details.get('reason', '')),
            'dark_fraction': details.get('dark_fraction', ''),
            'ink_fraction': details.get('ink_fraction', ''),
            'left_ink_fraction': details.get('left_ink_fraction', ''),
            'midtone_fraction': details.get('midtone_fraction', ''),
            'scene_change': details.get('scene_change', ''),
            'rolling_dps': round(dps, 3),
            'rolling_damage': int(window_damage),
            'parsed_damage_events_total': int(self.parsed_events_total),
            'recording': bool(self.session_collecting),
        }
        with self.zone_event_lock:
            self.zone_recent_events.append(row)
            try:
                # Rotate at 256 KiB so an always-on live helper does not
                # accumulate an unlimited idle-session log.
                if self.zone_event_path.exists() and self.zone_event_path.stat().st_size > 256 * 1024:
                    self.zone_event_path.replace(HERE / 'live_zone_diagnostics.previous.jsonl')
                with self.zone_event_path.open('a', encoding='utf-8') as f:
                    f.write(json.dumps(row, ensure_ascii=False) + '\n')
            except OSError:
                # Disk access must not stop capture or live DPS.
                pass

    def on_capture_geometry(self, region, bgra, gray):
        with self.session_lock:
            path = self.session_path
            if path is None or self.session_geometry_written_for == path:
                return
            expected = session_rectangle(path)
            observed = {
                'actual_live_capture': rectangle(region),
                'actual_bgra_width': int(bgra.shape[1]),
                'actual_bgra_height': int(bgra.shape[0]),
                'actual_gray_width': int(gray.shape[1]),
                'actual_gray_height': int(gray.shape[0]),
                'first_capture_epoch': time.time(),
            }
            (path / 'live_capture_geometry.json').write_text(
                json.dumps(observed, indent=2), encoding='utf-8')
            comparison = write_comparison(
                path, expected, region, (gray.shape[1], gray.shape[0]),
                stage='first_live_mss_grab')
            self.session_capture_invalid = not comparison['match']
            self.session_geometry_observed = observed
            self.session_geometry_written_for = path

    def write_status(self, **updates):
        if updates.get('capture_change_during_session'):
            self.capture_change_during_session = True
        if 'capture_paused_no_ddo_focus' not in updates:
            return
        epoch = time.time()
        with self.session_lock:
            if updates['capture_paused_no_ddo_focus']:
                if self.focus_pause_started_epoch is None:
                    self.focus_pause_started_epoch = epoch
            elif self.focus_pause_started_epoch is not None:
                if self.session_start_epoch is not None and self.session_path is not None:
                    self.session_focus_intervals.append((self.focus_pause_started_epoch, epoch))
                self.focus_pause_started_epoch = None

    def wait_for_clock(self):
        self.start_epoch = time.time()
        return True

    def _session_relative_elapsed(self, continuous_elapsed):
        if self.session_start_elapsed is None:
            return None
        return float(continuous_elapsed) - float(self.session_start_elapsed)

    def _event_in_session(self, event_elapsed):
        rel = self._session_relative_elapsed(event_elapsed)
        if rel is None or rel < -0.05:
            return False, rel
        stop = self.session_stop_elapsed
        # A crop detected after STOP is not part of the recorded fight.  The old
        # 50-ms grace period let post-Stop hits enter live while they were absent
        # from FFV1 (seen at 26.403s vs recording end 26.386s).
        if stop is not None and float(event_elapsed) > float(stop) + 1e-6:
            return False, rel
        return self.session_path is not None, rel

    def record_live_damage_event(
        self, event_elapsed, detected_elapsed, raw_amount, final_amount,
        correction, recheck_result, verification_amount,
        batch_ocr_text, verification_ocr_text,
    ):
        raw_amount = int(raw_amount)
        final_amount = int(final_amount)
        with self.session_lock:
            ok, rel = self._event_in_session(event_elapsed)
            if not ok:
                return
            detected_rel = self._session_relative_elapsed(detected_elapsed)
            self.session_raw_total += raw_amount
            self.session_final_total += final_amount
            n = len(self.session_live_event_rows) + 1
            self.session_live_event_rows.append((
                n, max(0.0, float(rel)), max(0.0, float(detected_rel or 0.0)),
                raw_amount, final_amount,
                self.session_raw_total, self.session_final_total,
                correction or '', recheck_result or '',
                verification_amount if verification_amount is not None else '',
                batch_ocr_text or '', verification_ocr_text or '',
            ))
            self.session_events.append((max(0.0, float(rel)), final_amount))

    def record_live_ocr_line(self, elapsed, raw, effective, amount,
                             numeric_recheck, numeric_recheck_amount, event_elapsed):
        # The persistent helper must not accumulate unbounded OCR history.
        # Only rows inside the exact recording clock bounds enter a session.
        row = new_row(elapsed, self.start_epoch, raw, effective, amount,
                      numeric_recheck, numeric_recheck_amount, event_elapsed)
        with self.session_lock:
            self.ocr_recent_rows.append(row)
            if self.session_path is not None and self.session_start_elapsed is not None:
                ok, rel = self._event_in_session(elapsed)
                if ok:
                    record = dict(row)
                    record['capture_elapsed_seconds'] = max(0.0, rel)
                    if record.get('event_capture_elapsed_seconds') != '':
                        record['event_capture_elapsed_seconds'] = max(
                            0.0, float(record['event_capture_elapsed_seconds']) - self.session_start_elapsed)
                    self.session_ocr_rows.append(record)

    def write_live_ocr_log(self):
        # Continuous sessions are flushed on FLUSH_SESSION, never at worker exit.
        return

    def write_live_damage_events(self):
        # Base ocr_worker calls this only at process shutdown. Session flushing
        # is explicitly controlled by FLUSH_SESSION, so do not write idle data.
        return

    def _set_session_boundary(self, epoch_text):
        try:
            boundary_epoch = float(epoch_text)
            cutoff = boundary_epoch - float(self.start_epoch)
        except Exception:
            return

        # Only move the boundary forward. A late/repeated command is harmless.
        self.live_accept_after_elapsed = max(float(self.live_accept_after_elapsed), float(cutoff))
        self.pending_damage_wrap = None
        self.last_bottom_flush_event = None
        # Never carry a clipped bottom row from idle/pre-session capture into a
        # freshly started recording.
        self.clear_pending_bottom_line(count_drop=False)

        # Remove queued line crops detected before the boundary. The OCR worker
        # may already be processing one old batch; those rows are filtered again
        # when their parsed events are about to enter the rolling metrics.
        kept = []
        while True:
            try:
                item = self.ocr_queue.get_nowait()
            except queue.Empty:
                break
            try:
                if float(item[0]) + 1e-6 >= cutoff:
                    kept.append(item)
            finally:
                self.ocr_queue.task_done()
        for item in kept:
            try:
                self.ocr_queue.put_nowait(item)
            except queue.Full:
                self.dropped_lines_total += 1

    def _preflight_capture(self, path_text):
        """Confirm live geometry BEFORE the independent recorder receives START."""
        path = Path(path_text).resolve()
        try:
            expected = session_rectangle(path)
            self.capture_applied_event.clear()
            self.capture_reload_requested.set()
            reloaded = self.capture_applied_event.wait(timeout=5.0)
            actual = rectangle(self.capture)
            result = write_comparison(path, expected, actual, stage='recorder_ready_preflight')
            result['ready'] = bool(reloaded and result['match'])
            if not reloaded:
                result['error'] = 'Live capture thread did not acknowledge geometry reload.'
            elif not result['match']:
                result['error'] = 'Live and recorder use different Combat Log areas.'
        except Exception as exc:
            result = {'ready': False, 'error': f'{type(exc).__name__}: {exc}'}
        try:
            target = path / 'live_capture_preflight.json'
            tmp = target.with_suffix('.json.tmp')
            tmp.write_text(json.dumps(result, indent=2), encoding='utf-8')
            tmp.replace(target)
        except Exception:
            pass

    def _start_session(self, path_text):
        path = Path(path_text).resolve()
        clock = path / 'recording_clock.json'
        deadline = time.time() + 3.0
        data = None
        while time.time() < deadline:
            try:
                data = json.loads(clock.read_text(encoding='utf-8'))
                break
            except Exception:
                time.sleep(0.03)
        if not data:
            return
        start_epoch = float(data['start_epoch'])
        try:
            expected = session_rectangle(path)
        except Exception as exc:
            (path / 'live_capture_geometry_error.txt').write_text(
                'No valid recorder capture geometry: ' + str(exc), encoding='utf-8')
            return
        if rectangle(self.capture) != expected:
            # The capture loop owns its MSS region. Ask it to reload and wait
            # for the new coordinates; changing self.capture here would NOT
            # change its local rectangle and would silently fake a match.
            self.capture_applied_event.clear()
            self.capture_reload_requested.set()
            self.capture_applied_event.wait(timeout=2.0)
        if rectangle(self.capture) != expected:
            write_comparison(path, expected, self.capture, stage='start_session_rejected')
            (path / 'live_capture_geometry_error.txt').write_text(
                'Live capture differs from the recorder; refused to attach live '
                'data. Select the log area again or restart the app.', encoding='utf-8')
            self.session_capture_invalid = True
            return
        write_comparison(path, expected, self.capture, stage='start_session_config_check')
        self.session_geometry_written_for = None
        self.session_geometry_observed = None
        self.session_capture_invalid = False
        self.capture_change_during_session = False
        self._set_session_boundary(start_epoch)
        with self.session_lock:
            # A continuous live monitor starts before the first combat.
            # Attach diagnostics to THIS recording only, preserving the
            # exact live-MSS and Indra inputs until the session is flushed.
            if self.performance_mode == 'light':
                self.capture_diagnostics = None
            else:
                try:
                    self.capture_diagnostics = LiveInputDiagnostics(
                        path, self.capture, start_epoch, self.start_epoch,
                        self.indra_threads, self.ocr_batch_max,
                        self.tracking_fps, indra_module=__import__('indra_ocr'))
                except Exception as exc:
                    self.capture_diagnostics = None
                    try:
                        (path / 'live_diagnostic_init_error.txt').write_text(
                            str(exc), encoding='utf-8')
                    except Exception:
                        pass
            self.session_resource_tracker = LiveResourceTracker()
            self.session_focus_intervals = []
            self.session_path = path
            self.last_session_path = path
            self.session_start_epoch = start_epoch
            self.session_start_elapsed = start_epoch - float(self.start_epoch)
            self.session_stop_elapsed = None
            self.session_collecting = True
            self.session_live_event_rows = []
            self.session_live_samples = []
            self.session_ocr_rows = []
            self.session_ocr_dropped_at_start = self.dropped_lines_total
            # OCR from the capture may have completed before START_SESSION was
            # delivered by the GUI. Recover these already-recognized rows.
            for old_row in self.ocr_recent_rows:
                offset = float(old_row['capture_wall_epoch']) - start_epoch
                if offset >= -0.02:
                    recovered = dict(old_row)
                    recovered['capture_elapsed_seconds'] = max(0.0, offset)
                    if recovered.get('event_capture_elapsed_seconds') != '':
                        recovered['event_capture_elapsed_seconds'] = max(
                            0.0, float(recovered['event_capture_elapsed_seconds']) - self.session_start_elapsed)
                    self.session_ocr_rows.append(recovered)
            self.session_events = deque()
            self.session_raw_total = 0
            self.session_final_total = 0
            self.session_snapshot = {
                'parsed': self.parsed_events_total,
                'detected': self.detected_lines_total,
                'ocr': self.ocr_lines_total,
                'dropped': self.dropped_lines_total,
                'attacks': self.parsed_attacks_total,
                'tracking_frames': self.tracking_frames,
                'phase_corrected_scrolls': self.phase_corrected_scrolls,
                'anchor_phase_timeout_rebases': self.anchor_phase_timeout_rebases,
                'robust_fallback_overrides': self.robust_fallback_overrides,
                'anchor_linewise_overrides': self.anchor_linewise_overrides,
                'anchor_previous_frame_checks': self.anchor_previous_frame_checks,
                'anchor_previous_frame_recoveries': self.anchor_previous_frame_recoveries,
                'anchor_stall_warnings': self.anchor_stall_warnings,
                'anchor_unresolved_gaps': self.anchor_unresolved_gaps,
                'anchor_hold_streak_max': self.anchor_hold_streak_max,
                'capture_refreshes': self.capture_refreshes,
                'zone_hidden_count': self.zone_hidden_count,
                'zone_resume_count': self.zone_resume_count,
            }

    def _stop_session(self, stop_epoch_text=''):
        with self.session_lock:
            if self.session_path is None:
                return
            try:
                stop_epoch = float(stop_epoch_text) if stop_epoch_text else time.time()
            except Exception:
                stop_epoch = time.time()
            self.session_stop_elapsed = stop_epoch - float(self.start_epoch)
            self.session_collecting = False
            stop_elapsed = self.session_stop_elapsed
        diag = self.capture_diagnostics
        if diag is not None:
            try:
                diag.mark_stop(stop_elapsed)
            except Exception:
                pass
        # If the newest combat row is still clipped when Stop is pressed, there
        # may never be a later scroll frame that exposes it fully. Queue that one
        # fragment now; its original capture timestamp remains before the stop
        # boundary and the OCR worker applies only the narrow EOF damage repair.
        self.flush_pending_bottom_line(max_elapsed=stop_elapsed)

    def _flush_session(self):
        # Catch a pending pre-stop row that may have been deferred by the tracking
        # thread just after STOP_SESSION was processed.
        with self.session_lock:
            boundary_for_bottom = self.session_stop_elapsed
        if boundary_for_bottom is not None:
            self.flush_pending_bottom_line(max_elapsed=boundary_for_bottom)

        # If Stop forced the final clipped row into OCR, wait specifically for
        # that queue item to finish before snapshotting the session buffers.
        # This avoids a race where qsize() reaches zero after the worker takes
        # the item but before its parsed damage has been recorded.
        bottom_flush_event = self.last_bottom_flush_event
        if bottom_flush_event is not None:
            bottom_flush_event.wait(timeout=3.0)

        # Give asynchronous OCR a chance to consume all line crops captured at
        # or before the recording stop boundary. New post-stop lines do not
        # enter the session buffer because _event_in_session checks the boundary.
        deadline = time.time() + 5.0
        session_ocr_drained = False
        while time.time() < deadline:
            with self.session_lock:
                boundary = self.session_stop_elapsed
            # The last OCR row is not necessarily near Stop. Instead inspect
            # pending/in-flight capture timestamps and wait only for pre-stop
            # work, never for the continuous after-Stop DDO activity.
            with self.ocr_queue.mutex:
                waiting_pre_stop = any(float(item[0]) <= boundary + 1e-6
                                       for item in self.ocr_queue.queue) if boundary is not None else False
            inflight_pre_stop = any(float(x) <= boundary + 1e-6
                                    for x in self.ocr_batch_inflight) if boundary is not None else False
            if not waiting_pre_stop and not inflight_pre_stop:
                session_ocr_drained = True
                break
            time.sleep(0.04)
        with self.session_lock:
            path = self.session_path or self.last_session_path
            if path is None:
                return
            event_rows = list(self.session_live_event_rows)
            live_rows = list(self.session_live_samples)
            ocr_rows = list(self.session_ocr_rows)
            # STOP_SESSION can arrive a little later than the GUI's STOP
            # button. Apply its authoritative, transmitted timestamp again
            # here before writing, removing any already-buffered late rows.
            stop_rel = (self.session_stop_elapsed - self.session_start_elapsed
                        if self.session_stop_elapsed is not None
                        and self.session_start_elapsed is not None else None)
            event_rows, ocr_rows, live_rows, stop_filter_stats = filter_session_to_stop(
                event_rows, ocr_rows, live_rows, stop_rel)
            dropped_during = max(0, self.dropped_lines_total - self.session_ocr_dropped_at_start)
            started_epoch = self.session_start_epoch
            stopped_epoch = self.start_epoch + self.session_stop_elapsed if self.session_stop_elapsed is not None else None
            focus_windows = list(self.session_focus_intervals)
            with self.zone_event_lock:
                # Preserve some context before START_SESSION as well. The
                # ongoing idle log remains in _internal even after Stop.
                zone_events = [dict(row) for row in self.zone_recent_events
                               if float(row['wall_epoch']) >= float(started_epoch or 0) - 30.0
                               and float(row['wall_epoch']) <= float(stopped_epoch or time.time())]
            if self.focus_pause_started_epoch is not None and stopped_epoch is not None:
                focus_windows.append((self.focus_pause_started_epoch, stopped_epoch))
            start_tracking = dict(self.session_snapshot)
            tracker_counts = {
                key: max(0, int(getattr(self, key, 0)) - int(start_tracking.get(key, 0)))
                for key in ('tracking_frames', 'phase_corrected_scrolls',
                            'anchor_phase_timeout_rebases', 'robust_fallback_overrides',
                            'anchor_linewise_overrides', 'anchor_previous_frame_checks',
                            'anchor_previous_frame_recoveries', 'anchor_stall_warnings',
                            'anchor_unresolved_gaps', 'capture_refreshes',
                            'zone_hidden_count', 'zone_resume_count')
            }
            tracker_counts['anchor_hold_streak_max'] = int(self.anchor_hold_streak_max)
            tracker_counts['capture_change_during_session'] = bool(self.capture_change_during_session)
            tracker_counts['capture_geometry_valid'] = not bool(self.session_capture_invalid)
            if not self.session_geometry_observed:
                tracker_counts['capture_geometry_verified_by_mss'] = False
            else:
                tracker_counts['capture_geometry_verified_by_mss'] = True
            recording_length = max(0.0, (stopped_epoch or 0.0) - (started_epoch or 0.0))
            last_ocr = max((float(r.get('capture_elapsed_seconds', 0.0)) for r in ocr_rows),
                           default=None)
            tracker_counts.update({
                'stop_boundary_excluded_events': stop_filter_stats['excluded_events'],
                'stop_boundary_excluded_ocr_rows': stop_filter_stats['excluded_ocr_rows'],
                'ocr_rows': len(ocr_rows),
                'ocr_queue_drained': session_ocr_drained,
                'dropped_ocr_crops': dropped_during,
                'recording_duration_seconds': round(recording_length, 3),
                'last_live_ocr_at_seconds': round(last_ocr, 3) if last_ocr is not None else None,
                'recording_seconds_after_last_live_ocr': (
                    round(max(0.0, recording_length - last_ocr), 3)
                    if last_ocr is not None else None),
            })

        path.mkdir(parents=True, exist_ok=True)
        if self.performance_mode == 'light':
            focus_rows = []
            for began, ended in focus_windows:
                start = max(float(began), float(started_epoch or began))
                finish = min(float(ended), float(stopped_epoch or ended))
                if finish > start:
                    focus_rows.append((start - float(started_epoch or start),
                                       finish - float(started_epoch or start), finish - start))
            with (path / 'focus_interruptions.csv').open('w', newline='', encoding='utf-8') as stream:
                writer = csv.writer(stream)
                writer.writerow(['start_elapsed_seconds', 'end_elapsed_seconds',
                                 'duration_seconds', 'resume_saved_frame'])
                for a, b, duration in focus_rows:
                    writer.writerow([f'{a:.6f}', f'{b:.6f}', f'{duration:.6f}', ''])
            tracker_counts['focus_interruptions'] = len(focus_rows)
            tracker_counts['focus_interruption_seconds'] = round(sum(r[2] for r in focus_rows), 4)
        # Small metadata-only diagnostics, shipped in the session ZIP when a
        # session is recorded across zoning; no image/text capture required.
        with (path / 'live_zone_events.csv').open('w', newline='', encoding='utf-8') as file:
            columns = ['utc', 'wall_epoch', 'elapsed_seconds', 'event', 'reason',
                       'dark_fraction', 'ink_fraction', 'left_ink_fraction',
                       'midtone_fraction', 'scene_change', 'rolling_dps',
                       'rolling_damage', 'parsed_damage_events_total', 'recording']
            writer = csv.DictWriter(file, fieldnames=columns, extrasaction='ignore')
            writer.writeheader()
            writer.writerows(zone_events)
        tracker_counts['zone_visibility_events_saved'] = len(zone_events)
        tracker_counts['zone_log_hidden_at_flush'] = bool(self.zone_log_hidden)
        (path / 'live_tracking_quality.json').write_text(
            json.dumps(tracker_counts, indent=2), encoding='utf-8')
        if self.session_resource_tracker is not None:
            try:
                metrics = self.session_resource_tracker.finish(
                    mode=self.performance_mode,
                    fps=self.tracking_fps, threads=self.indra_threads,
                    dropped_crops=dropped_during, ocr_rows=len(ocr_rows),
                    drained=session_ocr_drained)
                (path / 'live_resource_usage.json').write_text(
                    json.dumps(metrics, indent=2), encoding='utf-8')
            except Exception:
                pass
            self.session_resource_tracker = None
        event_path = path / 'live_damage_events.csv'
        with event_path.open('w', newline='', encoding='utf-8') as f:
            w = csv.writer(f)
            w.writerow([
                'event_number', 'event_capture_elapsed_seconds',
                'detected_capture_elapsed_seconds', 'raw_damage', 'final_damage',
                'cumulative_raw_damage', 'cumulative_final_damage',
                'correction_applied', 'numeric_recheck_result',
                'verification_damage', 'batch_ocr_text', 'verification_ocr_text',
            ])
            for row in event_rows:
                row = list(row)
                row[1] = f'{float(row[1]):.6f}'
                row[2] = f'{float(row[2]):.6f}'
                if self.performance_mode == 'light':
                    row[11] = ''
                w.writerow(row)

        live_path = path / 'live_dps.csv'
        with live_path.open('w', newline='', encoding='utf-8') as f:
            w = csv.writer(f)
            w.writerow([
                'capture_elapsed_seconds', 'wall_time_epoch', 'live_rolling_dps',
                'window_seconds', 'window_damage', 'parsed_events_total', 'detected_lines_total',
                'ocr_lines_total', 'ocr_queue_depth', 'dropped_lines_total',
                'live_rolling_apm', 'window_attacks', 'parsed_attacks_total',
            ])
            w.writerows(live_rows)

        # Publish a readiness marker only after both the full recognized-line
        # log and all live metrics are flushed. Fast post-analysis waits for it.
        write_log(path, ocr_rows, complete=session_ocr_drained,
                  dropped=dropped_during, started_epoch=started_epoch,
                  stopped_epoch=stopped_epoch,
                  note=('Pre-stop OCR queue drained' if session_ocr_drained else
                        'Pre-stop OCR backlog remained after 5s; quick results may be incomplete'))
        # Stop the bounded diagnostic capture only after pre-stop OCR has
        # been drained. Its write errors cannot break the recording.
        diag = self.capture_diagnostics
        self.capture_diagnostics = None
        if diag is not None:
            try:
                diag.finish({
                    'session_ocr_drained': session_ocr_drained,
                    'session_damage_events': len(event_rows),
                    'session_ocr_rows': len(ocr_rows),
                    'session_dropped_crops': dropped_during,
                    'session_recording_seconds': recording_length,
                })
            except Exception as exc:
                try:
                    (path / 'live_diagnostic_finish_error.txt').write_text(
                        str(exc), encoding='utf-8')
                except Exception:
                    pass

    def _record_session_sample(self, continuous_dps, continuous_apm, qdepth):
        with self.session_lock:
            if self.session_resource_tracker is not None:
                self.session_resource_tracker.sample()
            if self.session_path is None or self.session_start_elapsed is None:
                return
            now_cont = time.time() - float(self.start_epoch)
            if self.session_stop_elapsed is not None and now_cont > self.session_stop_elapsed + 0.25:
                return
            rel = max(0.0, now_cont - self.session_start_elapsed)
            cutoff = rel - self.window_seconds
            while self.session_events and self.session_events[0][0] <= cutoff:
                self.session_events.popleft()
            window_damage = sum(a for _, a in self.session_events)
            session_dps = window_damage / self.window_seconds
            snap = self.session_snapshot
            parsed = len(self.session_live_event_rows)
            detected = max(0, self.detected_lines_total - int(snap.get('detected', 0)))
            ocr = max(0, self.ocr_lines_total - int(snap.get('ocr', 0)))
            dropped = max(0, self.dropped_lines_total - int(snap.get('dropped', 0)))
            attacks = max(0, self.parsed_attacks_total - int(snap.get('attacks', 0)))
            # APM is primarily an overlay convenience. Keep the continuously
            # calculated value here; DPS is session-pure for graph comparison.
            self.session_live_samples.append([
                f'{rel:.6f}', f'{time.time():.6f}', f'{session_dps:.3f}',
                f'{self.window_seconds:g}', int(window_damage), parsed, detected, ocr, int(qdepth), dropped,
                f'{float(continuous_apm):.3f}', '', attacks,
            ])

    def _reload_config_and_position(self):
        try:
            cfg = json.loads(CONFIG.read_text(encoding='utf-8'))
            self.cfg = cfg
            self.live_cfg = cfg.get('live_dps', {})
            self.result_display_seconds = float(self.live_cfg.get('result_display_seconds', 10.0))
            self.window_seconds = max(1.0, float(self.live_cfg.get('rolling_window_seconds', 15.0)))
            self.history_seconds = max(60.0, self.window_seconds)
            if self.root is not None:
                width = int(self.live_cfg.get('overlay_width', 560))
                height = int(self.live_cfg.get('overlay_height', 190))
                with mss.MSS() as sct:
                    mon = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
                x, y = overlay_xy(cfg, width, height, mon)
                self.root.geometry(f'{width}x{height}{x:+d}{y:+d}')
                if self.overlay_canvas is not None:
                    font_size = int(self.live_cfg.get('font_size', 24))
                    self.overlay_canvas.itemconfigure(
                        self.value_item, font=('Segoe UI', font_size, 'bold'))
                    self.overlay_canvas.itemconfigure(
                        self.apm_item, font=('Segoe UI', max(18, int(font_size * 0.60)), 'bold'))
                    self.overlay_canvas.itemconfigure(
                        self.result_item, font=('Segoe UI', max(16, int(font_size * 0.55)), 'bold'))
        except Exception:
            pass

    def _follow_ddo_window(self):
        if self.root is None:
            return
        now = time.monotonic()
        if now - self.last_position_check < 0.5:
            return
        self.last_position_check = now
        try:
            width = int(self.live_cfg.get('overlay_width', 560))
            height = int(self.live_cfg.get('overlay_height', 190))
            with mss.MSS() as sct:
                mon = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
            x, y = overlay_xy(self.cfg, width, height, mon)
            self.root.geometry(f'{width}x{height}{x:+d}{y:+d}')
        except Exception:
            pass

    def parent_watchdog(self):
        """Stop this helper when the GUI process that created it exits.

        This waits on a Windows process handle. It never scans, terminates,
        injects into, or otherwise modifies unrelated processes.
        """
        if os.name != 'nt' or not self.parent_pid:
            return
        handle = None
        try:
            SYNCHRONIZE = 0x00100000
            INFINITE = 0xFFFFFFFF
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_bool, ctypes.c_ulong]
            kernel32.OpenProcess.restype = ctypes.c_void_p
            kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
            kernel32.WaitForSingleObject.restype = ctypes.c_ulong
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel32.CloseHandle.restype = ctypes.c_bool
            handle = kernel32.OpenProcess(SYNCHRONIZE, False, int(self.parent_pid))
            if not handle:
                self.stop_event.set()
                self.update_queue.put(('done',))
                return
            kernel32.WaitForSingleObject(handle, INFINITE)
            self.stop_event.set()
            self.update_queue.put(('done',))
        except Exception:
            pass
        finally:
            if handle:
                try:
                    kernel32.CloseHandle(handle)
                except Exception:
                    pass

    def control_worker(self):
        try:
            for raw in sys.stdin:
                line = raw.strip()
                if not line:
                    continue
                if line.startswith('SESSION_BOUNDARY|'):
                    self._set_session_boundary(line.split('|', 1)[1])
                elif line.startswith('PREPARE_CAPTURE|'):
                    self._preflight_capture(line.split('|', 1)[1])
                elif line.startswith('START_SESSION|'):
                    self._start_session(line.split('|', 1)[1])
                elif line.startswith('STOP_SESSION'):
                    parts = line.split('|', 1)
                    self._stop_session(parts[1] if len(parts) > 1 else '')
                elif line == 'FLUSH_SESSION':
                    self._flush_session()
                elif line.startswith('RESULT|'):
                    parts = line.split('|')
                    try:
                        self.result_dps = float(parts[1])
                        seconds = float(parts[2]) if len(parts) > 2 else self.result_display_seconds
                        # Count only time during which the result is actually
                        # visible over foreground DDO. If analysis finishes while
                        # the user is Alt-Tabbed away, the full result duration is
                        # still available when they return.
                        self.result_remaining_seconds = max(1.0, seconds)
                        self.result_last_visible_tick = None
                    except Exception:
                        pass
                elif line == 'RELOAD_CONFIG':
                    self.capture_reload_requested.set()
                    self.update_queue.put(('reload_config',))
                elif line == 'SHUTDOWN':
                    self.stop_event.set()
                    break
        finally:
            # If the GUI exits/crashes, its stdin pipe closes. Treat EOF as a
            # clean shutdown signal instead of leaving an orphan live overlay.
            if not self.stop_event.is_set():
                self.stop_event.set()
                try:
                    self.update_queue.put(('done',))
                except Exception:
                    pass

    def setup_overlay(self):
        if not self.overlay_enabled:
            return
        self.root = tk.Tk()
        self.root.title('DDO Combat Analyzer - Live DPS')
        self.root.overrideredirect(True)
        transparent = '#010203'
        self.root.configure(bg=transparent)
        self.root.attributes('-topmost', True)

        transparent_ok = False
        if os.name == 'nt':
            try:
                self.root.wm_attributes('-transparentcolor', transparent)
                transparent_ok = True
            except Exception:
                pass
        if not transparent_ok:
            self.root.configure(bg='#101214')
            try:
                self.root.attributes('-alpha', 0.90)
            except Exception:
                pass

        width = int(self.live_cfg.get('overlay_width', 560))
        height = int(self.live_cfg.get('overlay_height', 190))
        with mss.MSS() as sct:
            mon = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
        x, y = overlay_xy(self.cfg, width, height, mon)
        self.root.geometry(f'{width}x{height}{x:+d}{y:+d}')

        bg = transparent if transparent_ok else '#101214'
        self.overlay_canvas = tk.Canvas(self.root, width=width, height=height, bg=bg,
                                        highlightthickness=0, bd=0, relief='flat')
        self.overlay_canvas.pack(fill='both', expand=True)
        font_size = int(self.live_cfg.get('font_size', 24))
        self.value_item = self.overlay_canvas.create_text(
            width // 2, 42, text='0 DPS', fill='#ff3030',
            font=('Segoe UI', font_size, 'bold'), anchor='center')
        self.apm_item = self.overlay_canvas.create_text(
            width // 2, 92, text='0 APM', fill='#ff5050',
            font=('Segoe UI', max(18, int(font_size * 0.60)), 'bold'), anchor='center')
        self.result_item = self.overlay_canvas.create_text(
            width // 2, 132, text='', fill='#ffd060',
            font=('Segoe UI', max(16, int(font_size * 0.55)), 'bold'), anchor='center')
        self.detail_item = self.overlay_canvas.create_text(
            width // 2, 168, text=f'LIVE {self.window_seconds:g}s  |  HEJSA', fill='#ff9090',
            font=('Segoe UI', 10, 'bold'), anchor='center')

        self.root.update_idletasks()
        make_clickthrough(self.root)
        if os.name == 'nt':
            try:
                hwnd = self.root.winfo_id()
                ctypes.windll.user32.SetWindowPos(
                    hwnd, -1, 0, 0, 0, 0,
                    0x0001 | 0x0002 | 0x0010 | 0x0040)
            except Exception:
                pass

    def poll_updates(self):
        try:
            while True:
                msg = self.update_queue.get_nowait()
                kind = msg[0]
                if kind == 'metrics':
                    _, dps, apm, window_damage, window_attacks, total_events, total_attacks, qdepth = msg
                    self._record_session_sample(dps, apm, qdepth)
                    if self.root is not None and self.overlay_canvas is not None:
                        self.overlay_canvas.itemconfigure(self.value_item, text=f'{int(round(dps)):,} DPS')
                        self.overlay_canvas.itemconfigure(self.apm_item, text=f'{int(round(apm))} APM')
                        self.overlay_canvas.itemconfigure(
                            self.detail_item,
                            text=(f'LIVE {self.window_seconds:g}s  |  HEJSA  |  '
                                  f'{"LOG HIDDEN" if self.zone_log_hidden else f"queue {qdepth}"}'))
                elif kind == 'reload_config':
                    self._reload_config_and_position()
                elif kind == 'done':
                    if self.root is not None:
                        self.root.destroy()
                    return
                elif kind == 'error':
                    if self.root is not None and self.overlay_canvas is not None:
                        self.overlay_canvas.itemconfigure(self.value_item, text='NO LIVE DPS')
        except queue.Empty:
            pass

        if self.root is not None and self.overlay_canvas is not None:
            foreground = is_ddo_foreground()
            try:
                if foreground:
                    if str(self.root.state()) == 'withdrawn':
                        self.root.deiconify()
                        self.root.attributes('-topmost', True)
                    self._follow_ddo_window()
                else:
                    if str(self.root.state()) != 'withdrawn':
                        self.root.withdraw()
            except Exception:
                pass

            if foreground:
                now_visible = time.monotonic()
                if self.result_dps is not None and self.result_remaining_seconds > 0.0:
                    if self.result_last_visible_tick is not None:
                        self.result_remaining_seconds = max(
                            0.0,
                            self.result_remaining_seconds - (now_visible - self.result_last_visible_tick),
                        )
                    self.result_last_visible_tick = now_visible
                    if self.result_remaining_seconds > 0.0:
                        self.overlay_canvas.itemconfigure(
                            self.result_item, text=f'SESSION DPS: {int(round(self.result_dps)):,}')
                    else:
                        self.overlay_canvas.itemconfigure(self.result_item, text='')
                        self.result_dps = None
                else:
                    self.overlay_canvas.itemconfigure(self.result_item, text='')
                    self.result_last_visible_tick = None
            else:
                # Pause the result-display countdown while the overlay is hidden.
                self.result_last_visible_tick = None
        if self.root is not None:
            self.root.after(80, self.poll_updates)

    def run(self):
        if self.parent_pid:
            self.parent_watch_thread = threading.Thread(
                target=self.parent_watchdog, name='parent-watchdog', daemon=True
            )
            self.parent_watch_thread.start()
        self.control_thread = threading.Thread(target=self.control_worker, name='live-control', daemon=True)
        self.control_thread.start()
        super().run()
        try:
            self._flush_session()
        except Exception:
            pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--no-overlay', action='store_true')
    ap.add_argument('--parent-pid', type=int, default=None)
    args = ap.parse_args()
    ContinuousLiveEstimator(no_overlay=args.no_overlay, parent_pid=args.parent_pid).run()


if __name__ == '__main__':
    main()
