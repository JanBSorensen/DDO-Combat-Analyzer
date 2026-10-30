from __future__ import annotations

import argparse
import csv
import json
import queue
import threading
import time
from collections import deque
from pathlib import Path

import cv2

from live_overlay import (
    LiveEstimator,
    best_grid_phase_from_feature,
    crop_blank_to_filled_lines,
    crop_new_lines,
    detect_line_height_from_gray,
    estimate_shift_fast,
    estimate_shift_robust_live,
    fast_text_feature,
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

    def write_status(self, **updates):
        # The OCR worker calls this periodically.  Surface coarse progress to the
        # GUI without flooding stdout with one line per OCR batch.
        now = time.monotonic()
        ocr = int(updates.get("ocr_lines_total", self.ocr_lines_total) or 0)
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
        while not self.stop_event.is_set():
            try:
                self.ocr_queue.put(item, timeout=0.20)
                self.detected_lines_total += 1
                return True
            except queue.Full:
                continue
        self.dropped_lines_total += 1
        return False


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
    baseline = session / "baseline_gray.png"
    if baseline.exists():
        prev_gray = cv2.imread(str(baseline), cv2.IMREAD_GRAYSCALE)
        if prev_gray is not None:
            prev_feat = fast_text_feature(prev_gray)
            prev_phase = best_grid_phase_from_feature(prev_feat, est.line_height)

    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        raise RuntimeError("Could not open the saved FFV1 capture video.")

    ocr_thread = threading.Thread(target=est.ocr_worker, name="replay-live-ocr", daemon=True)
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
                prev_gray = None
                prev_feat = None
                prev_phase = None
                est.clear_pending_bottom_line(count_drop=True)

            feat = fast_text_feature(gray)
            est.tracking_frames += 1

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
                            est.clear_pending_bottom_line(count_drop=True)
                        est.line_height_locked = True

            phase = best_grid_phase_from_feature(feat, est.line_height)
            robust_shift = 0
            robust_score = 0.0
            robust_second = 0.0
            robust_confident = False
            robust_override = False
            crops = []

            if prev_gray is None:
                shift = 0
                score = 1.0
                zero = 1.0
                decision = "baseline"
            else:
                shift, score, zero, decision = estimate_shift_fast(
                    prev_feat, feat, est.line_height, est.max_shift_px
                )
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
                        prev_gray,
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

                if not crop_entries and shift == 0 and prev_phase == phase:
                    append_crops = crop_blank_to_filled_lines(
                        prev_feat, gray, feat, phase, est.line_height
                    )
                    if append_crops:
                        crop_entries.extend((elapsed, c) for c in append_crops)
                        decision += "+append"

                for crop_elapsed, crop in crop_entries:
                    est.enqueue_new_lines([crop], crop_elapsed)
                crops = [crop for _crop_elapsed, crop in crop_entries]

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
        est.ocr_queue.join()
        est.stop_event.set()
        ocr_thread.join(timeout=60.0)
        if ocr_thread.is_alive():
            raise RuntimeError("Replay OCR worker did not finish after the queue drained.")
    finally:
        cap.release()
        track_f.flush()
        track_f.close()
        if not est.stop_event.is_set():
            est.stop_event.set()
        if ocr_thread.is_alive():
            ocr_thread.join(timeout=2.0)

    duration = float(rows[-1]["elapsed"])
    write_replay_dps(est, duration)

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
        "replay_live_attack_events": int(est.parsed_attacks_total),
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
