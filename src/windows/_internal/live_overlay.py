from __future__ import annotations

import argparse
import csv
import ctypes
import json
import itertools
import math
import os
import queue
import re
import shutil
import threading
import time
from collections import deque
from pathlib import Path

import cv2
import mss
import numpy as np
try:
    import indra_ocr
except Exception:
    indra_ocr = None
import tkinter as tk

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "config.json"
DEFAULT_LIVE_WINDOW_SECONDS = 15.0

from cpu_utils import resolve_indra_threads

OCR_DIGIT_MAP = str.maketrans({
    "S": "5",
    "s": "5",
    "O": "0",
    "o": "0",
    "I": "1",
    "l": "1",
    "|": "1",
})
NUM_TOKEN = r"[0-9SOsoIl|,]+"

OUT_RE = re.compile(
    rf"\bYou\s+(?:sneak\s+attack|hit)\s+"
    rf"(?P<target>.+?)\s+for\s+"
    rf"(?P<amount>{NUM_TOKEN})\s+points?\s+of\s+"
    rf"(?P<dtype>[A-Za-z][A-Za-z -]*?)\s+damage\b",
    re.IGNORECASE,
)
OUT_UNTYPED_RE = re.compile(
    rf"\bYou\s+(?:sneak\s+attack|hit)\s+"
    rf"(?P<target>.+?)\s+for\s+"
    rf"(?P<amount>{NUM_TOKEN})\s+points?\s+of\s+damage\b",
    re.IGNORECASE,
)

# Live numeric verification only needs to recover the amount token, even when a
# long DDO damage message wraps before the final word "damage.".
OUT_AMOUNT_LOOSE_RE = re.compile(
    rf"\bYou\s+(?:sneak\s+attack|hit)\s+.+?\s+for\s+"
    rf"(?P<amount>{NUM_TOKEN})\s+points?\b",
    re.IGNORECASE,
)

OUT_WRAP_TYPED_PREFIX_RE = re.compile(
    rf"\bYou\s+(?:sneak\s+attack|hit)\s+.+?\s+for\s+"
    rf"{NUM_TOKEN}\s+points?\s+of\s+[A-Za-z][A-Za-z -]*?\s*$",
    re.IGNORECASE,
)
OUT_WRAP_BARE_PREFIX_RE = re.compile(
    rf"\bYou\s+(?:sneak\s+attack|hit)\s+.+?\s+for\s+"
    rf"{NUM_TOKEN}\s+points?\s+of\s*$",
    re.IGNORECASE,
)


def damage_wrap_kind(line):
    line = normalize_text(line)
    if OUT_WRAP_TYPED_PREFIX_RE.search(line):
        return "typed"
    if OUT_WRAP_BARE_PREFIX_RE.search(line):
        return "bare"
    return None


def join_damage_wrap(prefix, continuation, kind=None):
    prefix = normalize_text(prefix)
    cont = normalize_text(continuation)
    cont = re.sub(r"^[\|\[\]{}Il1:;`'.,_-]+\s*", "", cont).strip()
    kind = kind or damage_wrap_kind(prefix)
    if kind == "typed" and re.match(r"^damage\b", cont, re.IGNORECASE):
        return normalize_text(prefix + " " + cont)
    if kind == "bare" and re.match(r"^[A-Za-z][A-Za-z -]*?\s+damage\b", cont, re.IGNORECASE):
        return normalize_text(prefix + " " + cont)
    return None

ATTACK_RE = re.compile(
    rf"\bYou\s+attack\s+(?P<target>.+?)\.\s+You\s+roll\s+a\s+"
    rf"(?P<roll>{NUM_TOKEN})"
    rf"(?:\s*\(\+(?P<bonus>{NUM_TOKEN})\))?\s*:\s*"
    rf"(?P<result>.+)$",
    re.IGNORECASE,
)


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
    """True only while the actual DDO client is the Windows foreground window."""
    if os.name != "nt":
        return True
    hwnd = find_ddo_window()
    if not hwnd:
        return False
    try:
        return int(ctypes.windll.user32.GetForegroundWindow()) == int(hwnd)
    except Exception:
        return False


def set_low_priority():
    """Keep the live process below the precise recorder in Windows scheduling."""
    if os.name != "nt":
        return
    try:
        BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetCurrentProcess()
        kernel32.SetPriorityClass(handle, BELOW_NORMAL_PRIORITY_CLASS)
    except Exception:
        pass



DDO_NUMBER_TOKEN_RE = re.compile(r"^\d{1,3}(?:,\d{3})*$")


def normalize_numeric_token(token):
    """Normalize OCR lookalikes but preserve DDO thousands separators."""
    return (token or "").translate(OCR_DIGIT_MAP).strip()


def valid_ddo_number_token(token):
    """True only for the number format DDO actually displays.

    DDO uses comma thousands separators consistently, so values above 999 must
    contain the required comma(s): 1,234; 12,345; 1,234,567; etc.  This catches
    the observed live OCR failure where a comma is read as the digit 1.
    """
    normalized = normalize_numeric_token(token)
    return bool(DDO_NUMBER_TOKEN_RE.fullmatch(normalized))


def parse_number(token):
    corrected = normalize_numeric_token(token).replace(",", "")
    return int(corrected) if corrected.isdigit() else None


def extract_outgoing_amount_token(line, loose=False):
    if loose:
        m = OUT_AMOUNT_LOOSE_RE.search(line or "")
    else:
        m = OUT_RE.search(line or "") or OUT_UNTYPED_RE.search(line or "")
    return m.group("amount") if m else None


def parse_outgoing_damage_details(line, loose=False):
    token = extract_outgoing_amount_token(line, loose=loose)
    if token is None:
        return None, "", False
    return parse_number(token), token, valid_ddo_number_token(token)


def parse_outgoing_damage(line):
    return parse_outgoing_damage_details(line, loose=False)[0]


def parse_outgoing_amount_loose(line):
    return parse_outgoing_damage_details(line, loose=True)[0]


def comma_as_one_candidates(token, max_replacements=3):
    """Return valid DDO-number candidates if OCR read comma(s) as digit 1.

    Example: 11388 -> 1,388 and 21319 -> 2,319.  The function never changes a
    token that is already valid.  Candidates are used only after an isolated
    verification pass fails to recover explicit comma punctuation.
    """
    normalized = normalize_numeric_token(token)
    if not normalized or valid_ddo_number_token(normalized):
        return set()
    positions = [i for i, ch in enumerate(normalized) if ch == "1"]
    candidates = set()
    for count in range(1, min(max_replacements, len(positions)) + 1):
        for combo in itertools.combinations(positions, count):
            chars = list(normalized)
            for idx in combo:
                chars[idx] = ","
            candidate = "".join(chars)
            if valid_ddo_number_token(candidate):
                value = parse_number(candidate)
                if value is not None:
                    candidates.add((value, candidate))
    return candidates


def duplicated_one_collapse_candidates(amount):
    """Return values obtained by collapsing one adjacent '11' to '1'.

    Live batch OCR has repeatedly duplicated the glyph for digit 1.  Examples
    observed in real combat data include 1,xxx -> 11,xxx and 9,197 -> 91,197.
    We never apply a collapse from this function alone; it only defines the
    exact alternatives that an isolated single-line OCR pass may confirm.
    """
    if amount is None:
        return set()
    digits = str(int(amount))
    candidates = set()
    start = 0
    while True:
        i = digits.find("11", start)
        if i < 0:
            break
        collapsed = digits[:i] + "1" + digits[i + 2:]
        if collapsed and collapsed.isdigit():
            candidates.add(int(collapsed))
        start = i + 1
    candidates.discard(int(amount))
    return candidates


def needs_live_numeric_recheck(amount):
    # Re-check any damage value containing adjacent 11.  This covers the known
    # live-only OCR failure 1 -> 11 wherever it occurs inside a larger number,
    # while leaving all other damage values on the fast batch-OCR path.
    return bool(duplicated_one_collapse_candidates(amount))


def parse_attack_line(line):
    """Return True for one player attack-roll line. Damage/proc lines do not count."""
    return ATTACK_RE.search(line) is not None


def normalize_text(s):
    s = s.replace("\r", " ").replace("\n", " ")
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"^[\|\[\]{}Il1:;`'.,_-]+\s*(?=\(Combat\))", "", s)
    return s


# ---------------------------------------------------------------------------
# Fast visual line tracking. No OCR is performed in this path.
# ---------------------------------------------------------------------------

