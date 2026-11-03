from __future__ import annotations

import argparse
import csv
import json
import queue
import threading
import time
from collections import deque
import math
import traceback

from live_ocr_log import COLUMNS as OCR_COLUMNS
from three_mode_compare import generate as generate_three_mode_comparison
from attack_mode_audit import save_from_replay_ocr
from focus_resume_guard import recover_append_rows
from combat_log_visibility import CombatLogVisibilityGuard
from pathlib import Path

import cv2

from live_overlay import (
    LiveEstimator,
    best_grid_phase_from_feature,
    circular_phase_distance,
    crop_append_with_phase_tolerance,
    crop_new_lines,
    detect_line_height_from_gray,
    estimate_shift_fast,
    estimate_shift_linewise_live,
    estimate_shift_robust_live,
    fast_text_feature,
    recovery_shift_from_previous,
)


class ReplayLiveEstimator(LiveEstimator):
    """Run the current live OCR/parser against a saved capture.

    The replay intentionally blocks when the OCR queue is full instead of dropping
    lines.  This makes it a correctness/reconstruction test of the live pipeline,
    not a benchmark of whether a particular PC can keep up at 30 fps in real time.
    """

    def __init__(self, session: Path):
        super().__init__(session, no_overlay=True)
        self.live_path = session / "replay_live_dps.csv"
        self.live_event_path = session / "replay_live_damage_events.csv"
        self.scan_path = session / "replay_live_ocr_scan.csv"
        self.track_path = session / "replay_live_tracking.csv"
        self.overlay_error_path = session / "replay_live_error.txt"
        self._last_progress_print = 0.0
        self._last_progress_ocr = -1
        self._last_ocr_progress_at = time.monotonic()
        self._last_ocr_health_count = 0
        self.ocr_worker_failure = ""
        self.ocr_worker_thread = None
        self.start_epoch = self._original_start_epoch(session)

    @staticmethod
    def _original_start_epoch(session: Path):
        """A replay has no live wait_for_clock() call: restore the saved clock.

        This is mandatory: LiveEstimator.record_live_ocr_line() creates wall
        timestamps using start_epoch and crashes if it remains None.
        """
        sources = [
            (session / "recording_clock.json", "start_epoch"),
            (session / "live_ocr_complete.json", "capture_start_epoch"),
        ]
        for path, field in sources:
            try:
                val = float(json.loads(path.read_text(encoding="utf-8"))[field])
                if math.isfinite(val) and val > 0:
                    return val
            except (OSError, ValueError, TypeError, KeyError):
                pass
        # Files from old recordings may lack clock metadata. Their replay
        # relative timestamps are still usable; use an explicit fallback epoch.
        return time.time()

    def _check_worker_health(self, max_stall_seconds=120.0):
        """Do not block forever when the separate OCR worker has died/stalled."""
        if self.ocr_worker_failure:
            raise RuntimeError("Replay OCR worker failed. See replay_live_error.txt: "
                               + self.ocr_worker_failure.splitlines()[-1])
        if self.ocr_worker_thread is not None and not self.ocr_worker_thread.is_alive():
            raise RuntimeError("Replay OCR worker stopped before its queue was drained. "
                               "See replay_live_error.txt or the replay output.")
        if self.stop_event.is_set():
            raise RuntimeError("Replay OCR worker stopped unexpectedly before completion.")
        if time.monotonic() - self._last_ocr_progress_at > max_stall_seconds:
            raise RuntimeError("Replay OCR did not complete a batch for over "
                               f"{max_stall_seconds:g} seconds. Aborting instead of hanging.")

    def ocr_worker(self):
        """Propagate previously silent worker crashes to the replay process."""
        try:
            super().ocr_worker()
        except BaseException:
            self.ocr_worker_failure = traceback.format_exc()
            self.stop_event.set()
            try:
                self.overlay_error_path.write_text(self.ocr_worker_failure, encoding="utf-8")
            except OSError:
                pass
            print("REPLAY_ERROR|OCR worker failed: "
                  + self.ocr_worker_failure.splitlines()[-1], flush=True)

    def write_live_ocr_log(self):
        """Keep original live OCR evidence intact; write replay-only outputs."""
        with self.live_ocr_records_lock:
            rows = list(self.live_ocr_records)
        rows.sort(key=lambda r: (float(r.get("capture_elapsed_seconds") or 0),
                                 float(r.get("ocr_finished_wall_epoch") or 0)))
        path = self.session / "replay_live_ocr_lines.csv"
        temp = path.with_name(path.name + ".tmp")
        with temp.open("w", newline="", encoding="utf-8") as file:
            writer = csv.DictWriter(file, fieldnames=OCR_COLUMNS, extrasaction="ignore")
            writer.writeheader()
            for i, row in enumerate(rows, 1):
                writer.writerow(dict(row, line_number=i))
        temp.replace(path)
        meta_path = self.session / "replay_live_ocr_complete.json"
        metadata = {
            "format_version": 1,
            "ready": True,
            "capture_ocr_complete": True,
            "ocr_queue_drained": True,
            "recognized_lines": len(rows),
            "dropped_line_crops": int(self.dropped_lines_total),
            "capture_start_epoch": self.start_epoch,
            "note": "Offline replay OCR; original live OCR files are unchanged",
        }
        temp_meta = meta_path.with_name(meta_path.name + ".tmp")
        temp_meta.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        temp_meta.replace(meta_path)

    def write_status(self, **updates):
        # The OCR worker calls this periodically.  Surface coarse progress to the
        # GUI without flooding stdout with one line per OCR batch.
        now = time.monotonic()
        ocr = int(updates.get("ocr_lines_total", self.ocr_lines_total) or 0)
        if ocr > self._last_ocr_health_count:
            self._last_ocr_health_count = ocr
            self._last_ocr_progress_at = now
        if ocr != self._last_progress_ocr and now - self._last_progress_print >= 0.45:
            self._last_progress_ocr = ocr
            self._last_progress_print = now
            print(
                f"REPLAY_PROGRESS|OCR|{ocr}|{self.detected_lines_total}|{self.parsed_events_total}",
                flush=True,
            )

    def _enqueue_one_line(self, crop, elapsed, block_timeout=0.0, completion_event=None):
        """Offline replay: wait for OCR capacity rather than create fake drops."""
        item = (float(elapsed), crop, completion_event)
        while True:
            self._check_worker_health()
            try:
                self.ocr_queue.put(item, timeout=0.20)
                self.detected_lines_total += 1
                return True
            except queue.Full:
                # In the old implementation this loop was unbounded when the
                # OCR thread crashed and the 512-line queue became full.
                self._check_worker_health()


