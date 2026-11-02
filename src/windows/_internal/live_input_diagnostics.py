"""Bounded, session-scoped diagnostics for real-time DDO Combat Log OCR.

This is an opt-in *test build* helper, not part of damage parsing. It observes the
pixels actually returned to the live MSS process and the actual Indra batch OCR
input. Raw and preprocessed text-row samples remain in the combat session folder.
No whole-desktop screenshots, networking or in-game process access is used.
"""
from __future__ import annotations

import csv
import json
import threading
import time
from collections import Counter
from importlib import metadata
from pathlib import Path

import cv2
import numpy as np


class LiveInputDiagnostics:
    """Small, self-contained visual evidence collector for one recording."""

    MAX_ROW_SAMPLES = 18
    MAX_BATCH_SAMPLES = 6
    MAX_FRAME_SAMPLES = 3

    def __init__(self, session, capture, session_start_epoch, continuous_epoch,
                 indra_threads, batch_max, tracking_fps, indra_module=None):
        self.session = Path(session)
        self.folder = self.session / 'live_ocr_diagnostic'
        self.folder.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.active = True
        self.start_epoch = float(session_start_epoch)
        self.stop_seconds = None
        self.continuous_epoch = float(continuous_epoch)
        self.capture = {key: int(capture.get(key, 0))
                        for key in ('left', 'top', 'width', 'height')}
        self.indra_threads = int(indra_threads)
        self.batch_max = int(batch_max)
        self.tracking_fps = int(tracking_fps)
        self.frame_counter = 0
        self.batch_counter = 0
        self.ocr_row_counter = 0
        self.row_samples = []
        self.batch_rows = []
        self.row_geometry = []
        self.frame_geometry = []
        self.errors = []
        self.sample_bucket_counts = Counter()
        self.batch_sampled = set()
        self.batch_sampled_buckets = set()
        self.snapshot_seconds = set()
        self.ocr_version = 'not available'
        try:
            self.ocr_version = metadata.version('indra-ocr')
        except Exception:
            try:
                self.ocr_version = str(getattr(indra_module, '__version__', 'unknown'))
            except Exception:
                pass

    @staticmethod
    def _png(folder, name, array):
        ok, encoded = cv2.imencode('.png', array)
        if not ok:
            raise RuntimeError('OpenCV PNG encoder returned false')
        (folder / name).write_bytes(encoded.tobytes())

    def _seconds(self, elapsed):
        return float(elapsed) + self.continuous_epoch - self.start_epoch

    def mark_stop(self, continuous_elapsed):
        with self.lock:
            self.stop_seconds = self._seconds(continuous_elapsed)

    def _in_recording(self, seconds):
        return (seconds >= -0.01 and
                (self.stop_seconds is None or seconds <= self.stop_seconds + 0.00001))

    def _warning(self, exc):
        if len(self.errors) < 12:
            self.errors.append(f'{type(exc).__name__}: {exc}')

    def capture_frame(self, elapsed, screenshot, gray):
        """Record actual live MSS image dimensions (never the whole desktop)."""
        try:
            sec = self._seconds(elapsed)
            with self.lock:
                if not self.active or not self._in_recording(sec):
                    return
                self.frame_counter += 1
                # A single geometry row each second; ~60 records in a test run.
                sec_bucket = int(sec)
                if sec_bucket not in self.snapshot_seconds:
                    self.snapshot_seconds.add(sec_bucket)
                    self.frame_geometry.append({
                        'session_seconds': round(sec, 4),
                        'live_frame_number': int(self.frame_counter),
                        'requested_left': self.capture['left'],
                        'requested_top': self.capture['top'],
                        'requested_width': self.capture['width'],
                        'requested_height': self.capture['height'],
                        'mss_bgra_width': int(screenshot.shape[1]),
                        'mss_bgra_height': int(screenshot.shape[0]),
                        'mss_bgra_channels': (int(screenshot.shape[2])
                                               if screenshot.ndim > 2 else 1),
                        'gray_width': int(gray.shape[1]),
                        'gray_height': int(gray.shape[0]),
                    })
                # Exactly three bounded full-COMBAT-LOG region snapshots, never
                # an entire monitor. These provide reference pixels for width.
                sample_marks = (0, 15, 35)
                for n, t in enumerate(sample_marks, start=1):
                    key = f'frame-{n}'
                    if sec >= t and key not in self.batch_sampled:
                        self.batch_sampled.add(key)
                        self._png(self.folder, f'live_region_{n:02d}_gray.png', gray)
                        break
        except Exception as exc:
            with self.lock:
                self._warning(exc)

    @staticmethod
    def _boxes_by_slot(page, slots):
        boxes = [[] for _ in slots]
        for line in getattr(page, 'lines', []) or []:
            box = getattr(line, 'box', None)
            if box is None:
                continue
            try:
                x0, y0, x1, y1 = (float(box[i]) for i in range(4))
                cy = (y0 + y1) / 2.0
            except Exception:
                continue
            best = None
            best_dist = float('inf')
            for i, (top, bottom) in enumerate(slots):
                if top - 4 <= cy <= bottom + 4:
                    best = i
                    break
                dist = abs(cy - (top + bottom) / 2.0)
                if dist < best_dist:
                    best_dist, best = dist, i
            if best is not None:
                boxes[best].append((x0, y0, x1, y1))
        return boxes

    @staticmethod
    def _is_suspect(text):
        t = str(text or '').lower()
        return (('you hit ' in t or 'you sneak attack ' in t)
                and ' for ' in t and ('damage' not in t or len(t) <= 60)) or (
                    'confirm a critical' in t and 'critical hit' not in t
                    and 'critical miss' not in t and 'failed' not in t
                )

    def record_batch(self, items, canvas, slots, page, texts, batch_ms=0.0,
                     queue_depth=0):
        """Observe actual crop/canvas shapes and limited pixel samples.

        Called from the existing OCR worker *after* the real Indra call. Any
        diagnostics failure must not modify OCR results or stop combat tracking.
        """
        try:
            from live_overlay import preprocess_line_indra
            slot_boxes = self._boxes_by_slot(page, slots)
            with self.lock:
                if not self.active:
                    return
                if not any(self._in_recording(self._seconds(item[0])) for item in items):
                    return
                self.batch_counter += 1
                b = self.batch_counter
                start = self._seconds(items[0][0]) if items else 0.0
                row_entry = {
                    'batch_number': b,
                    'session_seconds_first': round(start, 4),
                    'batch_size': len(items),
                    'batch_image_width': int(canvas.shape[1]),
                    'batch_image_height': int(canvas.shape[0]),
                    'indra_line_objects': sum(len(x) for x in slot_boxes),
                    'queue_depth_after': int(queue_depth),
                    'batch_ocr_milliseconds': round(float(batch_ms), 2),
                }
                self.batch_rows.append(row_entry)
                for i, (item, text) in enumerate(zip(items, texts)):
                    seconds = self._seconds(item[0])
                    if not self._in_recording(seconds):
                        continue
                    self.ocr_row_counter += 1
                    crop = item[1]
                    h, w = crop.shape[:2]
                    obj = slot_boxes[i] if i < len(slot_boxes) else []
                    xmax = max((z[2] for z in obj), default=None)
                    xmin = min((z[0] for z in obj), default=None)
                    rec = {
                        'row_number': self.ocr_row_counter,
                        'batch_number': b,
                        'session_seconds': round(seconds, 4),
                        'row_in_batch': i + 1,
                        'batch_size': len(items),
                        'raw_width_px': int(w),
                        'raw_height_px': int(h),
                        'preprocessed_width_px': int(2 * (w + 6)),
                        'preprocessed_height_px': int(2 * (h + 6)),
                        'batch_canvas_width_px': int(canvas.shape[1]),
                        'slot_y0': int(slots[i][0]),
                        'slot_y1': int(slots[i][1]),
                        'indra_bbox_min_x': '' if xmin is None else round(xmin, 2),
                        'indra_bbox_max_x': '' if xmax is None else round(xmax, 2),
                        'indra_bbox_count': len(obj),
                        'ocr_length': len(text or ''),
                        'ocr_text': text or '',
                        'sample_filename': '',
                        'reason': '',
                    }
                    bucket = int(seconds // 10)
                    suspect = self._is_suspect(text)
                    take_sample = (
                        len(self.row_samples) < self.MAX_ROW_SAMPLES and
                        self.sample_bucket_counts[bucket] < 3 and
                        (suspect or self.ocr_row_counter <= 2 or
                         (self.ocr_row_counter % 35 == 0))
                    )
                    if take_sample:
                        n = len(self.row_samples) + 1
                        stem = f'row_{n:02d}_t{seconds:06.1f}'
                        prepared = preprocess_line_indra(crop)
                        self._png(self.folder, stem + '_raw.png', crop)
                        self._png(self.folder, stem + '_indra_input.png', prepared)
                        self.sample_bucket_counts[bucket] += 1
                        rec['sample_filename'] = stem
                        rec['reason'] = ('truncated_damage_or_confirmation'
                                         if suspect else 'periodic_control')
                        self.row_samples.append(rec.copy())
                        # One full Indra batch example per 10-second slice
                        # (up to six), rather than six from the opening seconds.
                        if (bucket not in self.batch_sampled_buckets and
                                len(self.batch_sampled_buckets) < self.MAX_BATCH_SAMPLES):
                            self.batch_sampled_buckets.add(bucket)
                            self.batch_sampled.add(b)
                            self._png(self.folder, f'batch_{b:04d}_indra_input.png', canvas)
                    self.row_geometry.append(rec)
        except Exception as exc:
            with self.lock:
                self._warning(exc)

    @staticmethod
    def _write_csv(path, rows, fields):
        with path.open('w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore')
            writer.writeheader()
            writer.writerows(rows)

    def finish(self, extra=None):
        with self.lock:
            if not self.active:
                return
            self.active = False
            data = {
                'test_name': 'live_ocr_width_diagnostics',
                'format_version': 1,
                'session_start_epoch': self.start_epoch,
                'session_stop_elapsed': self.stop_seconds,
                'requested_capture': self.capture,
                'indra_ocr_version': self.ocr_version,
                'indra_threads': self.indra_threads,
                'configured_batch_max_lines': self.batch_max,
                'tracking_fps_target': self.tracking_fps,
                'observed_frames': self.frame_counter,
                'ocr_batches': self.batch_counter,
                'ocr_rows_observed': self.ocr_row_counter,
                'sampled_rows': len(self.row_samples),
                'frame_geometry_measurements': len(self.frame_geometry),
                'extra': extra or {},
                'diagnostics_errors': self.errors,
                'privacy_notice': 'PNG files show Combat Log pixels. Review before sharing.',
            }
            try:
                self._write_csv(self.folder / 'live_frame_geometry.csv',
                                self.frame_geometry,
                                ['session_seconds','live_frame_number','requested_left',
                                 'requested_top','requested_width','requested_height',
                                 'mss_bgra_width','mss_bgra_height','mss_bgra_channels',
                                 'gray_width','gray_height'])
                self._write_csv(self.folder / 'live_ocr_batches.csv',
                                self.batch_rows,
                                ['batch_number','session_seconds_first','batch_size',
                                 'batch_image_width','batch_image_height',
                                 'indra_line_objects','queue_depth_after','batch_ocr_milliseconds'])
                self._write_csv(self.folder / 'live_ocr_row_geometry.csv',
                                self.row_geometry,
                                ['row_number','batch_number','session_seconds','row_in_batch',
                                 'batch_size','raw_width_px','raw_height_px',
                                 'preprocessed_width_px','preprocessed_height_px',
                                 'batch_canvas_width_px','slot_y0','slot_y1',
                                 'indra_bbox_min_x','indra_bbox_max_x','indra_bbox_count',
                                 'ocr_length','ocr_text','sample_filename','reason'])
                self._write_csv(self.folder / 'sample_index.csv',
                                self.row_samples,
                                ['row_number','batch_number','session_seconds','batch_size',
                                 'raw_width_px','raw_height_px','preprocessed_width_px',
                                 'ocr_length','sample_filename','reason','ocr_text'])
                (self.folder / 'summary.json').write_text(
                    json.dumps(data, indent=2, ensure_ascii=False), encoding='utf-8')
            except Exception as exc:
                self._warning(exc)
                (self.folder / 'diagnostic_error.txt').write_text(
                    '\n'.join(self.errors), encoding='utf-8')
