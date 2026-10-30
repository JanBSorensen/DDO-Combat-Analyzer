from __future__ import annotations

import csv
import ctypes
import json
import os
import shutil
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

import cv2
import mss
import numpy as np

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"


def find_ddo_window():
    if os.name != "nt":
        return None
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.FindWindowW("Turbine Device Class", None)
        if not hwnd:
            hwnd = user32.FindWindowW("Turbine Device Class Name", None)
        return hwnd or None
    except Exception:
        return None


def is_ddo_foreground():
    if os.name != "nt":
        return True
    hwnd = find_ddo_window()
    if not hwnd:
        return False
    try:
        return int(ctypes.windll.user32.GetForegroundWindow()) == int(hwnd)
    except Exception:
        return False


def find_ffmpeg(config_value: str) -> str | None:
    candidates = []
    if config_value:
        candidates.append(Path(config_value))
    candidates.extend([
        HERE / "ffmpeg.exe",
        HERE / "ffmpeg" / "bin" / "ffmpeg.exe",
    ])
    for p in candidates:
        if p.exists():
            return str(p)
    on_path = shutil.which("ffmpeg")
    if on_path:
        return on_path

    # WinGet installs are not always added to PATH immediately.
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        packages = Path(local) / "Microsoft" / "WinGet" / "Packages"
        if packages.exists():
            try:
                for root in packages.glob("Gyan.FFmpeg*"):
                    for exe in root.rglob("ffmpeg.exe"):
                        if exe.exists():
                            return str(exe)
            except Exception:
                pass
    return None


def text_feature(gray: np.ndarray, threshold: int, border: int) -> np.ndarray:
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (11, 7))
    top_hat = cv2.morphologyEx(gray, cv2.MORPH_TOPHAT, kernel)
    blur = cv2.GaussianBlur(gray, (0, 0), 2.0)
    high = cv2.absdiff(gray, blur)
    feat = cv2.max(top_hat, high)
    _, feat = cv2.threshold(feat, threshold, 255, cv2.THRESH_TOZERO)

    if border > 0 and feat.shape[0] > 2 * border and feat.shape[1] > 2 * border:
        feat[:border, :] = 0
        feat[-border:, :] = 0
        feat[:, :border] = 0
        feat[:, -border:] = 0

    return feat.astype(np.float32) / 255.0


def feature_change_ratio(prev_feat, curr_feat, pixel_diff_threshold: float) -> float:
    if prev_feat is None:
        return 1.0
    diff = np.abs(curr_feat - prev_feat)
    return float(np.count_nonzero(diff >= pixel_diff_threshold)) / float(diff.size)