def read_capture_index(session: Path):
    path = session / "capture_index.csv"
    if not path.exists():
        raise RuntimeError("capture_index.csv is missing from the selected session.")
    rows = []
    with path.open("r", encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            try:
                elapsed = float(row.get("elapsed_seconds", "") or 0.0)
            except Exception:
                continue
            saved = str(row.get("saved", "0")).strip() == "1"
            sf = row.get("saved_frame", "")
            try:
                saved_frame = int(sf) if sf not in (None, "") else None
            except Exception:
                saved_frame = None
            rows.append({
                "capture_frame": int(row.get("capture_frame", "0") or 0),
                "elapsed": elapsed,
                "saved": saved,
                "saved_frame": saved_frame,
            })
    if not rows:
        raise RuntimeError("capture_index.csv contains no capture rows.")
    return rows


def read_focus_resume_frames(session: Path):
    path = session / "focus_interruptions.csv"
    result = set()
    if not path.exists():
        return result
    try:
        with path.open("r", encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                raw = row.get("resume_saved_frame", "")
                if raw not in (None, ""):
                    result.add(int(raw))
    except Exception:
        return set()
    return result


def post_totals(session: Path):
    path = session / "damage_events.csv"
    total = 0
    count = 0
    if not path.exists():
        return None, None
    try:
        with path.open("r", encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                if row.get("event") != "damage_dealt":
                    continue
                try:
                    total += int(float(row.get("amount", "0") or 0))
                    count += 1
                except Exception:
                    pass
        return total, count
    except Exception:
        return None, None


def write_replay_dps(est: ReplayLiveEstimator, duration: float):
    with est.live_event_records_lock:
        damage_rows = list(est.live_event_records)
    damage_events = sorted((float(r[1]), int(r[4])) for r in damage_rows)
    with est.events_lock:
        attack_events = sorted(float(x) for x in est.attacks)

    window = float(est.window_seconds)
    out = est.live_path
    out.parent.mkdir(parents=True, exist_ok=True)

    damage_q = deque()
    attack_q = deque()
    damage_i = 0
    attack_i = 0
    cumulative = 0
    t = 0.0
    step = 0.20

    with out.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "capture_elapsed_seconds",
            "live_rolling_dps",
            "window_seconds",
            "window_damage",
            "parsed_events_total",
            "live_rolling_apm",
            "window_attacks",
            "parsed_attacks_total",
            "cumulative_damage",
            "source",
        ])
        while t <= duration + 1e-9:
            while damage_i < len(damage_events) and damage_events[damage_i][0] <= t + 1e-9:
                ev = damage_events[damage_i]
                damage_q.append(ev)
                cumulative += ev[1]
                damage_i += 1
            while attack_i < len(attack_events) and attack_events[attack_i] <= t + 1e-9:
                attack_q.append(attack_events[attack_i])
                attack_i += 1
            cutoff = t - window
            while damage_q and damage_q[0][0] <= cutoff:
                damage_q.popleft()
            while attack_q and attack_q[0] <= cutoff:
                attack_q.popleft()
            wd = sum(v for _ts, v in damage_q)
            wa = len(attack_q)
            w.writerow([
                f"{t:.6f}",
                f"{wd / window:.3f}",
                f"{window:g}",
                wd,
                damage_i,
                f"{wa * 60.0 / window:.3f}",
                wa,
                attack_i,
                cumulative,
                "offline_live_replay",
            ])
            t += step


def run_replay(session: Path):
    video = session / "combat_changed_gray_ffv1.mkv"
    if not video.exists():
        raise RuntimeError("The selected session has no combat_changed_gray_ffv1.mkv capture.")

    rows = read_capture_index(session)
    saved_rows = [r for r in rows if r["saved"] and r["saved_frame"] is not None]
    if not saved_rows:
        raise RuntimeError("The selected capture contains no saved video frames.")
    resume_frames = read_focus_resume_frames(session)

    # Stale replay results must not be confused with the current run.
    for name in ("replay_live_error.txt", "replay_live_summary.json",
                 "replay_live_ocr_complete.json"):
        (session / name).unlink(missing_ok=True)

    est = ReplayLiveEstimator(session)
    est.live_event_records = []
    est.live_raw_damage_total = 0
    est.live_corrected_damage_total = 0
    est.events = deque()
    est.attacks = deque()
    est.pending_damage_wrap = None

    # Historical replay should start from the actual pre-timeline baseline when
    # available.  This is the closest saved equivalent of the continuously running
    # live monitor's pre-recording frame.
    prev_gray = None
    prev_feat = None
    prev_phase = None
    align_gray = None
    align_feat = None
    align_phase = None
    align_age_frames = 0
    pre_focus_feat = None
    pre_focus_phase = None
    pre_focus_line_height = None
    focus_resume_pending = False
    # FFV1 stores changed frames only: one stable return frame is enough to
    # safely REBASELINE; the next saved frame may be a new damage event.
    visibility_guard = CombatLogVisibilityGuard(resume_frames=1)
    visibility_events = []
    baseline = session / "baseline_gray.png"
    if baseline.exists():
        prev_gray = cv2.imread(str(baseline), cv2.IMREAD_GRAYSCALE)
        if prev_gray is not None:
            prev_feat = fast_text_feature(prev_gray)
            prev_phase = best_grid_phase_from_feature(prev_feat, est.line_height)
            align_gray = prev_gray
            align_feat = prev_feat
            align_phase = prev_phase
            align_age_frames = 0

    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        raise RuntimeError("Could not open the saved FFV1 capture video.")

    ocr_thread = threading.Thread(target=est.ocr_worker, name="replay-live-ocr", daemon=True)
    est.ocr_worker_thread = ocr_thread
    ocr_thread.start()

    track_f = est.track_path.open("w", encoding="utf-8", newline="")
    track_w = csv.writer(track_f)
    track_w.writerow([
        "saved_frame",
        "capture_frame",
        "capture_elapsed_seconds",
        "shift_px",
        "new_lines",
        "alignment_score",
        "zero_score",
        "decision",
        "grid_phase",
        "anchor_phase",
        "phase_delta",
        "anchor_age_frames",
        "anchor_action",
        "linewise_shift_px",
        "linewise_score",
        "linewise_second_score",
        "linewise_confident",
        "robust_shift_px",
        "robust_score",
        "robust_second_score",
        "robust_confident",
        "robust_override",
        "ocr_queue_depth",
    ])

    processed_saved = 0
    started = time.perf_counter()
    try:
        for entry in saved_rows:
            est._check_worker_health()
            ok, gray = cap.read()
            if not ok or gray is None:
                raise RuntimeError(
                    f"Video ended after {processed_saved} saved frames; capture_index expects {len(saved_rows)}."
                )
            if len(gray.shape) == 3:
                gray = cv2.cvtColor(gray, cv2.COLOR_BGR2GRAY)
            elapsed = float(entry["elapsed"])
            saved_frame = int(entry["saved_frame"])
            processed_saved += 1

            # The recorder forces an anchor after DDO regains focus.  Live capture
            # also resets its visual baseline there, so replay must do the same.
            if saved_frame in resume_frames:
                pre_focus_feat = prev_feat
                pre_focus_phase = prev_phase
                pre_focus_line_height = est.line_height
                focus_resume_pending = True
                prev_gray = None
                prev_feat = None
                prev_phase = None
                align_gray = None
                align_feat = None
                align_phase = None
                align_age_frames = 0
                est.clear_pending_bottom_line(count_drop=True)

            feat = fast_text_feature(gray)
            est.tracking_frames += 1
            visibility = visibility_guard.inspect(gray)
            if visibility.transition:
                visibility_events.append({
                    'saved_frame': saved_frame,
                    'elapsed_seconds': f'{elapsed:.6f}',
                    'event': visibility.transition,
                    'reason': visibility.reason,
                    'dark_fraction': f'{visibility.dark_fraction:.5f}',
                    'ink_fraction': f'{visibility.ink_fraction:.5f}',
                    'scene_change': f'{visibility.scene_change:.5f}',
                })
            if visibility.action != 'track':
                # Never treat old Combat Log returning from a loading screen
                # as a newly scrolled/appended message.
                prev_gray = prev_feat = prev_phase = None
                align_gray = align_feat = align_phase = None
                align_age_frames = 0
                focus_resume_pending = False
                pre_focus_feat = pre_focus_phase = pre_focus_line_height = None
                est.clear_pending_bottom_line(count_drop=True)
                if visibility.action == 'rebaseline':
                    phase = best_grid_phase_from_feature(feat, est.line_height)
                    prev_gray, prev_feat, prev_phase = gray, feat, phase
                    align_gray, align_feat, align_phase = gray, feat, phase
                track_w.writerow([
                    saved_frame, entry['capture_frame'], f'{elapsed:.6f}', 0, 0,
                    '', '', f'visibility-{visibility.action}:{visibility.reason}',
                    '', '', '', 0, 'visibility-guard',
                    '', '', '', False, '', '', '', False, False,
                    est.ocr_queue.qsize(),
                ])
                continue

            if est.auto_line_height and not est.line_height_locked:
                detected_h, _lh_score, _lh_margin, lh_ok = detect_line_height_from_gray(
                    gray,
                    est.line_height,
                    int(est.analysis_cfg.get("line_height_min", 10)),
                    int(est.analysis_cfg.get("line_height_max", 22)),
                )
                if lh_ok:
                    est.line_height_candidates.append(detected_h)
                    est.line_height_candidates = est.line_height_candidates[-5:]
                    if est.line_height_candidates.count(detected_h) >= 3:
                        if detected_h != est.line_height:
                            est.line_height = detected_h
                            prev_gray = None
                            prev_feat = None
                            prev_phase = None
                            align_gray = None
                            align_feat = None
                            align_phase = None
                            align_age_frames = 0
                            est.clear_pending_bottom_line(count_drop=True)
                        est.line_height_locked = True

            phase = best_grid_phase_from_feature(feat, est.line_height)
            robust_shift = 0
            robust_score = 0.0
            robust_second = 0.0
            robust_confident = False
            robust_override = False
            lw_shift = 0
            lw_score = 0.0
            lw_second = 0.0
            lw_confident = False
            phase_delta = 0
            anchor_action = ""
            crops = []

            if align_gray is None:
                shift = 0
                score = 1.0
                zero = 1.0
                decision = "baseline"
                if focus_resume_pending:
                    crops = (recover_append_rows(
                        pre_focus_feat, gray, feat, pre_focus_phase,
                        phase, est.line_height, scale=255.0)
                        if pre_focus_line_height == est.line_height else [])
                    for crop in crops:
                        est.enqueue_new_lines([crop], elapsed)
                    decision = "focus-resume-append" if crops else "focus-resume-baseline"
                    focus_resume_pending = False
                    pre_focus_feat = None
                    pre_focus_phase = None
                    pre_focus_line_height = None
                align_gray = gray
                align_feat = feat
                align_phase = phase
                align_age_frames = 0
                anchor_action = "baseline"
            else:
                phase_delta = circular_phase_distance(phase, align_phase, est.line_height)
                shift, score, zero, decision = estimate_shift_fast(
                    align_feat, feat, est.line_height, est.max_shift_px,
                    phase_offset=(align_phase - phase) % est.line_height,
                )
                if decision == "phase-corrected-positive" and shift > 0:
                    est.phase_corrected_scrolls += 1
                if decision == "uncertain->zero":
                    est.robust_fallback_runs += 1
                    (
                        robust_shift,
                        robust_score,
                        _robust_zero,
                        robust_decision,
                        robust_confident,
                        _robust_margin,
                        robust_second,
                    ) = estimate_shift_robust_live(
                        align_gray,
                        gray,
                        est.line_height,
                        est.max_shift_px,
                        est.analysis_cfg,
                    )
                    if robust_confident and robust_decision == "positive" and robust_shift > 0:
                        shift = int(robust_shift)
                        decision += "+robust-positive"
                        robust_override = True
                        est.robust_fallback_overrides += 1
                    else:
                        decision += "+robust-keep-zero"

                if (
                    shift == 0
                    and phase_delta <= 1
                    and zero > est.anchor_zero_rebase_error
                ):
                    est.anchor_linewise_runs += 1
                    lw_shift, lw_score, lw_second, lw_confident = estimate_shift_linewise_live(
                        align_gray,
                        gray,
                        align_phase,
                        phase,
                        est.line_height,
                        est.max_shift_px,
                    )
                    if lw_confident and lw_shift > 0:
                        shift = int(lw_shift)
                        decision += "+linewise-positive"
                        est.anchor_linewise_overrides += 1
                    elif lw_confident:
                        decision += "+linewise-zero"

                if shift == 0 and align_age_frames >= 1 and prev_feat is not None:
                    est.anchor_previous_frame_checks += 1
                    pair_shift, pair_decision = recovery_shift_from_previous(
                        prev_feat, feat, prev_phase, phase,
                        est.line_height, est.max_shift_px,
                    )
                    if pair_shift > 0:
                        shift = pair_shift
                        decision += '+prev-frame-positive'
                        est.anchor_previous_frame_recoveries += 1

                crop_entries = []
                recovered = est.recover_pending_bottom_line(gray, feat, shift)
                if recovered is not None:
                    crop_entries.append(recovered)
                    decision += "+recover-bottom"

                new_crops, deferred_y0 = crop_new_lines(
                    gray, feat, shift, phase, est.line_height
                )
                crop_entries.extend((elapsed, c) for c in new_crops)
                if deferred_y0 is not None:
                    if est.defer_pending_bottom_line(gray, deferred_y0, elapsed):
                        decision += "+defer-bottom"

                if (
                    not crop_entries
                    and shift == 0
                    and prev_feat is not None
                ):
                    append_crops = crop_append_with_phase_tolerance(
                        prev_feat, gray, feat, prev_phase, phase,
                        est.line_height,
                    )
                    if append_crops:
                        crop_entries.extend((elapsed, c) for c in append_crops)
                        decision += ("+append" if prev_phase == phase
                                     else "+append-phase-tolerant")

                for crop_elapsed, crop in crop_entries:
                    est.enqueue_new_lines([crop], crop_elapsed)
                crops = [crop for _crop_elapsed, crop in crop_entries]

                if shift > 0:
                    align_gray = gray
                    align_feat = feat
                    align_phase = phase
                    align_age_frames = 0
                    anchor_action = "scroll-rebase"
                elif "+append" in decision:
                    align_gray = gray
                    align_feat = feat
                    align_phase = phase
                    align_age_frames = 0
                    anchor_action = "append-rebase"
                elif lw_confident and lw_shift == 0:
                    align_gray = gray
                    align_feat = feat
                    align_phase = phase
                    align_age_frames = 0
                    anchor_action = "linewise-zero-rebase"
                elif phase_delta == 0 and zero <= est.anchor_zero_rebase_error:
                    align_gray = gray
                    align_feat = feat
                    align_phase = phase
                    align_age_frames = 0
                    anchor_action = "static-rebase"
                else:
                    align_age_frames += 1
                    est.anchor_hold_frames_total += 1
                    est.anchor_hold_streak_max = max(est.anchor_hold_streak_max, align_age_frames)
                    anchor_action = "hold"
                    decision += "+anchor-hold"
                    if align_age_frames == 8 and zero > est.anchor_zero_rebase_error:
                        est.anchor_stall_warnings += 1
                        decision += '+possible-scroll-gap'
                    if align_age_frames >= est.anchor_timeout_frames:
                        if phase_delta > 1:
                            est.anchor_phase_timeout_rebases += 1
                            decision += "+phase-timeout"
                        est.anchor_unresolved_gaps += 1
                        align_gray = gray
                        align_feat = feat
                        align_phase = phase
                        align_age_frames = 0
                        anchor_action = "timeout-rebase"
                        decision += "+anchor-timeout-rebase+unresolved-gap"

            track_w.writerow([
                saved_frame,
                entry["capture_frame"],
                f"{elapsed:.6f}",
                shift,
                len(crops),
                f"{score:.6f}",
                f"{zero:.6f}",
                decision,
                phase,
                "" if align_phase is None else align_phase,
                phase_delta,
                align_age_frames,
                anchor_action,
                lw_shift,
                f"{lw_score:.6f}",
                f"{lw_second:.6f}",
                lw_confident,
                robust_shift,
                f"{robust_score:.6f}",
                f"{robust_second:.6f}",
                robust_confident,
                robust_override,
                est.ocr_queue.qsize(),
            ])

            prev_gray = gray
            prev_feat = feat
            prev_phase = phase

            if processed_saved == 1 or processed_saved % 40 == 0 or processed_saved == len(saved_rows):
                print(
                    f"REPLAY_PROGRESS|FRAMES|{processed_saved}|{len(saved_rows)}|{est.detected_lines_total}",
                    flush=True,
                )
                track_f.flush()

        # Mirror Stop: one clipped newest row gets a final OCR chance.
        est.flush_pending_bottom_line()
        if est.last_bottom_flush_event is not None:
            est.last_bottom_flush_event.wait(timeout=30.0)

        print(
            f"REPLAY_PROGRESS|WAIT_OCR|{est.ocr_lines_total}|{est.detected_lines_total}|{est.parsed_events_total}",
            flush=True,
        )
        # queue.join() had no timeout and could hang forever after an OCR
        # worker exception. Poll instead and surface the real failure.
        while est.ocr_queue.unfinished_tasks:
            est._check_worker_health()
            time.sleep(0.2)
        est._check_worker_health()
        est.stop_event.set()
        ocr_thread.join(timeout=60.0)
        if ocr_thread.is_alive():
            raise RuntimeError("Replay OCR worker did not finish after the queue drained.")
        if est.ocr_worker_failure:
            raise RuntimeError("Replay OCR failed: " + est.ocr_worker_failure.splitlines()[-1])
    finally:
        cap.release()
        track_f.flush()
        track_f.close()
        if not est.stop_event.is_set():
            est.stop_event.set()
        if ocr_thread.is_alive():
            ocr_thread.join(timeout=2.0)

    # This stays useful even if the replay is run after the real recording.
    with (session / 'replay_live_visibility_events.csv').open(
        'w', newline='', encoding='utf-8'
    ) as f:
        writer = csv.DictWriter(f, fieldnames=[
            'saved_frame', 'elapsed_seconds', 'event', 'reason',
            'dark_fraction', 'ink_fraction', 'scene_change',
        ])
        writer.writeheader()
        writer.writerows(visibility_events)

    duration = float(rows[-1]["elapsed"])
    write_replay_dps(est, duration)
    # Independent attack/confirmation records from the replay's own OCR rows.
    # Do not overwrite the GUI's FFV1/fast-live attack_events.csv.
    replay_attacks, replay_confirms = save_from_replay_ocr(session)

    replay_total = int(est.live_corrected_damage_total)
    replay_events = int(est.parsed_events_total)
    post_total, post_events = post_totals(session)
    processing_seconds = time.perf_counter() - started
    difference = None if post_total is None else replay_total - int(post_total)
    difference_pct = None
    if post_total not in (None, 0):
        difference_pct = difference / float(post_total) * 100.0

    summary = {
        "mode": "offline_live_pipeline_replay",
        "session": session.name,
        "recording_duration_seconds": duration,
        "capture_index_rows": len(rows),
        "saved_video_frames": len(saved_rows),
        "replay_processing_seconds": processing_seconds,
        "replay_live_damage": replay_total,
        "replay_live_damage_events": replay_events,
        "replay_live_attack_events": int(replay_attacks),
        "replay_live_confirm_attempts": int(replay_confirms),
        "post_damage": post_total,
        "post_damage_events": post_events,
        "damage_difference_replay_minus_post": difference,
        "damage_difference_percent": difference_pct,
        "detected_lines": int(est.detected_lines_total),
        "ocr_lines": int(est.ocr_lines_total),
        "dropped_lines": int(est.dropped_lines_total),
        "bottom_deferred": int(est.bottom_deferred_total),
        "bottom_recovered": int(est.bottom_recovered_total),
        "bottom_stop_flushed": int(est.bottom_stop_flushed_total),
        "bottom_dropped": int(est.bottom_dropped_total),
        "robust_fallback_runs": int(est.robust_fallback_runs),
        "robust_fallback_overrides": int(est.robust_fallback_overrides),
        "anchor_hold_frames": int(est.anchor_hold_frames_total),
        "anchor_linewise_runs": int(est.anchor_linewise_runs),
        "anchor_linewise_overrides": int(est.anchor_linewise_overrides),
        "anchor_previous_frame_checks": int(est.anchor_previous_frame_checks),
        "anchor_previous_frame_recoveries": int(est.anchor_previous_frame_recoveries),
        "anchor_hold_streak_max": int(est.anchor_hold_streak_max),
        "anchor_stall_warnings": int(est.anchor_stall_warnings),
        "anchor_unresolved_gaps": int(est.anchor_unresolved_gaps),
        "zone_visibility_hidden_transitions": visibility_guard.transitions_to_hidden,
        "zone_visibility_resumed_transitions": visibility_guard.transitions_to_visible,
        "zone_visibility_blocked_saved_frames": visibility_guard.blocked_frames,
        "line_height_used": int(est.line_height),
        "indra_threads": int(est.indra_threads),
        "ocr_batch_max_lines": int(est.ocr_batch_max),
        "note": (
            "Offline replay validates current live reconstruction/OCR/parser on the saved capture. "
            "It blocks instead of dropping when the OCR queue is full, so it does not measure real-time throughput. "
            "Unsaved 30-fps capture frames are treated as unchanged; only recorder-saved visual transitions need processing."
        ),
    }
    (session / "replay_live_summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    # A separate report compares the *independent* event streams. Never
    # silently replace an OCR total with the post-analysis result.
    try:
        comparison = generate_three_mode_comparison(session)
        summary['three_mode_comparison'] = comparison
        (session / "replay_live_summary.json").write_text(
            json.dumps(summary, indent=2), encoding="utf-8"
        )
        if comparison.get('all_three_damage_sequences_equal') is False:
            print("REPLAY_INFO|The three pipelines disagree; see three_mode_mismatches.csv", flush=True)
    except Exception as exc:
        print(f"REPLAY_INFO|Could not write three-mode comparison: {exc}", flush=True)

    print(
        "REPLAY_RESULT|"
        + json.dumps(
            {
                "replay_damage": replay_total,
                "replay_events": replay_events,
                "post_damage": post_total,
                "post_events": post_events,
                "difference": difference,
                "difference_pct": difference_pct,
            },
            separators=(",", ":"),
        ),
        flush=True,
    )
    print("REPLAY_PROGRESS|DONE|1|1|0", flush=True)
    return 0


def main():
    ap = argparse.ArgumentParser(description="Replay the current live pipeline on a saved DDO combat session.")
    ap.add_argument("--session", required=True)
    args = ap.parse_args()
    session = Path(args.session).resolve()
    if not session.exists():
        raise SystemExit(f"Selected session does not exist: {session}")
    try:
        raise SystemExit(run_replay(session))
    except Exception as exc:
        try:
            (session / "replay_live_error.txt").write_text(
                f"{type(exc).__name__}: {exc}\n", encoding="utf-8"
            )
        except Exception:
            pass
        print(f"REPLAY_ERROR|{type(exc).__name__}: {exc}", flush=True)
        raise


if __name__ == "__main__":
    main()