def fast_text_feature(gray, threshold=8):
    """Cheap high-frequency text map suitable for 30-fps tracking."""
    blur = cv2.GaussianBlur(gray, (3, 3), 0)
    feat = cv2.absdiff(gray, blur)
    _, feat = cv2.threshold(feat, threshold, 255, cv2.THRESH_TOZERO)
    b = 5
    if feat.shape[0] > 2 * b and feat.shape[1] > 2 * b:
        feat[:b, :] = 0
        feat[-b:, :] = 0
        feat[:, :b] = 0
        feat[:, -b:] = 0
    return feat




def detect_line_height_from_gray(gray, fallback=16, min_h=10, max_h=22):
    """Estimate DDO chat line spacing from vertical periodicity of text edges.

    Returns (line_height, score, margin, confident). The detector is deliberately
    conservative: if the periodic signal is weak or ambiguous, callers keep the
    configured fallback.
    """
    if gray is None or gray.size == 0 or gray.shape[0] < max_h * 6:
        return int(fallback), 0.0, 0.0, False
    blur = cv2.GaussianBlur(gray, (3, 3), 0)
    feat = cv2.absdiff(gray, blur).astype(np.float32)
    energy = feat.mean(axis=1)
    x = energy - float(np.mean(energy))
    denom0 = float(np.linalg.norm(x))
    if denom0 < 1e-6:
        return int(fallback), 0.0, 0.0, False

    scored = []
    for h in range(int(min_h), int(max_h) + 1):
        a = x[:-h]
        b = x[h:]
        denom = float(np.linalg.norm(a) * np.linalg.norm(b))
        score = float(np.dot(a, b) / denom) if denom > 1e-9 else -1.0
        scored.append((score, h))
    scored.sort(reverse=True)
    best_score, best_h = scored[0]
    second_score = scored[1][0] if len(scored) > 1 else -1.0
    margin = float(best_score - second_score)
    confident = bool(best_score >= 0.80 and margin >= 0.08)
    return int(best_h if confident else fallback), float(best_score), margin, confident

def best_grid_phase_from_feature(feat, line_height):
    energy = feat.mean(axis=1)
    scores = []
    for phase in range(line_height):
        vals = energy[phase::line_height]
        scores.append(float(np.mean(vals)) if len(vals) else 1e9)
    return int(np.argmin(scores))


def _shift_error(prev_feat, curr_feat, shift):
    # Downsample aggressively for speed. We only need vertical scroll distance,
    # not OCR-quality pixels.
    if shift == 0:
        a = prev_feat[::2, ::4]
        b = curr_feat[::2, ::4]
    else:
        a = prev_feat[shift::2, ::4]
        b = curr_feat[:-shift:2, ::4]
    if a.size == 0 or b.size == 0 or a.shape != b.shape:
        return 1e9
    return float(np.mean(np.abs(a.astype(np.int16) - b.astype(np.int16))))