def main():
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    cap = cfg["capture"]
    fps = max(1, int(cfg.get("capture_fps", 30)))
    interval = 1.0 / fps

    tc = cfg["text_change"]
    feature_threshold = int(tc["feature_threshold"])
    pixel_diff_threshold = float(tc["pixel_difference_threshold"])
    save_ratio_threshold = float(tc["changed_pixel_ratio_to_save"])
    force_anchor = float(tc["force_anchor_every_seconds"])
    border = int(tc["ignore_border_pixels"])

    ffmpeg = find_ffmpeg(str(cfg.get("ffmpeg_path", "")).strip())
    if not ffmpeg:
        print()
        print("ERROR: FFmpeg was not found.")
        print("Install FFmpeg with winget or set ffmpeg_path in config.json.")
        raise SystemExit(2)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    session = HERE / f"combat_{stamp}"
    session.mkdir(parents=True, exist_ok=True)

    video_path = session / "combat_changed_gray_ffv1.mkv"
    index_path = session / "capture_index.csv"
    focus_path = session / "focus_interruptions.csv"
    ffmpeg_log_path = session / "ffmpeg.log"
    summary_path = session / "recording_summary.txt"
    config_copy = session / "config_used.json"
    config_copy.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

    width = int(cap["width"])
    height = int(cap["height"])

    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel", "warning",
        "-f", "rawvideo",
        "-pixel_format", "gray",
        "-video_size", f"{width}x{height}",
        "-framerate", str(fps),
        "-i", "pipe:0",
        "-an",
        "-c:v", "ffv1",
        "-level", "3",
        "-coder", "1",
        "-context", "1",
        "-g", "1",
        "-slicecrc", "1",
        "-pix_fmt", "gray",
        "-y",
        str(video_path),
    ]

    print()
    print("DDO COMBAT ANALYZER v5.3 - RECORDING")
    print("-----------------------------")
    print(f"Capture area: {cap}")
    print(f"Capture rate: {fps} FPS")
    print("Lossless grayscale FFV1; only text-change frames + anchor frames are saved.")
    print()
    print("Recorder engine is preparing...")
    print()

    stop_event = threading.Event()

    ffmpeg_log = ffmpeg_log_path.open("wb")
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=ffmpeg_log,
        creationflags=creationflags,
    )

    # Tell the GUI which session directory belongs to this recording.
    print(f"SESSION_DIR|{session}", flush=True)

    # Handshake with the GUI. The recorder is fully initialized before DDO
    # is brought to the foreground, preventing a hidden startup gap.
    print("RECORDER_READY", flush=True)
    try:
        start_command = input().strip().upper()
    except EOFError:
        start_command = ""
    if start_command != "START":
        try:
            proc.stdin.close()
        except Exception:
            pass
        proc.terminate()
        proc.wait(timeout=10)
        ffmpeg_log.close()
        print("Recorder cancelled before capture started.", flush=True)
        raise SystemExit(0)

    # Capture an explicit pre-timeline baseline while DDO is already in front.
    # New sessions can therefore compare the very first recorded frame against
    # a known pre-combat image instead of discarding that frame wholesale.
    baseline_path = session / "baseline_gray.png"
    try:
        if is_ddo_foreground():
            with mss.MSS() as baseline_sct:
                baseline_shot = baseline_sct.grab(cap)
                baseline_bgra = np.asarray(baseline_shot, dtype=np.uint8)
                baseline_gray = cv2.cvtColor(baseline_bgra, cv2.COLOR_BGRA2GRAY)
                ok, enc = cv2.imencode(".png", baseline_gray)
                if ok:
                    enc.tofile(str(baseline_path))
                    print("BASELINE_CAPTURED", flush=True)
        else:
            print("BASELINE_WARNING|DDO is not foreground; first visible frame will become the baseline.", flush=True)
    except Exception as exc:
        print(f"BASELINE_WARNING|{type(exc).__name__}: {exc}", flush=True)

    def wait_for_stop():
        try:
            while True:
                command = input().strip().upper()
                if command in ("STOP", ""):
                    stop_event.set()
                    break
        except EOFError:
            stop_event.set()

    threading.Thread(target=wait_for_stop, daemon=True).start()

    # Shared clock for the live estimator and the final post-analysis timeline.
    start_epoch = time.time()
    start = time.perf_counter()
    clock_path = session / "recording_clock.json"
    clock_path.write_text(
        json.dumps({
            "start_epoch": start_epoch,
            "start_wall_time": datetime.now().isoformat(timespec="milliseconds"),
        }, indent=2),
        encoding="utf-8",
    )

    print("CAPTURE_STARTED", flush=True)

    captured = 0
    saved = 0
    skipped = 0
    forced = 0
    late_frames = 0
    max_lateness = 0.0

    next_deadline = start
    last_saved_time = -1e9
    last_status = start
    previous_feature = None

    focus_interruptions = []
    focus_pause_started = None
    focus_pause_total = 0.0
    pending_resume_index = None
    force_next_anchor = False

    with index_path.open("w", newline="", encoding="utf-8") as cf:
        writer = csv.writer(cf)
        writer.writerow([
            "capture_frame",
            "wall_time",
            "elapsed_seconds",
            "saved",
            "saved_frame",
            "text_change_ratio",
            "forced_anchor",
            "lateness_ms",
        ])
        cf.flush()

        try:
            with mss.MSS() as sct:
                while not stop_event.is_set():
                    now = time.perf_counter()
                    if now < next_deadline:
                        time.sleep(next_deadline - now)

                    focus_check = time.perf_counter()
                    focus_elapsed = max(0.0, focus_check - start)
                    if not is_ddo_foreground():
                        if focus_pause_started is None:
                            focus_pause_started = focus_elapsed
                            print(f"FOCUS_PAUSED|{focus_elapsed:.6f}", flush=True)
                        # Do not inspect pixels from another foreground window.
                        # Reset the visual baseline so the return to DDO cannot
                        # be mistaken for a huge Combat Log scroll.
                        previous_feature = None
                        next_deadline = focus_check + interval
                        continue

                    if focus_pause_started is not None:
                        resume_elapsed = focus_elapsed
                        duration_focus = max(0.0, resume_elapsed - focus_pause_started)
                        focus_pause_total += duration_focus
                        focus_interruptions.append({
                            "start_elapsed_seconds": focus_pause_started,
                            "end_elapsed_seconds": resume_elapsed,
                            "duration_seconds": duration_focus,
                            "resume_saved_frame": "",
                        })
                        pending_resume_index = len(focus_interruptions) - 1
                        focus_pause_started = None
                        previous_feature = None
                        force_next_anchor = True
                        print(f"FOCUS_RESUMED|{resume_elapsed:.6f}|{duration_focus:.6f}", flush=True)

                    shot = sct.grab(cap)
                    bgra = np.asarray(shot, dtype=np.uint8)
                    gray = cv2.cvtColor(bgra, cv2.COLOR_BGRA2GRAY)

                    feature = text_feature(gray, feature_threshold, border)
                    change_ratio = feature_change_ratio(
                        previous_feature,
                        feature,
                        pixel_diff_threshold,
                    )

                    captured_at = time.perf_counter()
                    captured += 1
                    elapsed = captured_at - start

                    is_forced = (
                        force_next_anchor
                        or saved == 0
                        or (captured_at - last_saved_time) >= force_anchor
                    )
                    should_save = is_forced or change_ratio >= save_ratio_threshold

                    if proc.poll() is not None:
                        raise RuntimeError(
                            f"FFmpeg stopped unexpectedly. See {ffmpeg_log_path}"
                        )

                    saved_frame_number = ""
                    if should_save:
                        proc.stdin.write(gray.tobytes())
                        saved += 1
                        saved_frame_number = saved
                        last_saved_time = captured_at
                        if pending_resume_index is not None:
                            focus_interruptions[pending_resume_index]["resume_saved_frame"] = saved
                            pending_resume_index = None
                        if is_forced and change_ratio < save_ratio_threshold:
                            forced += 1
                        force_next_anchor = False
                    else:
                        skipped += 1

                    lateness = max(0.0, captured_at - next_deadline)
                    if lateness > interval:
                        late_frames += 1
                    max_lateness = max(max_lateness, lateness)

                    writer.writerow([
                        captured,
                        datetime.now().isoformat(timespec="milliseconds"),
                        f"{elapsed:.6f}",
                        int(should_save),
                        saved_frame_number,
                        f"{change_ratio:.7f}",
                        int(is_forced and should_save),
                        f"{lateness * 1000:.3f}",
                    ])

                    if captured % 30 == 0:
                        cf.flush()

                    previous_feature = feature
                    next_deadline += interval

                    now2 = time.perf_counter()
                    if next_deadline < now2 - interval:
                        next_deadline = now2 + interval

                    status_every = max(1.0, float(cfg.get("status_every_seconds", 5)))
                    if now2 - last_status >= status_every:
                        run_elapsed = max(0.001, now2 - start)
                        capture_fps = captured / run_elapsed
                        keep_pct = 100.0 * saved / max(1, captured)
                        size_mb = (
                            video_path.stat().st_size / (1024 * 1024)
                            if video_path.exists()
                            else 0.0
                        )
                        projected_gb_15m = (size_mb / run_elapsed) * 900 / 1024
                        print(
                            f"{run_elapsed:7.1f}s | "
                            f"{capture_fps:5.2f} fps | "
                            f"{saved}/{captured} kept ({keep_pct:4.1f}%) | "
                            f"{size_mb:6.1f} MB | ~{projected_gb_15m:4.2f} GB/15min"
                        )
                        last_status = now2

        except KeyboardInterrupt:
            print()
            print("Ctrl+C received.")
        finally:
            cf.flush()

    capture_elapsed = max(0.001, time.perf_counter() - start)
    if focus_pause_started is not None:
        duration_focus = max(0.0, capture_elapsed - focus_pause_started)
        focus_pause_total += duration_focus
        focus_interruptions.append({
            "start_elapsed_seconds": focus_pause_started,
            "end_elapsed_seconds": capture_elapsed,
            "duration_seconds": duration_focus,
            "resume_saved_frame": "",
        })
        focus_pause_started = None

    with focus_path.open("w", newline="", encoding="utf-8") as ff:
        fw = csv.DictWriter(ff, fieldnames=[
            "start_elapsed_seconds", "end_elapsed_seconds",
            "duration_seconds", "resume_saved_frame",
        ])
        fw.writeheader()
        for row in focus_interruptions:
            fw.writerow({
                "start_elapsed_seconds": f"{float(row['start_elapsed_seconds']):.6f}",
                "end_elapsed_seconds": f"{float(row['end_elapsed_seconds']):.6f}",
                "duration_seconds": f"{float(row['duration_seconds']):.6f}",
                "resume_saved_frame": row.get("resume_saved_frame", ""),
            })

    capture_end_epoch = time.time()
    try:
        clock_data = json.loads(clock_path.read_text(encoding="utf-8"))
    except Exception:
        clock_data = {"start_epoch": start_epoch}
    clock_data["end_epoch"] = capture_end_epoch
    clock_data["capture_elapsed_seconds"] = capture_elapsed
    clock_data["focus_interruptions"] = len(focus_interruptions)
    clock_data["focus_interruption_seconds"] = focus_pause_total
    clock_path.write_text(json.dumps(clock_data, indent=2), encoding="utf-8")

    print()
    print("Finalizing recording...")

    try:
        if proc.stdin:
            proc.stdin.close()
    except Exception:
        pass

    try:
        proc.wait(timeout=60)
    except subprocess.TimeoutExpired:
        proc.terminate()
        proc.wait(timeout=10)

    ffmpeg_log.close()

    end = time.perf_counter()
    finalize_elapsed = max(0.0, end - start - capture_elapsed)
    run_elapsed = capture_elapsed
    size_mb = video_path.stat().st_size / (1024 * 1024) if video_path.exists() else 0.0

    summary = [
        "DDO optimized combat recording",
        f"Session: {stamp}",
        f"Capture area: {cap}",
        f"Target FPS: {fps}",
        f"Captured frames: {captured}",
        f"Saved frames: {saved}",
        f"Skipped frames: {skipped}",
        f"Forced anchors: {forced}",
        f"Elapsed seconds: {run_elapsed:.3f}",
        f"Focus interruptions: {len(focus_interruptions)}",
        f"Focus interruption seconds: {focus_pause_total:.3f}",
        f"Active capture seconds: {max(0.001, run_elapsed - focus_pause_total):.3f}",
        f"Finalization seconds: {finalize_elapsed:.3f}",
        f"Average active capture FPS: {captured/max(0.001, run_elapsed-focus_pause_total):.3f}",
        f"Frames > one interval late: {late_frames}",
        f"Maximum lateness: {max_lateness*1000:.3f} ms",
        f"Video size: {size_mb:.1f} MB",
        f"FFmpeg exit code: {proc.returncode}",
    ]
    summary_path.write_text("\n".join(summary) + "\n", encoding="utf-8")

    if proc.returncode != 0:
        print(f"FFmpeg exit code {proc.returncode}. Analysis will not start.")
        raise SystemExit(3)

    # Pointer file lets the analyzer find the exact session just recorded.
    (HERE / "LATEST_SESSION.txt").write_text(str(session), encoding="utf-8")

    print()
    print(f"Recording saved: {session}")
    print(f"Saved frames: {saved}/{captured}")
    print(f"Video size: {size_mb:.1f} MB")
    print()


if __name__ == "__main__":
    main()