def estimate_shift_fast(prev_feat, curr_feat, line_height, max_shift_px):
    # Require at least four text rows of overlap so very large shifts cannot win
    # merely because only a tiny image strip remains to compare.
    max_shift = min(max_shift_px, prev_feat.shape[0] - 4 * line_height)
    max_shift = max(0, (max_shift // line_height) * line_height)
    candidates = [(s, _shift_error(prev_feat, curr_feat, s))
                  for s in range(0, max_shift + 1, line_height)]
    zero_error = candidates[0][1]
    best_shift, best_error = min(candidates, key=lambda x: x[1])

    if best_shift == 0:
        return 0, best_error, zero_error, "zero"

    # A genuine scroll normally produces a dramatic error reduction. This
    # relative test is intentionally conservative to avoid duplicate OCR lines.
    ratio = best_error / max(zero_error, 1e-6)
    if ratio > 0.62:
        return 0, best_error, zero_error, "uncertain->zero"

    # If several shifts are nearly identical, prefer the smallest positive one.
    tolerance = max(0.25, best_error * 0.12)
    close = [(s, e) for s, e in candidates if s > 0 and e <= best_error + tolerance]
    if close:
        best_shift, best_error = min(close, key=lambda x: x[0])
    return best_shift, best_error, zero_error, "positive"


# Robust post-analysis alignment, used only as a second opinion when the cheap
# 30-fps live matcher returns uncertain->zero.  This is deliberately not run on
# normal live frames.
def robust_text_feature(gray, threshold=18):
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (11, 7))
    top_hat = cv2.morphologyEx(gray, cv2.MORPH_TOPHAT, kernel)
    blur = cv2.GaussianBlur(gray, (0, 0), 2.0)
    high = cv2.absdiff(gray, blur)
    feat = cv2.max(top_hat, high)
    _, feat = cv2.threshold(feat, threshold, 255, cv2.THRESH_TOZERO)
    b = 6
    if feat.shape[0] > 2 * b and feat.shape[1] > 2 * b:
        feat[:b, :] = 0
        feat[-b:, :] = 0
        feat[:, :b] = 0
        feat[:, -b:] = 0
    return feat.astype(np.float32) / 255.0


def _robust_alignment_score(prev_feat, curr_feat, shift):
    if shift == 0:
        a = prev_feat
        b = curr_feat
    else:
        if shift >= prev_feat.shape[0] - 32:
            return 0.0
        a = prev_feat[shift:]
        b = curr_feat[:-shift]
    mx = max(5, int(a.shape[1] * 0.025))
    if a.shape[1] > 2 * mx + 20:
        a = a[:, mx:-mx]
        b = b[:, mx:-mx]
    aa = float(np.sum(a * a))
    bb = float(np.sum(b * b))
    if aa < 1e-9 or bb < 1e-9:
        return 0.0
    return float(np.sum(a * b) / math.sqrt(aa * bb))


def _robust_alignment_scores_fast(prev_feat, curr_feat, shifts):
    h, w = prev_feat.shape[:2]
    mx = max(5, int(w * 0.025))
    if w > 2 * mx + 20:
        prev = np.ascontiguousarray(prev_feat[:, mx:-mx])
        curr = np.ascontiguousarray(curr_feat[:, mx:-mx])
    else:
        prev = np.ascontiguousarray(prev_feat)
        curr = np.ascontiguousarray(curr_feat)

    prev_row_sq = np.einsum("ij,ij->i", prev, prev, optimize=True).astype(np.float64, copy=False)
    curr_row_sq = np.einsum("ij,ij->i", curr, curr, optimize=True).astype(np.float64, copy=False)
    prev_sq_prefix = np.empty(h + 1, dtype=np.float64)
    curr_sq_prefix = np.empty(h + 1, dtype=np.float64)
    prev_sq_prefix[0] = 0.0
    curr_sq_prefix[0] = 0.0
    np.cumsum(prev_row_sq, out=prev_sq_prefix[1:])
    np.cumsum(curr_row_sq, out=curr_sq_prefix[1:])

    scored = []
    for shift in shifts:
        shift = int(shift)
        if shift != 0 and shift >= h - 32:
            scored.append((shift, 0.0))
            continue
        overlap_h = h - shift
        aa = float(prev_sq_prefix[h] - prev_sq_prefix[shift])
        bb = float(curr_sq_prefix[overlap_h])
        if aa < 1e-9 or bb < 1e-9:
            score = 0.0
        else:
            dot = float(np.dot(prev[shift:].reshape(-1), curr[:overlap_h].reshape(-1)))
            score = dot / math.sqrt(aa * bb)
        scored.append((shift, float(score)))
    return scored


def estimate_shift_robust_live(prev_gray, curr_gray, line_height, max_shift_px, analysis_cfg):
    prev_feat = robust_text_feature(prev_gray)
    curr_feat = robust_text_feature(curr_gray)
    max_shift = min(int(max_shift_px), prev_feat.shape[0] - 48)
    max_shift = max(0, (max_shift // int(line_height)) * int(line_height))
    min_score = float(analysis_cfg.get("min_alignment_score", 0.80))
    min_improvement = float(analysis_cfg.get("min_improvement_over_zero", 0.035))
    close_tol = float(analysis_cfg.get("close_score_tolerance", 0.008))

    zero = _robust_alignment_score(prev_feat, curr_feat, 0)
    zero_first_threshold = float(analysis_cfg.get("zero_first_threshold", 0.995))
    if bool(analysis_cfg.get("zero_first_enabled", True)) and zero >= zero_first_threshold:
        return 0, zero, zero, "zero-fast", True, 1.0, 0.0

    candidates = list(range(0, max_shift + 1, int(line_height)))
    scores = _robust_alignment_scores_fast(prev_feat, curr_feat, candidates)
    scores = [(sh, zero if sh == 0 else sc) for sh, sc in scores]
    ranked = sorted(scores, key=lambda x: x[1], reverse=True)
    best_shift, best_score = ranked[0]
    second_score = ranked[1][1] if len(ranked) > 1 else 0.0
    margin = float(best_score - second_score)

    if best_shift == 0:
        shift, chosen_score, decision = 0, best_score, "zero"
    elif best_score < min_score or (best_score - zero) < min_improvement:
        shift, chosen_score, decision = 0, best_score, "uncertain->zero"
    else:
        close = [
            (sh, sc) for sh, sc in scores
            if sh > 0 and sc >= best_score - close_tol and sc >= min_score
        ]
        if close:
            shift, chosen_score = min(close, key=lambda x: x[0])
        else:
            shift, chosen_score = best_shift, best_score
        decision = "positive"

    confident_score = float(analysis_cfg.get("whole_frame_confident_score", 0.97))
    confident_margin = float(analysis_cfg.get("whole_frame_confident_margin", 0.04))
    confident_positive_improvement = float(
        analysis_cfg.get("whole_frame_confident_positive_improvement", 0.08)
    )
    confident = bool(
        decision in ("zero", "positive")
        and chosen_score >= confident_score
        and margin >= confident_margin
        and (shift == 0 or (chosen_score - zero) >= confident_positive_improvement)
    )
    return shift, chosen_score, zero, decision, confident, margin, second_score


def line_cells(gray, phase, line_height):
    h = gray.shape[0]
    cells = []
    y = phase
    while y < 0:
        y += line_height
    while y + line_height <= h:
        cells.append((y, y + line_height))
        y += line_height
    return cells


def bottom_partial_line_info_live(gray, feat, phase, line_height):
    """Return a text-bearing >=70%-visible bottom fragment, if present."""
    cells = line_cells(gray, phase, line_height)
    partial_y0 = cells[-1][1] if cells else int(phase)
    h = int(gray.shape[0])
    partial_h = h - int(partial_y0)
    min_partial_h = max(8, int(math.ceil(float(line_height) * 0.70)))
    if partial_h < min_partial_h or partial_h >= int(line_height):
        return None
    level = float(np.mean(feat[partial_y0:h])) / 255.0
    if level < 0.003:
        return None
    return int(partial_y0), int(h), level


def crop_new_lines(gray, feat, shift, phase, line_height):
    """Return (full_crops, deferred_partial_y0) for newly scrolled rows.

    A partial newest row reserves one of the newly scrolled line slots but is
    deferred until a later frame exposes the same row at full height.  This
    avoids OCR'ing a clipped row and avoids substituting an older duplicate.
    """
    n = int(round(shift / line_height))
    if n <= 0:
        return [], None
    cells = line_cells(gray, phase, line_height)
    partial = bottom_partial_line_info_live(gray, feat, phase, line_height)
    deferred_y0 = partial[0] if partial is not None else None
    full_rows_needed = max(0, n - (1 if partial is not None else 0))
    if full_rows_needed <= 0:
        return [], deferred_y0

    scored = []
    for y0, y1 in cells:
        sc = float(np.mean(feat[y0:y1])) / 255.0
        scored.append((sc, y0, y1))

    selected = []
    for sc, y0, y1 in reversed(scored):
        if sc < 0.003:
            continue
        selected.append((y0, y1))
        if len(selected) >= full_rows_needed:
            break
    selected.reverse()
    return [gray[y0:y1].copy() for y0, y1 in selected], deferred_y0




def crop_blank_to_filled_lines(prev_feat, curr_gray, curr_feat, phase, line_height):
    """Catch appends while the visible log is not yet full and therefore does not scroll."""
    crops = []
    for y0, y1 in line_cells(curr_gray, phase, line_height):
        prev_level = float(np.mean(prev_feat[y0:y1])) / 255.0
        curr_level = float(np.mean(curr_feat[y0:y1])) / 255.0
        if prev_level < 0.0020 and curr_level >= 0.0030:
            crops.append(curr_gray[y0:y1].copy())
    return crops

def preprocess_line(line_gray):
    line = cv2.copyMakeBorder(line_gray, 4, 4, 3, 3, cv2.BORDER_REPLICATE)
    line = cv2.resize(line, None, fx=4.0, fy=4.0, interpolation=cv2.INTER_CUBIC)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (21, 13))
    top = cv2.morphologyEx(line, cv2.MORPH_TOPHAT, kernel)
    top = cv2.GaussianBlur(top, (3, 3), 0)
    _, bw = cv2.threshold(top, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return 255 - bw



def preprocess_line_indra(line_gray):
    """Lighter preprocessing for Indra: black DDO text on a white background."""
    line = cv2.copyMakeBorder(line_gray, 3, 3, 3, 3, cv2.BORDER_REPLICATE)
    line = cv2.resize(line, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (13, 9))
    top = cv2.morphologyEx(line, cv2.MORPH_TOPHAT, kernel)
    top = cv2.GaussianBlur(top, (3, 3), 0)
    _, bw = cv2.threshold(top, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return 255 - bw


def build_indra_batch(items, gap=14):
    """Stack DDO line crops vertically and keep y-ranges for timestamp mapping."""
    # Queue items may carry an optional completion Event in slot 2 for the
    # Stop-flushed pending bottom row. OCR pixels are always item[1].
    prepared = [preprocess_line_indra(item[1]) for item in items]
    if not prepared:
        return None, []
    width = max(im.shape[1] for im in prepared)
    height = sum(im.shape[0] for im in prepared) + gap * (len(prepared) + 1)
    canvas = np.full((height, width), 255, dtype=np.uint8)
    slots = []
    y = gap
    for im in prepared:
        h, w = im.shape[:2]
        canvas[y:y+h, 0:w] = im
        slots.append((y, y+h))
        y += h + gap
    return canvas, slots


# ---------------------------------------------------------------------------
# Overlay helpers
# ---------------------------------------------------------------------------

def choose_monitor(monitors, capture):
    cx = int(capture["left"]) + int(capture["width"]) // 2
    cy = int(capture["top"]) + int(capture["height"]) // 2
    for mon in monitors[1:]:
        if mon["left"] <= cx < mon["left"] + mon["width"] and mon["top"] <= cy < mon["top"] + mon["height"]:
            return mon
    return monitors[1]


def intersects(a, b, pad=0):
    ax1, ay1, aw, ah = a
    bx1, by1, bw, bh = b
    ax2, ay2 = ax1 + aw, ay1 + ah
    bx2, by2 = bx1 + bw, by1 + bh
    return not (ax2 + pad <= bx1 or bx2 + pad <= ax1 or ay2 + pad <= by1 or by2 + pad <= ay1)


def choose_overlay_position(mon, capture, width, height, margin):
    candidates = [
        (mon["left"] + mon["width"] - width - margin, mon["top"] + margin),
        (mon["left"] + margin, mon["top"] + margin),
        (mon["left"] + mon["width"] - width - margin, mon["top"] + mon["height"] - height - margin),
        (mon["left"] + margin, mon["top"] + mon["height"] - height - margin),
    ]
    cap_rect = (int(capture["left"]), int(capture["top"]), int(capture["width"]), int(capture["height"]))
    for x, y in candidates:
        if not intersects((x, y, width, height), cap_rect, pad=12):
            return x, y
    return candidates[0]


def make_clickthrough(root):
    if os.name != "nt":
        return
    try:
        root.update_idletasks()
        hwnd = root.winfo_id()
        GWL_EXSTYLE = -20
        WS_EX_TRANSPARENT = 0x00000020
        WS_EX_TOOLWINDOW = 0x00000080
        WS_EX_NOACTIVATE = 0x08000000
        user32 = ctypes.windll.user32
        style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        # Do not force WS_EX_LAYERED here. Tk manages layered composition itself
        # for transparent-color/alpha windows; forcing it after widget creation
        # can leave a visible rectangle while child/text drawing disappears.
        style |= WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
    except Exception:
        pass


class LiveEstimator:
    def __init__(self, session: Path, no_overlay=False):
        self.session = session
        self.cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
        self.capture = self.cfg["capture"]
        self.live_cfg = self.cfg.get("live_dps", {})
        self.analysis_cfg = self.cfg.get("analysis", {})

        self.window_seconds = max(1.0, float(self.live_cfg.get('rolling_window_seconds', DEFAULT_LIVE_WINDOW_SECONDS)))
        # Retain enough recent events to allow the user to increase the rolling
        # window while continuous live mode is already running.
        self.history_seconds = max(60.0, self.window_seconds)
        self.tracking_fps = max(10, int(self.live_cfg.get("tracking_fps", self.cfg.get("capture_fps", 30))))
        self.tracking_interval = 1.0 / self.tracking_fps
        self.line_height = max(8, int(self.analysis_cfg.get("line_height", 16)))
        self.auto_line_height = bool(self.analysis_cfg.get("auto_line_height", True))
        self.line_height_locked = not self.auto_line_height
        self.line_height_candidates = []
        self.max_shift_px = min(
            int(self.live_cfg.get("max_live_scroll_pixels", 384)),
            int(self.analysis_cfg.get("max_scroll_pixels", 384)),
        )
        self.min_score = float(self.live_cfg.get("min_alignment_score", self.analysis_cfg.get("min_alignment_score", 0.80)))
        self.min_improvement = float(self.live_cfg.get("min_improvement_over_zero", 0.020))
        self.close_tol = float(self.live_cfg.get("close_score_tolerance", self.analysis_cfg.get("close_score_tolerance", 0.008)))
        self.overlay_enabled = bool(self.live_cfg.get("overlay", True)) and not no_overlay
        self.overlay_width = int(self.live_cfg.get("overlay_width", 300))
        self.overlay_height = int(self.live_cfg.get("overlay_height", 92))
        self.overlay_margin = int(self.live_cfg.get("overlay_margin", 20))
        self.ocr_queue_max = max(64, int(self.live_cfg.get("ocr_queue_max_lines", 512)))
        self.ocr_engine_preference = str(self.live_cfg.get("ocr_engine", "indra")).strip().lower()
        self.ocr_batch_max = max(1, min(32, int(self.live_cfg.get("ocr_batch_max_lines", 12))))
        self.indra_threads_setting = self.live_cfg.get("indra_threads", "auto")
        self.indra_threads = resolve_indra_threads(self.indra_threads_setting)
        self.indra_converter = None
        self.ocr_engine_active = "starting"

        if indra_ocr is None:
            raise RuntimeError("Indra OCR is not installed. Run SETUP DDO Combat Analyzer.cmd.")

        self.clock_path = self.session / "recording_clock.json"
        self.stop_path = self.session / "live_stop.flag"
        self.live_path = self.session / "live_dps.csv"
        # Full per-event live damage diagnostics are buffered in RAM while
        # combat is running and written once at shutdown.  This avoids adding
        # per-hit disk I/O to the latency-sensitive live OCR path.
        self.live_event_path = self.session / "live_damage_events.csv"
        self.live_event_records = []
        self.live_event_records_lock = threading.Lock()
        self.live_raw_damage_total = 0
        self.live_corrected_damage_total = 0
        self.scan_path = Path(os.devnull)
        self.track_path = Path(os.devnull)
        self.status_path = None
        self.overlay_error_path = self.session / "live_overlay_error.txt"

        self.start_epoch = None
        self.events = deque()  # (capture_elapsed_seconds, amount)
        self.attacks = deque()  # capture_elapsed_seconds for player attack-roll lines
        self.pending_damage_wrap = None  # (elapsed, text, kind, crop)
        self.pending_bottom_lock = threading.Lock()
        self.pending_bottom_line = None  # y0, elapsed, partial_crop, line_height
        self.bottom_deferred_total = 0
        self.bottom_recovered_total = 0
        self.bottom_stop_flushed_total = 0
        self.bottom_dropped_total = 0
        self.last_bottom_flush_event = None
        self.robust_fallback_runs = 0
        self.robust_fallback_overrides = 0
        self.events_lock = threading.Lock()
        self.ocr_queue = queue.Queue(maxsize=self.ocr_queue_max)
        self.update_queue = queue.Queue()
        self.stop_event = threading.Event()
        self.status_lock = threading.Lock()

        self.detected_lines_total = 0
        self.ocr_lines_total = 0
        self.parsed_events_total = 0
        self.parsed_attacks_total = 0
        self.dropped_lines_total = 0
        self.tracking_frames = 0
        self.tracking_late_frames = 0
        self.tracking_max_lateness_ms = 0.0

        self.root = None
        self.overlay_canvas = None
        self.value_item = None
        self.apm_item = None
        self.detail_item = None

    def write_status(self, **updates):
        return

    def wait_for_clock(self):
        deadline = time.time() + 8.0
        while time.time() < deadline and not self.stop_event.is_set():
            if self.clock_path.exists():
                try:
                    data = json.loads(self.clock_path.read_text(encoding="utf-8"))
                    self.start_epoch = float(data["start_epoch"])
                    return True
                except Exception:
                    pass
            time.sleep(0.05)
        return False

    def _enqueue_one_line(self, crop, elapsed, block_timeout=0.0, completion_event=None):
        item = (float(elapsed), crop, completion_event)
        try:
            if block_timeout and block_timeout > 0:
                self.ocr_queue.put(item, timeout=float(block_timeout))
            else:
                self.ocr_queue.put_nowait(item)
            self.detected_lines_total += 1
            return True
        except queue.Full:
            self.dropped_lines_total += 1
            self.write_status(
                queue_overflow=True,
                dropped_lines_total=self.dropped_lines_total,
                ocr_queue_depth=self.ocr_queue.qsize(),
            )
            return False

    def enqueue_new_lines(self, crops, elapsed):
        for crop in crops:
            self._enqueue_one_line(crop, elapsed)

    def clear_pending_bottom_line(self, count_drop=False):
        with self.pending_bottom_lock:
            had_pending = self.pending_bottom_line is not None
            self.pending_bottom_line = None
        if had_pending and count_drop:
            self.bottom_dropped_total += 1
        return had_pending

    def defer_pending_bottom_line(self, gray, y0, elapsed):
        y0 = int(y0)
        if y0 < 0 or y0 >= gray.shape[0]:
            return False
        partial = gray[y0:].copy()
        with self.pending_bottom_lock:
            self.pending_bottom_line = {
                "y0": y0,
                "elapsed": float(elapsed),
                "partial_crop": partial,
                "line_height": int(self.line_height),
            }
        self.bottom_deferred_total += 1
        return True

    def recover_pending_bottom_line(self, gray, feat, shift):
        if shift <= 0:
            return None
        with self.pending_bottom_lock:
            pending = self.pending_bottom_line
            if pending is None:
                return None
            recovered_y0 = int(pending["y0"] - int(shift))
            recovered_y1 = recovered_y0 + int(pending.get("line_height", self.line_height))
            elapsed = float(pending["elapsed"])
            if 0 <= recovered_y0 and recovered_y1 <= gray.shape[0]:
                level = float(np.mean(feat[recovered_y0:recovered_y1])) / 255.0
                if level >= 0.003:
                    crop = gray[recovered_y0:recovered_y1].copy()
                    self.pending_bottom_line = None
                    self.bottom_recovered_total += 1
                    return elapsed, crop
            self.pending_bottom_line = None
            self.bottom_dropped_total += 1
            return None

    def flush_pending_bottom_line(self, max_elapsed=None):
        """OCR one still-clipped newest row when Stop prevents later recovery."""
        with self.pending_bottom_lock:
            pending = self.pending_bottom_line
            if pending is None:
                return False
            elapsed = float(pending["elapsed"])
            if max_elapsed is not None and elapsed > float(max_elapsed) + 0.05:
                return False
            crop = pending.get("partial_crop")
            self.pending_bottom_line = None
        if crop is None or crop.size == 0:
            self.bottom_dropped_total += 1
            return False
        completion_event = threading.Event()
        if self._enqueue_one_line(
            crop, elapsed, block_timeout=0.50, completion_event=completion_event
        ):
            self.last_bottom_flush_event = completion_event
            self.bottom_stop_flushed_total += 1
            self.write_status(
                bottom_stop_flushed_total=self.bottom_stop_flushed_total,
                bottom_pending=False,
            )
            return True
        return False

    def current_metrics(self, now_elapsed):
        cutoff = now_elapsed - self.window_seconds
        history_cutoff = now_elapsed - max(self.history_seconds, self.window_seconds)
        with self.events_lock:
            while self.events and self.events[0][0] <= history_cutoff:
                self.events.popleft()
            while self.attacks and self.attacks[0] <= history_cutoff:
                self.attacks.popleft()
            window_damage = sum(amount for ts, amount in self.events if ts > cutoff)
            window_attacks = sum(1 for ts in self.attacks if ts > cutoff)
        dps = window_damage / self.window_seconds
        apm = window_attacks * 60.0 / self.window_seconds
        return dps, window_damage, apm, window_attacks

    def record_live_damage_event(
        self,
        event_elapsed,
        detected_elapsed,
        raw_amount,
        final_amount,
        correction,
        recheck_result,
        verification_amount,
        batch_ocr_text,
        verification_ocr_text,
    ):
        """Buffer one parsed live damage event entirely in RAM.

        The list is intentionally not written here: live OCR/tracking should not
        pay for a disk flush per hit.  write_live_damage_events() persists the
        whole buffer after the OCR queue has drained at Stop & Analyse.
        """
        raw_amount = int(raw_amount)
        final_amount = int(final_amount)
        with self.live_event_records_lock:
            self.live_raw_damage_total += raw_amount
            self.live_corrected_damage_total += final_amount
            event_number = len(self.live_event_records) + 1
            self.live_event_records.append((
                event_number,
                float(event_elapsed),
                float(detected_elapsed),
                raw_amount,
                final_amount,
                self.live_raw_damage_total,
                self.live_corrected_damage_total,
                correction or "",
                recheck_result or "",
                verification_amount if verification_amount is not None else "",
                batch_ocr_text or "",
                verification_ocr_text or "",
            ))

    def write_live_damage_events(self):
        """Write the buffered per-hit live damage log once, after combat."""
        with self.live_event_records_lock:
            rows = list(self.live_event_records)
        self.live_event_path.parent.mkdir(parents=True, exist_ok=True)
        with self.live_event_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow([
                "event_number",
                "event_capture_elapsed_seconds",
                "detected_capture_elapsed_seconds",
                "raw_damage",
                "final_damage",
                "cumulative_raw_damage",
                "cumulative_final_damage",
                "correction_applied",
                "numeric_recheck_result",
                "verification_damage",
                "batch_ocr_text",
                "verification_ocr_text",
            ])
            for row in rows:
                row = list(row)
                row[1] = f"{row[1]:.6f}"
                row[2] = f"{row[2]:.6f}"
                w.writerow(row)

    def init_live_ocr_engine(self):
        """Load the persistent Indra OCR model used by live DPS."""
        if indra_ocr is None:
            raise RuntimeError("Indra OCR is not installed.")
        self.indra_converter = indra_ocr.load(threads=self.indra_threads, mode="ocr")
        self.ocr_engine_active = "indra"

    def ocr_indra_batch(self, items):
        canvas, slots = build_indra_batch(items)
        if canvas is None:
            return [""] * len(items)
        page = self.indra_converter.ocr(canvas)
        by_slot = [[] for _ in items]
        for line in getattr(page, "lines", []) or []:
            text = normalize_text(getattr(line, "text", "") or "")
            if not text:
                continue
            box = getattr(line, "box", None)
            if box is None:
                continue
            try:
                cy = (float(box[1]) + float(box[3])) / 2.0
            except Exception:
                continue
            best = None
            best_dist = 1e18
            for i, (y0, y1) in enumerate(slots):
                if y0 - 4 <= cy <= y1 + 4:
                    best = i
                    break
                center = (y0 + y1) / 2.0
                dist = abs(cy - center)
                if dist < best_dist:
                    best_dist = dist
                    best = i
            if best is not None:
                by_slot[best].append(text)
        return [normalize_text(" ".join(parts)) for parts in by_slot]

    def _ocr_indra_prepared(self, image):
        page = self.indra_converter.ocr(image)
        parts = []
        for detected in getattr(page, "lines", []) or []:
            text = normalize_text(getattr(detected, "text", "") or "")
            if text:
                parts.append(text)
        return normalize_text(" ".join(parts))

    def ocr_indra_single(self, crop):
        """OCR one line crop without batch neighbours for numeric verification."""
        return self._ocr_indra_prepared(preprocess_line_indra(crop))

    def ocr_indra_single_variants(self, crop):
        """Try several isolated renderings when numeric punctuation is suspect.

        Commas are tiny in the DDO font and can disappear or become a '1' after
        one thresholding path.  These variants are intentionally used only for
        suspicious numbers, so normal live OCR speed is unaffected.
        """
        variants = [preprocess_line_indra(crop), preprocess_line(crop)]
        raw = cv2.copyMakeBorder(crop, 4, 4, 4, 4, cv2.BORDER_REPLICATE)
        raw = cv2.resize(raw, None, fx=3.0, fy=3.0, interpolation=cv2.INTER_CUBIC)
        raw = cv2.normalize(raw, None, 0, 255, cv2.NORM_MINMAX)
        variants.append(raw)

        results = []
        seen = set()
        for image in variants:
            try:
                text = self._ocr_indra_prepared(image)
            except Exception:
                text = ""
            if text and text not in seen:
                seen.add(text)
                results.append(text)
        return results

    def ocr_worker(self):
        self.write_status(ocr_worker_started=True, ocr_worker_finished=False)
        self.init_live_ocr_engine()
        with self.scan_path.open("w", newline="", encoding="utf-8") as scan_f:
            scan_w = csv.writer(scan_f)
            scan_w.writerow([
                "capture_elapsed_seconds",
                "ocr_duration_ms",
                "parsed_damage",
                "queue_depth_after",
                "ocr_engine",
                "batch_size",
                "numeric_recheck",
                "numeric_recheck_amount",
                "numeric_recheck_ms",
                "numeric_recheck_text",
                "ocr_text",
            ])
            scan_f.flush()

            while not self.stop_event.is_set() or not self.ocr_queue.empty():
                try:
                    first = self.ocr_queue.get(timeout=0.08)
                except queue.Empty:
                    continue

                items = [first]
                while len(items) < self.ocr_batch_max:
                    try:
                        items.append(self.ocr_queue.get_nowait())
                    except queue.Empty:
                        break

                t0 = time.perf_counter()
                try:
                    texts = self.ocr_indra_batch(items)
                except Exception as exc:
                    self.update_queue.put(("error", f"Indra OCR error: {exc}"))
                    for item in items:
                        try:
                            completion_event = item[2] if len(item) > 2 else None
                            if completion_event is not None:
                                completion_event.set()
                        finally:
                            self.ocr_queue.task_done()
                    self.stop_event.set()
                    break

                batch_ms = (time.perf_counter() - t0) * 1000.0
                per_line_ms = batch_ms / max(1, len(items))
                for item, text in zip(items, texts):
                    elapsed, crop = item[0], item[1]
                    completion_event = item[2] if len(item) > 2 else None
                    self.last_ocr_elapsed_processed = float(elapsed)
                    live_accept_after = float(getattr(self, "live_accept_after_elapsed", -1e30))
                    accepted_for_live = float(elapsed) + 1e-6 >= live_accept_after
                    if accepted_for_live:
                        self.ocr_lines_total += 1
                    text = normalize_text(text) if text else ""
                    # Full-height rows are never repaired here.  A crop shorter
                    # than the locked line height can only be the Stop/EOF
                    # fallback for a pending bottom fragment.  On the validated
                    # reference session Indra consistently read g as q on such
                    # clipped rows (damage -> damaqe), so repair only that exact
                    # parser keyword in this unavoidable edge case.
                    if text and crop is not None and crop.shape[0] < int(self.line_height):
                        text = re.sub(r"\bdamaqe\b", "damage", text, flags=re.IGNORECASE)
                    parse_text = text
                    event_elapsed = elapsed
                    event_crop = crop
                    numeric_recheck = ""
                    numeric_recheck_amount = ""
                    numeric_recheck_ms = ""
                    numeric_recheck_text = ""

                    # Recover only the safe two-row damage wrap used by DDO,
                    # e.g. "... 37,225 points of pierce" + "damage.".
                    if self.pending_damage_wrap is not None:
                        p_elapsed, p_text, p_kind, p_crop = self.pending_damage_wrap
                        self.pending_damage_wrap = None
                        # Never bridge a two-line damage message across a live
                        # session boundary. A stale pre-boundary first half must
                        # not consume a fresh post-boundary line.
                        if float(p_elapsed) + 1e-6 >= live_accept_after:
                            joined = join_damage_wrap(p_text, text, p_kind)
                            if joined:
                                parse_text = joined
                                event_elapsed = p_elapsed
                                event_crop = p_crop

                    amount, amount_token, amount_format_valid = (
                        parse_outgoing_damage_details(parse_text)
                        if parse_text else (None, "", False)
                    )
                    raw_amount = amount
                    correction_applied = ""
                    if amount is None and parse_text == text and accepted_for_live:
                        wrap_kind = damage_wrap_kind(text)
                        if wrap_kind:
                            self.pending_damage_wrap = (elapsed, text, wrap_kind, crop)

                    # DDO always prints thousands separators.  A damage token such
                    # as 11388 is therefore invalid on its face: DDO would have
                    # printed 11,388 or 1,388.  First try several isolated OCR
                    # renderings that preserve punctuation.  If they still fail,
                    # accept a comma-as-1 repair only when the DDO grouping rule
                    # yields exactly one candidate.  Otherwise reject the number
                    # rather than create a false live-DPS spike.
                    if amount is not None and not amount_format_valid:
                        vr0 = time.perf_counter()
                        verify_texts = self.ocr_indra_single_variants(event_crop) if event_crop is not None else []
                        verified = []
                        for vt in verify_texts:
                            va, vtoken, vvalid = parse_outgoing_damage_details(vt, loose=True)
                            if va is not None and vvalid:
                                verified.append((va, vt, vtoken))
                        if verified:
                            verify_amount, verify_text, _ = verified[0]
                            numeric_recheck = "ddo_format_verified"
                            amount = verify_amount
                            correction_applied = "ddo_number_format_verified" if amount != raw_amount else ""
                        else:
                            inferred = comma_as_one_candidates(amount_token)
                            if len(inferred) == 1:
                                verify_amount, formatted = next(iter(inferred))
                                verify_text = f"inferred from DDO format: {formatted}"
                                numeric_recheck = "comma_as_1_inferred"
                                amount = verify_amount
                                correction_applied = "comma_as_1_ddo_format"
                            else:
                                verify_amount = None
                                verify_text = " | ".join(verify_texts)
                                numeric_recheck = "invalid_ddo_format_rejected"
                                amount = None
                        numeric_recheck_ms = f"{(time.perf_counter() - vr0) * 1000.0:.1f}"
                        numeric_recheck_text = verify_text
                        numeric_recheck_amount = verify_amount if verify_amount is not None else ""

                    # Keep the older guard for a different failure mode that can
                    # still look like a valid comma-formatted number, e.g.
                    # 9,197 -> 91,197.  Only an isolated OCR read that matches an
                    # exact 11 -> 1 collapse is allowed to change such a value.
                    elif needs_live_numeric_recheck(amount) and event_crop is not None:
                        collapse_candidates = duplicated_one_collapse_candidates(amount)
                        vr0 = time.perf_counter()
                        verify_texts = self.ocr_indra_single_variants(event_crop)
                        verify_text = verify_texts[0] if verify_texts else ""
                        verify_amount = None
                        for vt in verify_texts:
                            va, _vtoken, vvalid = parse_outgoing_damage_details(vt, loose=True)
                            if va is not None and vvalid:
                                verify_text = vt
                                verify_amount = va
                                if va in collapse_candidates:
                                    break
                        numeric_recheck_ms = f"{(time.perf_counter() - vr0) * 1000.0:.1f}"
                        numeric_recheck_text = verify_text
                        numeric_recheck_amount = verify_amount if verify_amount is not None else ""
                        if verify_amount is None:
                            numeric_recheck = "unresolved"
                        elif verify_amount == amount:
                            numeric_recheck = "confirmed"
                        elif verify_amount in collapse_candidates:
                            numeric_recheck = "changed_11_to_1"
                            amount = verify_amount
                            correction_applied = "11_to_1_verified"
                        else:
                            numeric_recheck = "disagreed_kept_batch"

                    is_attack = parse_attack_line(text) if text else False
                    if amount is not None:
                        self.record_live_damage_event(
                            event_elapsed=event_elapsed,
                            detected_elapsed=elapsed,
                            raw_amount=raw_amount if raw_amount is not None else amount,
                            final_amount=amount,
                            correction=correction_applied,
                            recheck_result=numeric_recheck,
                            verification_amount=(
                                numeric_recheck_amount
                                if numeric_recheck_amount != ""
                                else None
                            ),
                            batch_ocr_text=parse_text if parse_text else text,
                            verification_ocr_text=numeric_recheck_text,
                        )
                        if float(event_elapsed) + 1e-6 >= live_accept_after:
                            with self.events_lock:
                                self.events.append((event_elapsed, amount))
                            self.parsed_events_total += 1
                    if is_attack and accepted_for_live:
                        with self.events_lock:
                            self.attacks.append(elapsed)
                        self.parsed_attacks_total += 1

                    scan_w.writerow([
                        f"{elapsed:.6f}",
                        f"{per_line_ms:.1f}",
                        amount if amount is not None else "",
                        self.ocr_queue.qsize(),
                        self.ocr_engine_active,
                        len(items),
                        numeric_recheck,
                        numeric_recheck_amount,
                        numeric_recheck_ms,
                        numeric_recheck_text,
                        parse_text if parse_text else text,
                    ])
                    self.ocr_queue.task_done()
                    if completion_event is not None:
                        completion_event.set()
                scan_f.flush()

                if self.ocr_lines_total == len(items) or self.ocr_lines_total % 10 < len(items):
                    self.write_status(
                        detected_lines_total=self.detected_lines_total,
                        ocr_lines_total=self.ocr_lines_total,
                        parsed_events_total=self.parsed_events_total,
                        parsed_attacks_total=self.parsed_attacks_total,
                        dropped_lines_total=self.dropped_lines_total,
                        ocr_queue_depth=self.ocr_queue.qsize(),
                        last_ocr_batch_ms=batch_ms,
                        last_ocr_ms_per_line=per_line_ms,
                        last_ocr_batch_size=len(items),
                        ocr_engine=self.ocr_engine_active,
                    )

        # The worker can only reach this point after Stop has been requested and
        # the queued live lines have drained (or after an OCR error). Persist the
        # complete/partial buffered per-hit log here so it cannot race a still-
        # running OCR thread.
        try:
            self.write_live_damage_events()
        except Exception as exc:
            self.update_queue.put(("error", f"Could not write live_damage_events.csv: {exc}"))

        self.write_status(ocr_worker_finished=True, ocr_queue_depth=self.ocr_queue.qsize())

    def tracking_worker(self):
        self.write_status(
            live_process_started=True,
            tracking_worker_started=True,
            tracking_worker_finished=False,
            tracking_fps_target=self.tracking_fps,
            architecture="30fps visual tracking + asynchronous batched Indra OCR",
        )

        if not self.wait_for_clock():
            self.write_status(
                clock_available=False,
                worker_error="Recording clock was not available.",
                tracking_worker_finished=True,
            )
            self.update_queue.put(("error", "Recording clock was not available."))
            self.stop_event.set()
            return

        self.write_status(clock_available=True)
        self.stop_path.unlink(missing_ok=True)
        self.live_path.parent.mkdir(parents=True, exist_ok=True)

        ocr_thread = threading.Thread(target=self.ocr_worker, name="live-ocr", daemon=True)
        ocr_thread.start()

        with self.live_path.open("w", newline="", encoding="utf-8") as live_f, \
             self.track_path.open("w", newline="", encoding="utf-8") as track_f:

            live_w = csv.writer(live_f)
            live_w.writerow([
                "capture_elapsed_seconds",
                "wall_time_epoch",
                "live_rolling_dps",
                "window_seconds",
                "window_damage",
                "parsed_events_total",
                "detected_lines_total",
                "ocr_lines_total",
                "ocr_queue_depth",
                "dropped_lines_total",
                "live_rolling_apm",
                "window_attacks",
                "parsed_attacks_total",
            ])
            live_f.flush()

            track_w = csv.writer(track_f)
            track_w.writerow([
                "frame",
                "capture_elapsed_seconds",
                "shift_px",
                "new_lines",
                "alignment_score",
                "zero_score",
                "decision",
                "grid_phase",
                "linewise_shift_px",
                "linewise_score",
                "linewise_second_score",
                "robust_shift_px",
                "robust_score",
                "robust_second_score",
                "robust_confident",
                "robust_override",
                "ocr_queue_depth",
                "lateness_ms",
            ])
            track_f.flush()

            prev_gray = None
            prev_feat = None
            prev_phase = None
            last_phase = None

            baseline_path = self.session / "baseline_gray.png"
            if baseline_path.exists():
                try:
                    bdata = np.fromfile(str(baseline_path), dtype=np.uint8)
                    bgray = cv2.imdecode(bdata, cv2.IMREAD_GRAYSCALE)
                    if bgray is not None and bgray.shape == (
                        int(self.capture["height"]), int(self.capture["width"])
                    ):
                        prev_gray = bgray
                        prev_feat = fast_text_feature(bgray)
                        prev_phase = best_grid_phase_from_feature(prev_feat, self.line_height)
                        last_phase = prev_phase
                except Exception:
                    prev_gray = None
                    prev_feat = None
                    prev_phase = None
                    last_phase = None

            next_deadline = time.perf_counter()
            next_live_sample = next_deadline
            next_status = next_deadline

            region = {
                "left": int(self.capture["left"]),
                "top": int(self.capture["top"]),
                "width": int(self.capture["width"]),
                "height": int(self.capture["height"]),
            }

            capture_paused_for_focus = False
            with mss.MSS() as sct:
                while not self.stop_event.is_set():
                    if self.stop_path.exists():
                        break

                    now = time.perf_counter()
                    if now < next_deadline:
                        time.sleep(next_deadline - now)
                    actual = time.perf_counter()
                    lateness = max(0.0, actual - next_deadline)
                    if lateness >= self.tracking_interval:
                        self.tracking_late_frames += 1
                    self.tracking_max_lateness_ms = max(self.tracking_max_lateness_ms, lateness * 1000.0)
                    next_deadline += self.tracking_interval
                    if actual - next_deadline > self.tracking_interval * 4:
                        next_deadline = actual + self.tracking_interval

                    wall_epoch = time.time()
                    elapsed = max(0.0, wall_epoch - self.start_epoch)

                    # Screen OCR is meaningful only while DDO is foreground.
                    # When another application covers the Combat Log, pause
                    # capture entirely instead of feeding unrelated pixels to
                    # the scroll detector/OCR. Real time still advances, so old
                    # events naturally age out of the rolling window.
                    if not is_ddo_foreground():
                        if not capture_paused_for_focus:
                            capture_paused_for_focus = True
                            self.write_status(capture_paused_no_ddo_focus=True)
                        prev_gray = None
                        prev_feat = None
                        prev_phase = None
                        last_phase = None
                        self.clear_pending_bottom_line(count_drop=True)

                        now2 = time.perf_counter()
                        if now2 >= next_live_sample:
                            live_dps, window_damage, live_apm, window_attacks = self.current_metrics(elapsed)
                            live_w.writerow([
                                f"{elapsed:.6f}", f"{wall_epoch:.6f}", f"{live_dps:.3f}",
                                f"{self.window_seconds:g}", window_damage, self.parsed_events_total,
                                self.detected_lines_total, self.ocr_lines_total, self.ocr_queue.qsize(),
                                self.dropped_lines_total, f"{live_apm:.3f}", window_attacks,
                                self.parsed_attacks_total,
                            ])
                            live_f.flush()
                            self.update_queue.put((
                                "metrics", live_dps, live_apm, window_damage, window_attacks,
                                self.parsed_events_total, self.parsed_attacks_total, self.ocr_queue.qsize(),
                            ))
                            next_live_sample = now2 + 0.20
                        continue

                    if capture_paused_for_focus:
                        capture_paused_for_focus = False
                        # Treat the first visible DDO frame as a fresh visual
                        # baseline. Anything that happened while DDO was hidden
                        # cannot be reconstructed reliably from screen OCR.
                        prev_gray = None
                        prev_feat = None
                        prev_phase = None
                        last_phase = None
                        self.write_status(capture_paused_no_ddo_focus=False)

                    shot = sct.grab(region)
                    bgra = np.asarray(shot, dtype=np.uint8)
                    gray = cv2.cvtColor(bgra, cv2.COLOR_BGRA2GRAY)
                    feat = fast_text_feature(gray)
                    self.tracking_frames += 1

                    # Auto-detect chat line spacing. While unlocked, collect only
                    # high-confidence measurements. Once three agree, lock it for
                    # this recording. Reset the visual baseline when the value
                    # changes so the transition itself cannot look like a scroll.
                    if self.auto_line_height and not self.line_height_locked:
                        detected_h, lh_score, lh_margin, lh_ok = detect_line_height_from_gray(
                            gray, self.line_height,
                            int(self.analysis_cfg.get("line_height_min", 10)),
                            int(self.analysis_cfg.get("line_height_max", 22)),
                        )
                        if lh_ok:
                            self.line_height_candidates.append(detected_h)
                            self.line_height_candidates = self.line_height_candidates[-5:]
                            if self.line_height_candidates.count(detected_h) >= 3:
                                if detected_h != self.line_height:
                                    self.line_height = detected_h
                                    prev_gray = None
                                    prev_feat = None
                                    prev_phase = None
                                    last_phase = None
                                    self.clear_pending_bottom_line(count_drop=True)
                                self.line_height_locked = True

                    # Determine the row-grid phase from the CURRENT frame.
                    # Do not freeze the phase inherited from baseline_gray.png:
                    # an almost-empty Combat Log has no meaningful text grid and
                    # can legitimately report a different phase (for example 0)
                    # from the populated log (for example 5). Freezing that stale
                    # phase slices every later 14 px OCR row at the wrong vertical
                    # offset even though scroll detection itself remains correct.
                    # This calculation is cheap (~14 row-energy means) and is
                    # therefore safe on the 30-fps live path.
                    phase_now = best_grid_phase_from_feature(feat, self.line_height)
                    phase = phase_now
                    last_phase = phase

                    robust_shift = 0
                    robust_score = 0.0
                    robust_second = 0.0
                    robust_confident = False
                    robust_override = False
                    if prev_gray is None:
                        shift = 0
                        score = 1.0
                        zero = 1.0
                        decision = "baseline"
                        lw_shift = 0
                        lw_score = 1.0
                        lw_second = 0.0
                        crops = []
                    else:
                        shift, score, zero, decision = estimate_shift_fast(
                            prev_feat,
                            feat,
                            self.line_height,
                            self.max_shift_px,
                        )
                        lw_shift = 0
                        lw_score = 0.0
                        lw_second = 0.0

                        # Second opinion only for ambiguous live frames.  Keep
                        # the cheap matcher on the 30-fps fast path, and let the
                        # robust post-analysis matcher override only when it is
                        # itself confidently positive.
                        if decision == "uncertain->zero":
                            self.robust_fallback_runs += 1
                            (
                                robust_shift, robust_score, _robust_zero,
                                robust_decision, robust_confident,
                                _robust_margin, robust_second,
                            ) = estimate_shift_robust_live(
                                prev_gray,
                                gray,
                                self.line_height,
                                self.max_shift_px,
                                self.analysis_cfg,
                            )
                            if robust_confident and robust_decision == "positive" and robust_shift > 0:
                                shift = int(robust_shift)
                                decision += "+robust-positive"
                                robust_override = True
                                self.robust_fallback_overrides += 1
                            else:
                                decision += "+robust-keep-zero"

                        crop_entries = []  # (elapsed, crop)
                        recovered = self.recover_pending_bottom_line(gray, feat, shift)
                        if recovered is not None:
                            crop_entries.append(recovered)
                            decision += "+recover-bottom"

                        crops, deferred_y0 = crop_new_lines(
                            gray, feat, shift, phase, self.line_height
                        )
                        crop_entries.extend((elapsed, crop) for crop in crops)
                        if deferred_y0 is not None:
                            if self.defer_pending_bottom_line(gray, deferred_y0, elapsed):
                                decision += "+defer-bottom"

                        if not crop_entries and shift == 0 and prev_phase == phase:
                            append_crops = crop_blank_to_filled_lines(
                                prev_feat, gray, feat, phase, self.line_height
                            )
                            if append_crops:
                                crop_entries.extend((elapsed, crop) for crop in append_crops)
                                decision += "+append"

                        for crop_elapsed, crop in crop_entries:
                            self.enqueue_new_lines([crop], crop_elapsed)
                        crops = [crop for _crop_elapsed, crop in crop_entries]

                    track_w.writerow([
                        self.tracking_frames,
                        f"{elapsed:.6f}",
                        shift,
                        len(crops),
                        f"{score:.5f}",
                        f"{zero:.5f}",
                        decision,
                        phase,
                        lw_shift,
                        f"{lw_score:.5f}",
                        f"{lw_second:.5f}",
                        robust_shift,
                        f"{robust_score:.5f}",
                        f"{robust_second:.5f}",
                        robust_confident,
                        robust_override,
                        self.ocr_queue.qsize(),
                        f"{lateness * 1000.0:.2f}",
                    ])
                    if self.tracking_frames % 30 == 0:
                        track_f.flush()

                    prev_gray = gray
                    prev_feat = feat
                    prev_phase = phase

                    now2 = time.perf_counter()
                    if now2 >= next_live_sample:
                        live_dps, window_damage, live_apm, window_attacks = self.current_metrics(elapsed)
                        live_w.writerow([
                            f"{elapsed:.6f}",
                            f"{wall_epoch:.6f}",
                            f"{live_dps:.3f}",
                            f"{self.window_seconds:g}",
                            window_damage,
                            self.parsed_events_total,
                            self.detected_lines_total,
                            self.ocr_lines_total,
                            self.ocr_queue.qsize(),
                            self.dropped_lines_total,
                            f"{live_apm:.3f}",
                            window_attacks,
                            self.parsed_attacks_total,
                        ])
                        live_f.flush()
                        self.update_queue.put((
                            "metrics",
                            live_dps,
                            live_apm,
                            window_damage,
                            window_attacks,
                            self.parsed_events_total,
                            self.parsed_attacks_total,
                            self.ocr_queue.qsize(),
                        ))
                        next_live_sample = now2 + 0.20

                    if now2 >= next_status:
                        actual_fps = self.tracking_frames / max(elapsed, 0.001)
                        self.write_status(
                            tracking_frames=self.tracking_frames,
                            tracking_fps_actual=actual_fps,
                            tracking_late_frames=self.tracking_late_frames,
                            tracking_max_lateness_ms=self.tracking_max_lateness_ms,
                            detected_lines_total=self.detected_lines_total,
                            ocr_lines_total=self.ocr_lines_total,
                            parsed_events_total=self.parsed_events_total,
                            dropped_lines_total=self.dropped_lines_total,
                            ocr_queue_depth=self.ocr_queue.qsize(),
                            bottom_deferred_total=self.bottom_deferred_total,
                            bottom_recovered_total=self.bottom_recovered_total,
                            bottom_stop_flushed_total=self.bottom_stop_flushed_total,
                            bottom_dropped_total=self.bottom_dropped_total,
                            robust_fallback_runs=self.robust_fallback_runs,
                            robust_fallback_overrides=self.robust_fallback_overrides,
                        )
                        next_status = now2 + 1.0

        # Stop/EOF can leave the newest row only partially visible.  Give that
        # one pending fragment a final OCR chance before stopping the worker.
        self.flush_pending_bottom_line()

        # Stop capture first, then let OCR drain the lines that have already been detected.
        # The OCR worker itself writes live_damage_events.csv after its queue is empty.
        self.stop_event.set()
        ocr_thread.join(timeout=8.0)
        self.write_status(
            tracking_worker_finished=True,
            tracking_frames=self.tracking_frames,
            tracking_late_frames=self.tracking_late_frames,
            tracking_max_lateness_ms=self.tracking_max_lateness_ms,
            detected_lines_total=self.detected_lines_total,
            ocr_lines_total=self.ocr_lines_total,
            parsed_events_total=self.parsed_events_total,
            parsed_attacks_total=self.parsed_attacks_total,
            dropped_lines_total=self.dropped_lines_total,
            final_ocr_queue_depth=self.ocr_queue.qsize(),
        )
        self.update_queue.put(("done",))

    def setup_overlay(self):
        if not self.overlay_enabled:
            return

        self.root = tk.Tk()
        self.root.title("DDO Combat Analyzer - Live DPS")
        self.root.overrideredirect(True)
        transparent = "#010203"
        self.root.configure(bg=transparent)
        self.root.attributes("-topmost", True)

        # On Windows use a chroma-key background so there is no black rectangle:
        # only the red DPS text remains visible. If unsupported, fall back to a
        # lightly translucent dark window.
        transparent_ok = False
        if os.name == "nt":
            try:
                self.root.wm_attributes("-transparentcolor", transparent)
                transparent_ok = True
            except Exception:
                transparent_ok = False
        if not transparent_ok:
            self.root.configure(bg="#101214")
            try:
                self.root.attributes("-alpha", 0.90)
            except Exception:
                pass

        width = self.overlay_width
        height = self.overlay_height
        with mss.MSS() as sct:
            mon = choose_monitor(sct.monitors, self.capture)
        position = str(self.live_cfg.get("overlay_position", "screen_center_test"))
        if position == "screen_center_test":
            x = int(mon["left"] + (mon["width"] - width) / 2)
            y = int(mon["top"] + (mon["height"] - height) / 2)
        else:
            x, y = choose_overlay_position(mon, self.capture, width, height, self.overlay_margin)
        self.root.geometry(f"{width}x{height}{x:+d}{y:+d}")

        canvas_bg = transparent if transparent_ok else "#101214"
        self.overlay_canvas = tk.Canvas(
            self.root, width=width, height=height, bg=canvas_bg,
            highlightthickness=0, bd=0, relief="flat"
        )
        self.overlay_canvas.pack(fill="both", expand=True)
        self.value_item = self.overlay_canvas.create_text(
            width // 2, 42,
            text="0 DPS", fill="#ff3030", font=("Segoe UI", 34, "bold"),
            anchor="center"
        )
        self.apm_item = self.overlay_canvas.create_text(
            width // 2, 91,
            text="0 APM", fill="#ff5050", font=("Segoe UI", 22, "bold"),
            anchor="center"
        )
        self.detail_item = self.overlay_canvas.create_text(
            width // 2, 127,
            text=f"LIVE {self.window_seconds:g}s  |  HEJSA", fill="#ff9090", font=("Segoe UI", 10, "bold"),
            anchor="center"
        )

        self.root.update_idletasks()
        make_clickthrough(self.root)
        if os.name == "nt":
            try:
                hwnd = self.root.winfo_id()
                HWND_TOPMOST = -1
                SWP_NOSIZE = 0x0001
                SWP_NOMOVE = 0x0002
                SWP_NOACTIVATE = 0x0010
                SWP_SHOWWINDOW = 0x0040
                ctypes.windll.user32.SetWindowPos(
                    hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                    SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE | SWP_SHOWWINDOW,
                )
            except Exception:
                pass
        self.write_status(overlay_transparent_background=transparent_ok, overlay_canvas=True)

    def poll_updates(self):
        try:
            while True:
                msg = self.update_queue.get_nowait()
                kind = msg[0]
                if kind == "metrics" and self.root is not None and self.overlay_canvas is not None:
                    _, dps, apm, window_damage, window_attacks, total_events, total_attacks, qdepth = msg
                    self.overlay_canvas.itemconfigure(
                        self.value_item, text=f"{int(round(dps)):,} DPS"
                    )
                    self.overlay_canvas.itemconfigure(
                        self.apm_item, text=f"{int(round(apm))} APM"
                    )
                    self.overlay_canvas.itemconfigure(
                        self.detail_item,
                        text=f"LIVE {self.window_seconds:g}s  |  HEJSA  |  queue {qdepth}"
                    )
                elif kind == "done":
                    if self.root is not None:
                        self.root.destroy()
                    return
                elif kind == "error":
                    if self.root is not None and self.overlay_canvas is not None:
                        self.overlay_canvas.itemconfigure(self.value_item, text="NO LIVE DPS")
        except queue.Empty:
            pass
        if self.root is not None:
            try:
                if is_ddo_foreground():
                    if str(self.root.state()) == "withdrawn":
                        self.root.deiconify()
                        self.root.attributes("-topmost", True)
                else:
                    if str(self.root.state()) != "withdrawn":
                        self.root.withdraw()
            except Exception:
                pass
            self.root.after(80, self.poll_updates)

    def run(self):
        worker = threading.Thread(target=self.tracking_worker, name="live-tracker", daemon=True)
        worker.start()
        self.write_status(overlay_requested=self.overlay_enabled, overlay_created=False)

        if self.overlay_enabled:
            try:
                self.setup_overlay()
                self.write_status(overlay_created=self.root is not None)
            except Exception as exc:
                try:
                    self.overlay_error_path.write_text(f"{type(exc).__name__}: {exc}\n", encoding="utf-8")
                except Exception:
                    pass
                self.write_status(overlay_created=False, overlay_error=f"{type(exc).__name__}: {exc}")
                try:
                    if self.root is not None:
                        self.root.destroy()
                except Exception:
                    pass
                self.root = None

        if self.root is not None:
            self.root.after(80, self.poll_updates)
            self.root.mainloop()
        else:
            while worker.is_alive():
                if self.stop_path.exists():
                    self.stop_event.set()
                worker.join(timeout=0.2)

        self.stop_event.set()
        worker.join(timeout=10.0)
        self.write_status(process_finished=True, overlay_visible_runtime=self.root is not None)



def watch_parent_process(estimator, parent_pid):
    """Set the helper stop event when its creating GUI exits."""
    if os.name != "nt" or not parent_pid:
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
        handle = kernel32.OpenProcess(SYNCHRONIZE, False, int(parent_pid))
        if not handle:
            estimator.stop_event.set()
            return
        kernel32.WaitForSingleObject(handle, INFINITE)
        estimator.stop_event.set()
    except Exception:
        pass
    finally:
        if handle:
            try:
                kernel32.CloseHandle(handle)
            except Exception:
                pass

def main():
    set_low_priority()
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", required=True)
    ap.add_argument("--no-overlay", action="store_true")
    ap.add_argument("--parent-pid", type=int, default=None)
    args = ap.parse_args()
    estimator = LiveEstimator(Path(args.session), no_overlay=args.no_overlay)
    if args.parent_pid:
        threading.Thread(
            target=watch_parent_process, args=(estimator, args.parent_pid),
            name="parent-watchdog", daemon=True
        ).start()
    estimator.run()


if __name__ == "__main__":
    main()
