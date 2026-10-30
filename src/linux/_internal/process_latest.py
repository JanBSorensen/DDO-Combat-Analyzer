from __future__ import annotations

import csv
import json
import math
import os
import re
import shutil
import subprocess
import time
from collections import defaultdict
from pathlib import Path

import cv2
import numpy as np
try:
    import indra_ocr
except Exception:
    indra_ocr = None

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"

from cpu_utils import physical_core_count, resolve_indra_threads


# ---------------------------------------------------------------------------
# Session / executable discovery
# ---------------------------------------------------------------------------

def locate_session():
    pointer = HERE / "LATEST_SESSION.txt"
    if pointer.exists():
        try:
            p = Path(pointer.read_text(encoding="utf-8").strip())
            if p.exists() and (p / "combat_changed_gray_ffv1.mkv").exists():
                return p
        except Exception:
            pass

    sessions = [
        p for p in HERE.glob("combat_*")
        if p.is_dir() and (p / "combat_changed_gray_ffv1.mkv").exists()
    ]
    return max(sessions, key=lambda p: p.stat().st_mtime) if sessions else None



def find_ffmpeg(configured):
    candidates = []
    if configured:
        candidates.append(Path(configured))
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


# ---------------------------------------------------------------------------
# Image / scroll reconstruction
# ---------------------------------------------------------------------------

def normalize_text(s):
    s = s.replace("\r", " ").replace("\n", " ")
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"^[\|\[\]{}Il1:;`'.,_-]+\s*(?=\(Combat\))", "", s)
    return s


def text_feature(gray, threshold=18):
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (11, 7))
    top_hat = cv2.morphologyEx(gray, cv2.MORPH_TOPHAT, kernel)
    blur = cv2.GaussianBlur(gray, (0, 0), 2.0)
    high = cv2.absdiff(gray, blur)
    feat = cv2.max(top_hat, high)
    _, feat = cv2.threshold(feat, threshold, 255, cv2.THRESH_TOZERO)
    b = 6
    feat[:b, :] = 0
    feat[-b:, :] = 0
    feat[:, :b] = 0
    feat[:, -b:] = 0
    return feat.astype(np.float32) / 255.0




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

def cosine(a, b):
    aa = float(np.sum(a * a))
    bb = float(np.sum(b * b))
    if aa < 1e-9 or bb < 1e-9:
        return 0.0
    return float(np.sum(a * b) / math.sqrt(aa * bb))


def alignment_score(prev_feat, curr_feat, shift):
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
    return cosine(a, b)


def alignment_scores_fast(prev_feat, curr_feat, shifts):
    """Score several vertical shifts without changing the alignment model.

    The previous implementation recomputed both overlap norms and created three
    large temporary multiplication arrays for every candidate shift.  Here the
    horizontal crop is made contiguous once, row squared-norms are accumulated
    once, and each candidate only needs one BLAS-backed dot product.  The score
    is the same cosine similarity over exactly the same overlap pixels.
    """
    h, w = prev_feat.shape[:2]
    mx = max(5, int(w * 0.025))
    if w > 2 * mx + 20:
        prev = np.ascontiguousarray(prev_feat[:, mx:-mx])
        curr = np.ascontiguousarray(curr_feat[:, mx:-mx])
    else:
        prev = np.ascontiguousarray(prev_feat)
        curr = np.ascontiguousarray(curr_feat)

    # Cache squared norms by row.  Float64 prefix accumulation keeps the
    # denominator numerically stable while avoiding repeated full-overlap sums.
    prev_row_sq = np.einsum(
        "ij,ij->i", prev, prev, optimize=True
    ).astype(np.float64, copy=False)
    curr_row_sq = np.einsum(
        "ij,ij->i", curr, curr, optimize=True
    ).astype(np.float64, copy=False)

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
            a = prev[shift:]
            b = curr[:overlap_h]
            dot = float(np.dot(a.reshape(-1), b.reshape(-1)))
            score = dot / math.sqrt(aa * bb)
        scored.append((shift, float(score)))

    return scored


def estimate_shift(prev_feat, curr_feat, cfg):
    """Whole-frame scroll estimate plus a conservative confidence signal.

    A zero-shift fast path first tests only shift=0.  When consecutive frames
    are virtually identical, this avoids evaluating every possible positive
    scroll shift.  If the zero score is below the deliberately strict
    threshold, the original exhaustive whole-frame search runs unchanged.

    The confidence signal is used only to decide whether the expensive
    line-by-line verifier can be skipped.  If confidence is low, the original
    linewise fallback still runs and can override this estimate exactly as
    before.
    """
    acfg = cfg["analysis"]
    line_height = int(acfg.get("line_height", 16))
    max_shift = min(
        int(acfg.get("max_scroll_pixels", 384)),
        prev_feat.shape[0] - 48,
    )
    min_score = float(acfg.get("min_alignment_score", 0.80))
    min_improvement = float(acfg.get("min_improvement_over_zero", 0.035))
    close_tol = float(acfg.get("close_score_tolerance", 0.008))

    # Fast path: on forced anchors / unchanged frames, shift=0 is often an
    # exceptionally strong match.  A strict threshold makes it safe to accept
    # immediately; otherwise we fall through to the exact previous search.
    zero = alignment_score(prev_feat, curr_feat, 0)
    zero_first_enabled = bool(acfg.get("zero_first_enabled", True))
    zero_first_threshold = float(acfg.get("zero_first_threshold", 0.995))
    if zero_first_enabled and zero >= zero_first_threshold:
        return (
            0,
            zero,
            zero,
            "zero-fast",
            True,
            None,
            None,
            True,
        )

    candidates = list(range(0, max_shift + 1, line_height))
    # Compute all candidate overlaps in one optimized pass.  Preserve the exact
    # zero score from the original fast-path probe so zero-threshold behaviour
    # remains byte-for-byte equivalent at that decision point.
    scores = alignment_scores_fast(prev_feat, curr_feat, candidates)
    scores = [
        (s, zero if s == 0 else sc)
        for s, sc in scores
    ]

    ranked = sorted(scores, key=lambda x: x[1], reverse=True)
    best_shift, best_score = ranked[0]
    second_score = ranked[1][1] if len(ranked) > 1 else 0.0
    raw_margin = float(best_score - second_score)

    if best_shift == 0:
        shift, chosen_score, decision = 0, best_score, "zero"
    elif best_score < min_score or (best_score - zero) < min_improvement:
        shift, chosen_score, decision = 0, best_score, "uncertain->zero"
    else:
        close = [
            (s, sc) for s, sc in scores
            if s > 0 and sc >= best_score - close_tol and sc >= min_score
        ]
        if close:
            shift, chosen_score = min(close, key=lambda x: x[0])
        else:
            shift, chosen_score = best_shift, best_score
        decision = "positive"

    confident_score = float(acfg.get("whole_frame_confident_score", 0.97))
    confident_margin = float(acfg.get("whole_frame_confident_margin", 0.04))
    confident_positive_improvement = float(
        acfg.get("whole_frame_confident_positive_improvement", 0.08)
    )
    whole_confident = bool(
        decision in ("zero", "positive")
        and chosen_score >= confident_score
        and raw_margin >= confident_margin
        and (
            shift == 0
            or (chosen_score - zero) >= confident_positive_improvement
        )
    )

    return (
        shift,
        chosen_score,
        zero,
        decision,
        whole_confident,
        raw_margin,
        second_score,
        False,
    )


def grid_phase_score_from_energy(energy, line_height, phase):
    """Return the boundary-energy score for one text-grid phase.

    ``energy`` is the already-computed row energy from ``text_feature``.  The
    previous implementation recomputed the identical full-frame feature image
    once for every candidate phase (14 times at the current 14 px line height).
    Reusing the same energy array is mathematically identical and only removes
    redundant image processing.
    """
    h = len(energy)
    ys = list(range(int(phase), h, int(line_height)))
    if len(ys) < 4:
        return float("inf")
    vals = []
    for y in ys:
        lo = max(0, y - 1)
        hi = min(h, y + 2)
        vals.append(float(np.mean(energy[lo:hi])))
    return float(np.mean(vals)) if vals else float("inf")


def grid_phase_score(gray, line_height, phase, feat=None, energy=None):
    """Return the boundary-energy score for one text-grid phase. Lower is better."""
    if energy is None:
        if feat is None:
            feat = text_feature(gray)
        energy = feat.mean(axis=1)
    return grid_phase_score_from_energy(energy, line_height, phase)


def best_grid_phase_with_score(gray, line_height, feat=None):
    """Return (phase, best_score, second_score). Lower scores are better.

    DDO's visible text grid can change phase when a partially filled Combat Log
    becomes full and starts scrolling. Keeping the scores lets reconstruction
    distinguish a real, strongly supported re-lock from a noisy one-frame
    phase estimate.  The feature/energy image is computed exactly once per
    frame, so phase choice is unchanged while avoiding redundant work.
    """
    if feat is None:
        feat = text_feature(gray)
    energy = feat.mean(axis=1)
    scored = [
        (grid_phase_score_from_energy(energy, line_height, phase), phase)
        for phase in range(int(line_height))
    ]
    scored.sort(key=lambda x: x[0])
    if not scored:
        return 0, float("inf"), float("inf")
    best_score, best_phase = scored[0]
    second_score = scored[1][0] if len(scored) > 1 else float("inf")
    return int(best_phase), float(best_score), float(second_score)


def best_grid_phase(gray, line_height, feat=None):
    return best_grid_phase_with_score(gray, line_height, feat=feat)[0]


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



def line_match_feature(crop):
    """Feature used for conservative line-by-line scroll verification."""
    blur = cv2.GaussianBlur(crop, (0, 0), 1.4)
    high = cv2.absdiff(crop, blur)
    if high.shape[1] > 40:
        high = high[:, 15:-15]
    _, high = cv2.threshold(high, 10, 255, cv2.THRESH_TOZERO)
    return high.astype(np.float32) / 255.0


def estimate_shift_linewise(prev_gray, curr_gray, prev_phase, curr_phase, line_height, max_shift_px):
    """
    Independently estimate scroll by matching visual text lines in order.
    Returns (shift_px, best_score, second_score, confident).

    This is deliberately conservative. It only overrides the whole-frame
    estimator when a multi-line match is extremely strong.
    """
    prev_cells = line_cells(prev_gray, prev_phase, line_height)
    curr_cells = line_cells(curr_gray, curr_phase, line_height)
    if len(prev_cells) < 6 or len(curr_cells) < 6:
        return 0, 0.0, 0.0, False

    prev_feats = [
        line_match_feature(prev_gray[y0:y1])
        for y0, y1 in prev_cells
    ]
    curr_feats = [
        line_match_feature(curr_gray[y0:y1])
        for y0, y1 in curr_cells
    ]

    max_lines = min(
        int(max_shift_px // line_height),
        len(prev_feats) - 4,
    )
    candidates = []

    for shift_lines in range(max_lines + 1):
        overlap = min(len(prev_feats) - shift_lines, len(curr_feats))
        if overlap < 4:
            continue

        sims = []
        for j in range(overlap):
            sims.append(cosine(prev_feats[j + shift_lines], curr_feats[j]))

        score = float(np.mean(sims)) if sims else 0.0
        strong = sum(1 for x in sims if x >= 0.94)
        candidates.append((score, strong, overlap, shift_lines))

    if not candidates:
        return 0, 0.0, 0.0, False

    candidates.sort(key=lambda x: (x[0], x[1], x[2]), reverse=True)
    best = candidates[0]
    second_score = candidates[1][0] if len(candidates) > 1 else 0.0

    best_score, strong, overlap, shift_lines = best
    required_strong = max(4, int(round(overlap * 0.55)))
    confident = (
        best_score >= 0.94
        and strong >= required_strong
        and (best_score - second_score) >= 0.015
    )

    return shift_lines * line_height, best_score, second_score, confident


def bottom_partial_line_info(gray, phase, line_height, feat=None):
    """Return the visible bottom-row fragment when it is text-bearing.

    A DDO text row can straddle the lower edge of the selected capture area.
    The previous reconstruction fix sent a >=70%-visible fragment directly to
    OCR after padding it.  On the reference session those 12/14-pixel crops
    caused a systematic g->q OCR error (for example damage->damaqe).

    We still detect the same fragment, but callers now defer it until a later
    scrolling frame where that exact row has moved fully inside the capture.
    """
    cells = line_cells(gray, phase, line_height)
    if cells:
        partial_y0 = cells[-1][1]
    else:
        partial_y0 = int(phase)

    h = gray.shape[0]
    partial_h = h - partial_y0
    min_partial_h = max(8, int(math.ceil(float(line_height) * 0.70)))
    if partial_h < min_partial_h or partial_h >= int(line_height):
        return None

    if feat is None:
        feat = text_feature(gray)
    level = float(np.mean(feat[partial_y0:h]))
    if level < 0.006:
        return None
    return int(partial_y0), int(h), level


def crop_new_lines(gray, shift, phase, line_height, feat=None):
    """Extract only complete newly visible rows.

    Returns (full_crops, deferred_partial_y0).  If one of the n newly scrolled
    rows is the text-bearing partial row at the bottom edge, that row reserves
    one of the n slots but is not OCR'd yet.  This prevents us from replacing
    it with an older full row (which would create a duplicate).  The caller
    recovers the deferred row from a later frame after it has scrolled fully
    into view.
    """
    n = int(round(shift / line_height))
    if n <= 0:
        return [], None

    if feat is None:
        feat = text_feature(gray)
    cells = line_cells(gray, phase, line_height)

    partial = bottom_partial_line_info(gray, phase, line_height, feat)
    deferred_partial_y0 = partial[0] if partial is not None else None
    full_rows_needed = max(0, n - (1 if partial is not None else 0))
    if full_rows_needed <= 0:
        return [], deferred_partial_y0

    scored = []
    for y0, y1 in cells:
        sc = float(np.mean(feat[y0:y1]))
        scored.append((sc, y0, y1))

    selected = []
    for sc, y0, y1 in reversed(scored):
        if sc < 0.006:
            continue
        selected.append((y0, y1))
        if len(selected) >= full_rows_needed:
            break
    selected.reverse()
    return [gray[y0:y1].copy() for y0, y1 in selected], deferred_partial_y0


def crop_blank_to_filled_lines(prev_feat, curr_gray, curr_feat, phase, line_height):
    """Catch new rows while a not-yet-full Combat Log appends without scrolling.

    This is the post-analysis counterpart of the validated live-tracker path.
    Features here are normalized to 0..1, so thresholds are expressed directly
    in that scale.
    """
    crops = []
    for y0, y1 in line_cells(curr_gray, phase, line_height):
        prev_level = float(np.mean(prev_feat[y0:y1]))
        curr_level = float(np.mean(curr_feat[y0:y1]))
        if prev_level < 0.0020 and curr_level >= 0.0030:
            crops.append(curr_gray[y0:y1].copy())
    return crops


def preprocess_line(line_gray):
    line = cv2.copyMakeBorder(
        line_gray, 4, 4, 3, 3, cv2.BORDER_REPLICATE
    )
    line = cv2.resize(
        line, None, fx=4.0, fy=4.0, interpolation=cv2.INTER_CUBIC
    )
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (21, 13))
    top = cv2.morphologyEx(line, cv2.MORPH_TOPHAT, kernel)
    top = cv2.GaussianBlur(top, (3, 3), 0)
    _, bw = cv2.threshold(
        top, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU
    )
    return 255 - bw



def preprocess_line_indra(line_gray):
    """Same light DDO preprocessing used by the validated live Indra OCR path."""
    line = cv2.copyMakeBorder(line_gray, 3, 3, 3, 3, cv2.BORDER_REPLICATE)
    line = cv2.resize(line, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (13, 9))
    top = cv2.morphologyEx(line, cv2.MORPH_TOPHAT, kernel)
    top = cv2.GaussianBlur(top, (3, 3), 0)
    _, bw = cv2.threshold(top, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return 255 - bw


def build_indra_batch(lines, gap=14):
    prepared = [preprocess_line_indra(crop) for crop in lines]
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


def ocr_indra_batch(converter, lines):
    canvas, slots = build_indra_batch(lines)
    if canvas is None:
        return [""] * len(lines)
    page = converter.ocr(canvas)
    by_slot = [[] for _ in lines]
    for detected in getattr(page, "lines", []) or []:
        text = normalize_text(getattr(detected, "text", "") or "")
        if not text:
            continue
        box = getattr(detected, "box", None)
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


def _single_q_g_variant(a, b):
    """True only for one same-position q<->g OCR substitution."""
    aa = str(a or "")
    bb = str(b or "")
    if len(aa) != len(bb) or aa.lower() == bb.lower():
        return False
    diffs = [(x.lower(), y.lower()) for x, y in zip(aa, bb) if x.lower() != y.lower()]
    return len(diffs) == 1 and set(diffs[0]) == {"q", "g"}


def normalize_eof_partial_damage_row(text, prior_texts):
    """Conservatively repair the one unavoidable clipped row at end-of-file.

    Normal reconstruction never OCRs clipped rows.  At EOF there may be one
    pending bottom row that can never scroll fully into view.  On the validated
    DDO font a 12/14 px crop can turn g into q, notably damage->damaqe.  Repair
    only that exact keyword here.  If its target differs from a previously seen
    target by exactly one q<->g substitution, reuse the unique prior spelling.
    No general q->g replacement is performed.
    """
    raw = str(text or "")
    fixed = re.sub(r"\bdamaqe\b", "damage", raw, flags=re.IGNORECASE)

    m = re.search(r"\bYou\s+hit\s+(?P<target>.+?)\s+for\s+", fixed, re.IGNORECASE)
    if not m:
        return fixed
    raw_target = canonical_target(m.group("target"))
    if not raw_target:
        return fixed

    candidates = set()
    for prior in prior_texts or []:
        pm = re.search(r"\bYou\s+hit\s+(?P<target>.+?)\s+for\s+", str(prior or ""), re.IGNORECASE)
        if pm:
            candidate = canonical_target(pm.group("target"))
            if candidate:
                candidates.add(candidate)
    matches = [c for c in candidates if _single_q_g_variant(raw_target, c)]
    if len(matches) != 1:
        return fixed

    start, end = m.span("target")
    return fixed[:start] + matches[0] + fixed[end:]


# ---------------------------------------------------------------------------
# OCR-aware numeric parsing
# ---------------------------------------------------------------------------

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


def parse_ocr_number(token):
    """
    Convert a number token only in a numeric field.

    Safe contextual corrections include:
      S22 -> 522
      S06 -> 506
      43S -> 435
      1,O91 -> 1,091
      l,404 -> 1,404

    The original OCR text is never changed; corrections are logged.
    """
    raw = token.strip()
    corrected = raw.translate(OCR_DIGIT_MAP)
    corrected = corrected.replace(",", "")
    if not corrected.isdigit():
        return None, raw, raw, False
    value = int(corrected)
    changed = (raw != raw.translate(OCR_DIGIT_MAP))
    return value, raw, raw.translate(OCR_DIGIT_MAP), changed


def clean_damage_type(raw):
    t = raw.strip().lower()
    t = re.sub(r"\s+", " ", t)
    aliases = {
        "piercing": "pierce",
        "slashing": "slash",
        "bludgeoning": "bludgeon",
        "electricity": "electric",
    }
    return aliases.get(t, t)


# ---------------------------------------------------------------------------
# Combat patterns
# ---------------------------------------------------------------------------

OUT_RE = re.compile(
    rf"\bYou\s+(?P<kind>sneak\s+attack|hit)\s+"
    rf"(?P<target>.+?)\s+for\s+"
    rf"(?P<amount>{NUM_TOKEN})\s+points?\s+of\s+"
    rf"(?P<dtype>[A-Za-z][A-Za-z -]*?)\s+damage\b",
    re.IGNORECASE,
)

OUT_UNTYPED_RE = re.compile(
    rf"\bYou\s+(?P<kind>sneak\s+attack|hit)\s+"
    rf"(?P<target>.+?)\s+for\s+"
    rf"(?P<amount>{NUM_TOKEN})\s+points?\s+of\s+damage\b",
    re.IGNORECASE,
)

BLOCKED_AFTER_RE = re.compile(
    rf"\bdamage\s+after\s+(?P<blocked>{NUM_TOKEN})\b.*?\bblocked\b",
    re.IGNORECASE,
)
BLOCKED_REASON_RE = re.compile(
    r"\bblocked\s+by\s+(?P<reason>.+?)(?:[.!]|$)",
    re.IGNORECASE,
)
ZERO_BLOCKED_OUT_RE = re.compile(
    rf"\bYou\s+(?P<kind>sneak\s+attack|hit)\s+(?P<target>.+?)\s+"
    rf"but\s+did\s+no\s+damage;\s*(?P<blocked>{NUM_TOKEN})\s+points?\s+of\s+damage\s+"
    rf"(?:were|was)\s+blocked(?:\s+by\s+(?P<reason>.+?))?(?:[.!]|$)",
    re.IGNORECASE,
)


def pretty_damage_type(dtype):
    t = clean_damage_type(dtype or "")
    names = {
        "fire": "Fire",
        "cold": "Cold",
        "electric": "Electric",
        "acid": "Acid",
        "sonic": "Sonic",
        "light": "Light",
        "negative": "Negative",
        "positive": "Positive",
        "force": "Force",
        "pierce": "Piercing",
        "slash": "Slashing",
        "bludgeon": "Bludgeoning",
        "untyped": "Untyped",
        "absorbed": "Unknown",
    }
    return names.get(t, t.title() if t else "Unknown")


def normalize_reduction_type(reason, damage_type=""):
    """Return a canonical reduction label and broad class.

    DDO/OCR can produce several surface forms for the same DR, e.g. "Silver",
    "Silver damage", "Silver DR" and "Silver damage reduction".  Normalize
    these before aggregation so one mob/type appears on one row.
    """
    raw = re.sub(r"\s+", " ", (reason or "").strip().strip(" .,:;"))
    low = raw.lower()
    if not low:
        return "", ""

    if "energy resistance" in low or low in ("energy resist", "energy resistance"):
        return f"{pretty_damage_type(damage_type)} resistance", "Energy resistance"

    aliases = {
        "slash": "Slash", "slashing": "Slash",
        "pierce": "Pierce", "piercing": "Pierce",
        "bludgeon": "Bludgeon", "bludgeoning": "Bludgeon",
        "cold iron": "Cold Iron", "silver": "Silver",
        "good": "Good", "evil": "Evil", "lawful": "Lawful",
        "chaotic": "Chaotic", "adamantine": "Adamantine",
        "byeshk": "Byeshk", "crystal": "Crystal", "magic": "Magic",
    }

    # Strip the common full and OCR-truncated suffixes before alias lookup.
    core = low
    for suffix in (" damage reduction", " reduction", " damage", " dr"):
        if core.endswith(suffix):
            core = core[:-len(suffix)].strip()
            break
    if core in aliases:
        return f"DR / {aliases[core]}", "Damage reduction"

    # Preserve unfamiliar explicit DR wording but still present it consistently.
    if "damage reduction" in low:
        prefix = low.split("damage reduction", 1)[0].strip()
        label = aliases.get(prefix, prefix.title())
        return (f"DR / {label}" if label else "Damage reduction"), "Damage reduction"

    # If OCR retained only a known bypass word, it is still safe to classify.
    if low in aliases:
        return f"DR / {aliases[low]}", "Damage reduction"

    return raw[0].upper() + raw[1:] if raw else "", "Other"


def parse_reduction_type(line, damage_type=""):
    m = BLOCKED_REASON_RE.search(line)
    if not m:
        return "", ""
    return normalize_reduction_type(m.group("reason"), damage_type)


# Narrow two-row damage-wrap patterns.  These are intentionally much stricter
# than the generic Combat-message reassembly: they only join a damage row that
# ends immediately after "points of <type>" (or "points of") with the very
# next visual OCR row that supplies "damage.".  This preserves the validated
# one-row damage parser while recovering long wrapped sneak-attack lines.
OUT_WRAP_TYPED_PREFIX_RE = re.compile(
    rf"\bYou\s+(?P<kind>sneak\s+attack|hit)\s+"
    rf"(?P<target>.+?)\s+for\s+"
    rf"(?P<amount>{NUM_TOKEN})\s+points?\s+of\s+"
    rf"(?P<dtype>[A-Za-z][A-Za-z -]*?)\s*$",
    re.IGNORECASE,
)
OUT_WRAP_BARE_PREFIX_RE = re.compile(
    rf"\bYou\s+(?P<kind>sneak\s+attack|hit)\s+"
    rf"(?P<target>.+?)\s+for\s+"
    rf"(?P<amount>{NUM_TOKEN})\s+points?\s+of\s*$",
    re.IGNORECASE,
)


def _clean_continuation_row(line):
    line = normalize_text(line)
    return re.sub(r"^[\|\[\]{}Il1:;`'.,_-]+\s*", "", line).strip()


def damage_wrap_kind(line):
    """Return 'typed', 'bare', or None for an incomplete outgoing damage row."""
    line = normalize_text(line)
    if OUT_WRAP_TYPED_PREFIX_RE.search(line):
        return "typed"
    if OUT_WRAP_BARE_PREFIX_RE.search(line):
        return "bare"
    return None


def join_damage_wrap(prefix, continuation, kind=None):
    """Return a complete logical damage row if this exact two-row wrap is valid."""
    prefix = normalize_text(prefix)
    cont = _clean_continuation_row(continuation)
    kind = kind or damage_wrap_kind(prefix)
    if kind == "typed":
        if re.match(r"^damage\b", cont, re.IGNORECASE):
            return normalize_text(prefix + " " + cont)
    elif kind == "bare":
        if re.match(r"^[A-Za-z][A-Za-z -]*?\s+damage\b", cont, re.IGNORECASE):
            return normalize_text(prefix + " " + cont)
    return None


def reassemble_wrapped_damage_rows(texts, line_times=None):
    """Recover only safe two-row wrapped damage messages.

    Every ordinary OCR row remains independent.  Only a row that ends in an
    unmistakably incomplete outgoing-damage prefix may consume the immediately
    following row.  The first row's timestamp/line number is retained.
    """
    out_texts, out_times, out_lines = [], [], []
    i = 0
    while i < len(texts):
        line = normalize_text(texts[i])
        elapsed = line_times[i] if line_times is not None and i < len(line_times) else None
        kind = damage_wrap_kind(line)
        if kind and i + 1 < len(texts):
            joined = join_damage_wrap(line, texts[i + 1], kind)
            if joined:
                out_texts.append(joined)
                out_times.append(elapsed)
                out_lines.append(i + 1)
                i += 2
                continue
        out_texts.append(line)
        out_times.append(elapsed)
        out_lines.append(i + 1)
        i += 1
    return out_texts, out_times, out_lines

HEAL_TARGET_RE = re.compile(
    rf"(?:\(Combat\):\s*)?"
    rf"(?P<target>.+?)\s+(?:was|were)\s+healed\s+by\s+you\s+for\s+"
    rf"(?P<amount>{NUM_TOKEN})\s+points?",
    re.IGNORECASE,
)

YOU_HEAL_RE = re.compile(
    rf"\bYou\s+heal\s+(?P<target>.+?)\s+for\s+"
    rf"(?P<amount>{NUM_TOKEN})\s+points?",
    re.IGNORECASE,
)

IN_HIT_RE = re.compile(
    rf"(?:\(Combat\):\s*)?"
    rf"(?P<source>.+?)\s+(?:hit|hits)\s+you\s+for\s+"
    rf"(?:a\s+total\s+of\s+)?(?P<amount>{NUM_TOKEN})\s+points?\s+of\s+"
    rf"(?P<dtype>[A-Za-z][A-Za-z -]*?)\s+damage\b",
    re.IGNORECASE,
)

IN_WERE_HIT_RE = re.compile(
    rf"\bYou\s+(?:were|are)\s+hit\s+by\s+(?P<source>.+?)\s+for\s+"
    rf"(?:a\s+total\s+of\s+)?(?P<amount>{NUM_TOKEN})\s+points?\s+of\s+"
    rf"(?P<dtype>[A-Za-z][A-Za-z -]*?)\s+damage\b",
    re.IGNORECASE,
)

IN_SUFFER_RE = re.compile(
    rf"\bYou\s+suffer\s+(?:a\s+total\s+of\s+)?"
    rf"(?P<amount>{NUM_TOKEN})\s+points?\s+of\s+damage\s+from\s+"
    rf"(?P<dtype>[A-Za-z][A-Za-z -]*?)"
    rf"(?=\s+after\b|[.!]|$)",
    re.IGNORECASE,
)

IN_TAKE_RE = re.compile(
    rf"\bYou\s+take\s+"
    rf"(?:a\s+total\s+of\s+)?(?P<amount>{NUM_TOKEN})\s+points?\s+of\s+"
    rf"(?P<dtype>[A-Za-z][A-Za-z -]*?)\s+damage\b",
    re.IGNORECASE,
)

# DDO defensive attack feedback, e.g.
#   "(Combat): Orc misses you!"
#   "(Combat): Orc misses you, because of your Dodge!"
# The exact reason wording can vary, so classify by stable keywords below.
ENEMY_MISS_RE = re.compile(
    r"(?:\(Combat\):\s*)?(?P<source>.+?)\s+misses\s+you"
    r"(?:,\s+because\s+(?P<reason>.+?))?[!.]?\s*$",
    re.IGNORECASE,
)

ATTACK_RE = re.compile(
    rf"\bYou\s+attack\s+(?P<target>.+?)\.\s+You\s+roll\s+a\s+"
    rf"(?P<roll>{NUM_TOKEN})"
    rf"(?:\s*\(\+(?P<bonus>{NUM_TOKEN})\))?\s*:\s*"
    rf"(?P<result>.+)$",
    re.IGNORECASE,
)

CONFIRM_RE = re.compile(
    rf"\bYou\s+roll\s+to\s+confirm\s+a\s+critical\s+on\s+"
    rf"(?P<target>.+?)\.\s+You\s+roll\s+a\s+"
    rf"(?P<roll>{NUM_TOKEN})"
    rf"(?:\s*\(\+(?P<bonus>{NUM_TOKEN})\))?\s*:\s*"
    rf"(?P<result>.+)$",
    re.IGNORECASE,
)


def canonical_target(s):
    text = re.sub(r"\s+", " ", str(s or "").strip())
    # OCR frequently corrupts the leading Combat channel marker into forms
    # such as ICombat):, -Combat): or „Combat):.  If a parser capture has
    # swallowed that marker, remove it before the entity name is stored.
    text = re.sub(
        r"^\s*[^A-Za-z0-9]{0,3}[Il1]?\s*\(?Combat\)?\s*:\s*",
        "", text, count=1, flags=re.IGNORECASE,
    )
    return text.strip().strip(" .,:;")


COMBAT_MESSAGE_START_RE = re.compile(
    r"^\s*[|\[\]{}Il:;`'.,_-]{0,3}\s*\(?\s*Combat\s*\)?\s*:",
    re.IGNORECASE,
)


def reassemble_wrapped_combat_messages(texts, line_times=None):
    """Join visual OCR rows that belong to one wrapped Combat Log message.

    DDO can wrap a single logical Combat Log message across multiple visual
    rows.  Continuation rows do not begin with ``(Combat):``.  Parsing the
    visual rows independently therefore loses information such as critical
    confirmation bonuses/results.

    Returns ``(messages, times, source_lines)``.  The first visual row's
    timestamp and 1-based line number are retained for each logical message.
    Rows before the first recognizable Combat prefix are preserved as
    standalone messages rather than discarded.
    """
    messages = []
    times = []
    source_lines = []

    current_parts = []
    current_time = None
    current_line = None

    def flush_current():
        nonlocal current_parts, current_time, current_line
        if current_parts:
            messages.append(normalize_text(" ".join(current_parts)))
            times.append(current_time)
            source_lines.append(current_line)
        current_parts = []
        current_time = None
        current_line = None

    for idx, raw in enumerate(texts):
        line = normalize_text(raw)
        if not line:
            continue
        elapsed = None
        if line_times is not None and idx < len(line_times):
            elapsed = line_times[idx]

        if COMBAT_MESSAGE_START_RE.search(line):
            flush_current()
            current_parts = [line]
            current_time = elapsed
            current_line = idx + 1
        elif current_parts:
            # OCR occasionally leaves a border/separator glyph in front of a
            # continuation row.  Remove only obvious non-text edge noise; do
            # not strip '(' because confirmations often continue with (+N).
            continuation = re.sub(
                r"^[\|\[\]{}Il:;`'.,_-]+\s*", "", line
            ).strip()
            if continuation:
                current_parts.append(continuation)
        else:
            # Keep prefix-less rows rather than silently dropping them.
            messages.append(line)
            times.append(elapsed)
            source_lines.append(idx + 1)

    flush_current()
    return messages, times, source_lines


def parse_blocked_amount(line):
    m = BLOCKED_AFTER_RE.search(line)
    if not m:
        return 0, "", "", False
    value, raw, corrected, changed = parse_ocr_number(m.group("blocked"))
    return (int(value or 0), raw, corrected, changed)


def parse_combat_events(texts, line_times=None, line_numbers=None):
    outgoing = []
    incoming = []
    healing = []
    corrections = []
    suspicious = []

    for idx, line in enumerate(texts):
        line_no = (
            line_numbers[idx]
            if line_numbers is not None and idx < len(line_numbers)
            else idx + 1
        )
        parsed = False
        elapsed_seconds = None
        if line_times is not None and idx < len(line_times):
            elapsed_seconds = line_times[idx]

        m = OUT_RE.search(line)
        untyped = False
        if not m:
            m = OUT_UNTYPED_RE.search(line)
            untyped = m is not None
        if m:
            value, raw, corrected, changed = parse_ocr_number(m.group("amount"))
            if value is not None:
                mode = "sneak" if "sneak" in m.group("kind").lower() else "regular"
                target = canonical_target(m.group("target"))
                blocked, blocked_raw, blocked_corrected, blocked_changed = parse_blocked_amount(line)
                damage_type = "untyped" if untyped else clean_damage_type(m.group("dtype"))
                reduction_type, reduction_class = parse_reduction_type(line, damage_type)
                event = {
                    "line": line_no,
                    "elapsed_seconds": elapsed_seconds,
                    "direction": "outgoing",
                    "mode": mode,
                    "source": "You",
                    "target": target,
                    "damage_type": damage_type,
                    "amount": value,
                    "blocked_amount": blocked,
                    "pre_mitigation_amount": value + blocked,
                    "reduction_type": reduction_type,
                    "reduction_class": reduction_class,
                    "ocr_token": raw,
                    "parsed_token": corrected,
                    "text": line,
                }
                outgoing.append(event)
                if changed:
                    corrections.append({
                        "line": line_no,
                        "field": "damage",
                        "original": raw,
                        "corrected": corrected,
                        "text": line,
                    })
                if blocked_changed:
                    corrections.append({
                        "line": line_no, "field": "blocked_damage",
                        "original": blocked_raw, "corrected": blocked_corrected, "text": line,
                    })
                parsed = True

        if not parsed:
            zm = ZERO_BLOCKED_OUT_RE.search(line)
            if zm:
                blocked, braw, bcorr, bchanged = parse_ocr_number(zm.group("blocked"))
                if blocked is not None:
                    mode = "sneak" if "sneak" in zm.group("kind").lower() else "regular"
                    zero_reason = zm.groupdict().get("reason") or ""
                    reduction_type, reduction_class = normalize_reduction_type(zero_reason, "absorbed") if zero_reason else parse_reduction_type(line, "absorbed")
                    outgoing.append({
                        "line": line_no, "elapsed_seconds": elapsed_seconds,
                        "direction": "outgoing", "mode": mode, "source": "You",
                        "target": canonical_target(zm.group("target")),
                        "damage_type": "absorbed", "amount": 0,
                        "blocked_amount": int(blocked), "pre_mitigation_amount": int(blocked),
                        "reduction_type": reduction_type, "reduction_class": reduction_class,
                        "ocr_token": "0", "parsed_token": "0", "text": line,
                    })
                    if bchanged:
                        corrections.append({
                            "line": line_no, "field": "blocked_damage",
                            "original": braw, "corrected": bcorr, "text": line,
                        })
                    parsed = True

        if not parsed:
            m = HEAL_TARGET_RE.search(line) or YOU_HEAL_RE.search(line)
            if m:
                value, raw, corrected, changed = parse_ocr_number(m.group("amount"))
                if value is not None:
                    target = canonical_target(m.group("target"))
                    healing.append({
                        "line": line_no,
                        "elapsed_seconds": elapsed_seconds,
                        "source": "You",
                        "target": target,
                        "amount": value,
                        "ocr_token": raw,
                        "parsed_token": corrected,
                        "text": line,
                    })
                    if changed:
                        corrections.append({
                            "line": line_no,
                            "field": "healing",
                            "original": raw,
                            "corrected": corrected,
                            "text": line,
                        })
                    parsed = True

        if not parsed:
            m = IN_HIT_RE.search(line) or IN_WERE_HIT_RE.search(line)
            if m:
                value, raw, corrected, changed = parse_ocr_number(m.group("amount"))
                if value is not None:
                    incoming.append({
                        "line": line_no,
                        "elapsed_seconds": elapsed_seconds,
                        "direction": "incoming",
                        "mode": "",
                        "source": canonical_target(m.group("source")),
                        "target": "You",
                        "damage_type": clean_damage_type(m.group("dtype")),
                        "amount": value,
                        "blocked_amount": parse_blocked_amount(line)[0],
                        "pre_mitigation_amount": value + parse_blocked_amount(line)[0],
                        "reduction_type": parse_reduction_type(line, clean_damage_type(m.group("dtype")))[0],
                        "reduction_class": parse_reduction_type(line, clean_damage_type(m.group("dtype")))[1],
                        "ocr_token": raw,
                        "parsed_token": corrected,
                        "text": line,
                    })
                    if changed:
                        corrections.append({
                            "line": line_no,
                            "field": "incoming_damage",
                            "original": raw,
                            "corrected": corrected,
                            "text": line,
                        })
                    parsed = True

        if not parsed:
            m = IN_SUFFER_RE.search(line) or IN_TAKE_RE.search(line)
            if m:
                value, raw, corrected, changed = parse_ocr_number(m.group("amount"))
                if value is not None:
                    incoming.append({
                        "line": line_no,
                        "elapsed_seconds": elapsed_seconds,
                        "direction": "incoming",
                        "mode": "",
                        "source": "environment/effect",
                        "target": "You",
                        "damage_type": clean_damage_type(m.group("dtype")),
                        "amount": value,
                        "blocked_amount": parse_blocked_amount(line)[0],
                        "pre_mitigation_amount": value + parse_blocked_amount(line)[0],
                        "reduction_type": parse_reduction_type(line, clean_damage_type(m.group("dtype")))[0],
                        "reduction_class": parse_reduction_type(line, clean_damage_type(m.group("dtype")))[1],
                        "ocr_token": raw,
                        "parsed_token": corrected,
                        "text": line,
                    })
                    if changed:
                        corrections.append({
                            "line": line_no,
                            "field": "incoming_damage",
                            "original": raw,
                            "corrected": corrected,
                            "text": line,
                        })
                    parsed = True

        # Flag lines that look damage/healing-related but did not parse.
        if not parsed:
            low = line.lower()
            if (
                (" damage" in low and ("you " in low or " you " in low))
                or ("healed" in low and "you" in low)
            ):
                suspicious.append((line_no, line))

    return outgoing, incoming, healing, corrections, suspicious


def classify_incoming_miss_reason(reason_text):
    low = normalize_text(reason_text or "").lower()
    if not low:
        return "regular_miss"
    if "dodg" in low:
        return "dodge"
    if "blur" in low:
        return "blur"
    if "displac" in low:
        return "displacement"
    if "deflect" in low:
        return "deflect"
    return "other"


def parse_incoming_defense_events(texts, line_times=None, line_numbers=None):
    """Parse explicit enemy-miss/avoidance feedback from the DDO Combat Log.

    These are kept separate from incoming damage because they are outcomes of
    hostile attack attempts rather than damage events.  The normalized reason
    is deliberately evidence-based: only keywords present in the log are used.
    """
    events = []
    for idx, line in enumerate(texts):
        # OCR often renders the Combat prefix as "ICombat):" or similar.
        # COMBAT_MESSAGE_START_RE already tolerates those variants; strip it
        # before extracting the source so the prefix never becomes part of the
        # mob name.
        body = re.sub(
            r"^\s*.{0,6}?Combat\s*\)?\s*:\s*", "", line, count=1, flags=re.IGNORECASE
        ).strip()
        m = ENEMY_MISS_RE.search(body)
        if not m:
            continue
        line_no = (
            line_numbers[idx]
            if line_numbers is not None and idx < len(line_numbers)
            else idx + 1
        )
        elapsed_seconds = None
        if line_times is not None and idx < len(line_times):
            elapsed_seconds = line_times[idx]
        reason_text = normalize_text(m.group("reason") or "").strip(" .!")
        events.append({
            "line": line_no,
            "elapsed_seconds": elapsed_seconds,
            "event": "incoming_miss",
            "source": canonical_target(m.group("source")),
            "target": "You",
            "reason": classify_incoming_miss_reason(reason_text),
            "reason_text": reason_text,
            "text": line,
        })
    return events


def parse_attack_crit_events(texts, line_times=None, line_numbers=None):
    """Parse player attack rolls and critical confirmation rolls.

    An attack line counts once toward APM. A line whose result contains
    "critical hit" is treated as a critical threat; the following confirmation
    line determines whether the threat became a confirmed crit.
    """
    attacks = []
    confirms = []
    last_threat_by_target = {}
    last_attack_index_by_target = {}

    for idx, line in enumerate(texts):
        line_no = (
            line_numbers[idx]
            if line_numbers is not None and idx < len(line_numbers)
            else idx + 1
        )
        elapsed_seconds = None
        if line_times is not None and idx < len(line_times):
            elapsed_seconds = line_times[idx]

        m = ATTACK_RE.search(line)
        if m:
            roll, _, _, _ = parse_ocr_number(m.group("roll"))
            bonus = None
            if m.group("bonus"):
                bonus, _, _, _ = parse_ocr_number(m.group("bonus"))
            result_text = normalize_text(m.group("result"))
            low = result_text.lower()
            if "miss" in low:
                outcome = "miss"
            elif "grazing hit" in low:
                outcome = "grazing_hit"
            elif "critical hit" in low:
                outcome = "critical_threat"
            elif "hit" in low:
                outcome = "hit"
            else:
                outcome = "other"
            target = canonical_target(m.group("target"))
            event = {
                "line": line_no,
                "elapsed_seconds": elapsed_seconds,
                "event": "attack",
                "target": target,
                "roll": roll,
                "bonus": bonus,
                "total": (roll + bonus) if roll is not None and bonus is not None else None,
                "outcome": outcome,
                "confirmed": "",
                "linked_attack_line": "",
                "text": line,
            }
            attacks.append(event)
            last_attack_index_by_target[target] = len(attacks) - 1
            if outcome == "critical_threat":
                last_threat_by_target[target] = line_no
            continue

        m = CONFIRM_RE.search(line)
        if m:
            roll, _, _, _ = parse_ocr_number(m.group("roll"))
            bonus = None
            if m.group("bonus"):
                bonus, _, _, _ = parse_ocr_number(m.group("bonus"))
            result_text = normalize_text(m.group("result"))
            confirmed = "critical hit" in result_text.lower()
            target = canonical_target(m.group("target"))

            # A confirmation roll is itself authoritative evidence that the
            # preceding attack generated a critical threat.  If OCR classified
            # that attack merely as a normal hit, upgrade the most recent attack
            # on the same target instead of losing the threat.
            linked_attack_line = last_threat_by_target.get(target, "")
            if not linked_attack_line:
                ai = last_attack_index_by_target.get(target)
                if ai is not None and 0 <= ai < len(attacks):
                    prior = attacks[ai]
                    if prior.get("outcome") not in ("miss", "grazing_hit"):
                        prior["outcome"] = "critical_threat"
                        linked_attack_line = prior.get("line", "")
                        last_threat_by_target[target] = linked_attack_line

            confirms.append({
                "line": line_no,
                "elapsed_seconds": elapsed_seconds,
                "event": "confirm",
                "target": target,
                "roll": roll,
                "bonus": bonus,
                "total": (roll + bonus) if roll is not None and bonus is not None else None,
                "outcome": "confirmed" if confirmed else "failed",
                "confirmed": 1 if confirmed else 0,
                "linked_attack_line": linked_attack_line,
                "text": line,
            })
            last_threat_by_target.pop(target, None)

    return attacks, confirms


def observed_confirm_label(highest_failed, lowest_success):
    if highest_failed is not None and lowest_success is not None:
        lo = highest_failed + 1
        hi = lowest_success
        if lo == hi:
            return str(hi)
        if lo < hi:
            return f"{lo}-{hi}"
        # Conflicting observations can occur if target defenses/debuffs changed.
        return f"varies ({highest_failed} fail / {lowest_success} success)"
    if lowest_success is not None:
        return f"<={lowest_success}"
    if highest_failed is not None:
        return f">{highest_failed}"
    return "-"


def observed_hit_label(highest_miss, lowest_hit):
    if highest_miss is not None and lowest_hit is not None:
        lo = highest_miss + 1
        hi = lowest_hit
        if lo == hi:
            return str(hi)
        if lo < hi:
            return f"{lo}-{hi}"
        return f"varies ({highest_miss} miss / {lowest_hit} hit)"
    if lowest_hit is not None:
        return f"<={lowest_hit}"
    if highest_miss is not None:
        return f">{highest_miss}"
    return "-"


def summarize_attack_crit(attacks, confirms):
    stats = defaultdict(lambda: {
        "attacks": 0, "hits": 0, "grazes": 0, "misses": 0, "other": 0,
        "threats": 0, "confirm_attempts": 0, "confirmed": 0, "failed": 0,
        "highest_failed": None, "lowest_success": None,
        "highest_miss_roll": None, "lowest_full_hit_roll": None,
        "highest_miss_total": None, "lowest_full_hit_total": None,
    })
    for a in attacks:
        d = stats[a["target"]]
        d["attacks"] += 1
        outcome = a["outcome"]
        if outcome == "miss":
            d["misses"] += 1
        elif outcome == "grazing_hit":
            d["grazes"] += 1
        elif outcome in ("hit", "critical_threat"):
            d["hits"] += 1
        else:
            d["other"] += 1
        if outcome == "critical_threat":
            d["threats"] += 1

        # Evidence-based attack threshold bounds. Natural 1/20 are excluded
        # because automatic outcome rules can make them poor AC evidence.
        # Grazing hits are deliberately neither misses nor full hits here.
        roll = a.get("roll")
        total = a.get("total")
        threshold_eligible = roll is not None and roll not in (1, 20)
        if threshold_eligible and outcome == "miss":
            if d["highest_miss_roll"] is None or roll > d["highest_miss_roll"]:
                d["highest_miss_roll"] = roll
            if total is not None and (d["highest_miss_total"] is None or total > d["highest_miss_total"]):
                d["highest_miss_total"] = total
        elif threshold_eligible and outcome in ("hit", "critical_threat"):
            if d["lowest_full_hit_roll"] is None or roll < d["lowest_full_hit_roll"]:
                d["lowest_full_hit_roll"] = roll
            if total is not None and (d["lowest_full_hit_total"] is None or total < d["lowest_full_hit_total"]):
                d["lowest_full_hit_total"] = total

    for c in confirms:
        d = stats[c["target"]]
        d["confirm_attempts"] += 1
        if not c.get("linked_attack_line"):
            d["threats"] += 1
        total = c.get("total")
        roll = c.get("roll")
        # Natural 1/20 can have automatic outcome rules and therefore do not
        # establish the numeric confirmation threshold. They still count in
        # confirmation/crit rates, but are excluded from threshold bounds.
        threshold_eligible = total is not None and roll not in (1, 20)
        if c["confirmed"]:
            d["confirmed"] += 1
            if threshold_eligible:
                if d["lowest_success"] is None or total < d["lowest_success"]:
                    d["lowest_success"] = total
        else:
            d["failed"] += 1
            if threshold_eligible:
                if d["highest_failed"] is None or total > d["highest_failed"]:
                    d["highest_failed"] = total

    for d in stats.values():
        d["crit_rate"] = (100.0 * d["confirmed"] / d["attacks"]) if d["attacks"] else 0.0
        d["crit_on_hits"] = (100.0 * d["confirmed"] / d["hits"]) if d["hits"] else 0.0
        d["graze_rate"] = (100.0 * d["grazes"] / d["attacks"]) if d["attacks"] else 0.0
        d["threat_rate"] = (100.0 * d["threats"] / d["attacks"]) if d["attacks"] else 0.0
        d["confirm_rate"] = (100.0 * d["confirmed"] / d["confirm_attempts"]) if d["confirm_attempts"] else 0.0
        d["observed_confirm"] = observed_confirm_label(d["highest_failed"], d["lowest_success"])
        d["observed_hit_total"] = observed_hit_label(d["highest_miss_total"], d["lowest_full_hit_total"])
    return stats


def build_apm_rows(attacks, window_seconds=15.0):
    timed = [a for a in attacks if a.get("elapsed_seconds") is not None]
    timed.sort(key=lambda a: float(a["elapsed_seconds"]))
    rows = []
    if not timed:
        return rows
    first_t = float(timed[0]["elapsed_seconds"])
    last_t = float(timed[-1]["elapsed_seconds"])
    rel = [float(a["elapsed_seconds"]) - first_t for a in timed]
    step = 0.25
    left = 0
    right = 0
    t = 0.0
    while t <= (last_t - first_t) + 1e-9:
        while right < len(rel) and rel[right] <= t + 1e-9:
            right += 1
        cutoff = t - window_seconds
        while left < right and rel[left] <= cutoff:
            left += 1
        count = right - left
        rows.append((t, first_t + t, count * 60.0 / window_seconds, count))
        t += step
    return rows


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def fmt(n):
    return f"{n:,}"


def aggregate(outgoing, incoming, healing):
    target_stats = defaultdict(lambda: {
        "regular": 0,
        "sneak": 0,
        "damage": 0,
        "healing": 0,
        "net_hp_loss": 0,
        "hits": 0,
    })
    overall_type = defaultdict(lambda: {
        "regular": 0,
        "sneak": 0,
        "total": 0,
        "hits": 0,
    })
    target_type = defaultdict(lambda: defaultdict(lambda: {
        "regular": 0,
        "sneak": 0,
        "total": 0,
        "hits": 0,
    }))
    incoming_type = defaultdict(lambda: {"total": 0, "hits": 0})
    incoming_source = defaultdict(lambda: {"total": 0, "hits": 0})

    for e in outgoing:
        t = target_stats[e["target"]]
        t[e["mode"]] += e["amount"]
        t["damage"] += e["amount"]
        t["hits"] += 1

        d = overall_type[e["damage_type"]]
        d[e["mode"]] += e["amount"]
        d["total"] += e["amount"]
        d["hits"] += 1

        td = target_type[e["target"]][e["damage_type"]]
        td[e["mode"]] += e["amount"]
        td["total"] += e["amount"]
        td["hits"] += 1

    for h in healing:
        t = target_stats[h["target"]]
        t["healing"] += h["amount"]

    for target, t in target_stats.items():
        t["net_hp_loss"] = t["damage"] - t["healing"]

    for e in incoming:
        d = incoming_type[e["damage_type"]]
        d["total"] += e["amount"]
        d["hits"] += 1

        s = incoming_source[e["source"]]
        s["total"] += e["amount"]
        s["hits"] += 1

    return target_stats, overall_type, target_type, incoming_type, incoming_source


def recording_duration_seconds(session, outgoing=None):
    try:
        data = json.loads((session / "recording_clock.json").read_text(encoding="utf-8"))
        value = float(data.get("capture_elapsed_seconds", 0.0))
        if value > 0:
            return value
        if data.get("end_epoch") and data.get("start_epoch"):
            value = float(data["end_epoch"]) - float(data["start_epoch"])
            if value > 0:
                return value
    except Exception:
        pass
    try:
        for line in (session / "recording_summary.txt").read_text(encoding="utf-8").splitlines():
            if line.startswith("Elapsed seconds:"):
                value = float(line.split(":", 1)[1].strip())
                if value > 0:
                    return value
    except Exception:
        pass
    times = [float(e["elapsed_seconds"]) for e in (outgoing or []) if e.get("elapsed_seconds") is not None]
    if len(times) >= 2:
        return max(times) - min(times)
    return 0.0


def active_combat_seconds(outgoing, inactivity_timeout=5.0):
    times = sorted(float(e["elapsed_seconds"]) for e in outgoing if e.get("elapsed_seconds") is not None)
    if len(times) < 2:
        return 0.0
    total = 0.0
    for a, b in zip(times, times[1:]):
        total += min(max(0.0, b - a), float(inactivity_timeout))
    return total


def session_damage_timing(outgoing, batch_merge_seconds=0.10):
    """Estimate the observed damage-session duration without fencepost bias.

    Multiple DDO damage components from one attack often share the same timestamp
    or arrive only a few hundredths of a second apart.  First cluster such nearby
    timestamps into damage batches.  The session is then measured from the first
    batch to the last batch plus one typical batch interval (the median interval).

    Adding that final interval is the event/time analogue of a fencepost
    correction: N regularly spaced batches contain N intervals of exposure, not
    only the N-1 gaps between the first and last batch timestamps.
    """
    times = sorted(
        float(e["elapsed_seconds"])
        for e in outgoing
        if e.get("elapsed_seconds") is not None
    )
    if not times:
        return {
            "batch_count": 0,
            "first_damage_seconds": None,
            "last_damage_seconds": None,
            "damage_span_seconds": 0.0,
            "typical_batch_interval_seconds": None,
            "session_duration_seconds": 0.0,
        }

    # Work from unique timestamps first.  Reconstruction commonly assigns the
    # exact same time to all damage lines revealed by one visual update.
    unique_times = []
    for t in times:
        if not unique_times or abs(t - unique_times[-1]) > 1e-9:
            unique_times.append(t)

    batch_starts = []
    last_in_batch = None
    merge = max(0.0, float(batch_merge_seconds))
    for t in unique_times:
        if last_in_batch is None or (t - last_in_batch) > merge:
            batch_starts.append(t)
        last_in_batch = t

    first_t = batch_starts[0]
    last_t = batch_starts[-1]
    span = max(0.0, last_t - first_t)

    if len(batch_starts) < 2:
        return {
            "batch_count": 1,
            "first_damage_seconds": first_t,
            "last_damage_seconds": last_t,
            "damage_span_seconds": span,
            "typical_batch_interval_seconds": None,
            "session_duration_seconds": 0.0,
        }

    intervals = [
        max(0.0, b - a)
        for a, b in zip(batch_starts, batch_starts[1:])
        if b > a
    ]
    if not intervals:
        typical = None
        session_duration = 0.0
    else:
        intervals.sort()
        n = len(intervals)
        if n % 2:
            typical = intervals[n // 2]
        else:
            typical = 0.5 * (intervals[n // 2 - 1] + intervals[n // 2])
        session_duration = span + typical

    return {
        "batch_count": len(batch_starts),
        "first_damage_seconds": first_t,
        "last_damage_seconds": last_t,
        "damage_span_seconds": span,
        "typical_batch_interval_seconds": typical,
        "session_duration_seconds": session_duration,
    }


def csv_row_count(path):
    try:
        with path.open("r", encoding="utf-8", newline="") as f:
            return sum(1 for _ in csv.DictReader(f))
    except Exception:
        return 0


def create_reports(
    session, texts, outgoing, incoming, healing, attacks, confirms, defensive_misses,
    corrections, suspicious, frame_no, visual_line_count
):
    target_stats, overall_type, target_type, incoming_type, incoming_source = aggregate(
        outgoing, incoming, healing
    )
    attack_stats = summarize_attack_crit(attacks, confirms)

    # Recording is intentionally paused whenever DDO is not foreground. Keep
    # those gaps visible in the report/data-quality summary because combat may
    # have continued while the Combat Log could not be observed.
    focus_interruptions = []
    focus_interruption_seconds = 0.0
    focus_path = session / "focus_interruptions.csv"
    if focus_path.exists():
        try:
            with focus_path.open("r", encoding="utf-8", newline="") as f:
                focus_interruptions = list(csv.DictReader(f))
            for row in focus_interruptions:
                try:
                    focus_interruption_seconds += max(0.0, float(row.get("duration_seconds", "") or 0.0))
                except Exception:
                    pass
        except Exception:
            focus_interruptions = []
            focus_interruption_seconds = 0.0

    total_regular = sum(e["amount"] for e in outgoing if e["mode"] == "regular")
    total_sneak = sum(e["amount"] for e in outgoing if e["mode"] == "sneak")
    total_dealt = total_regular + total_sneak
    total_healing_to_targets = sum(h["amount"] for h in healing)
    total_net_hp_loss = total_dealt - total_healing_to_targets
    total_taken = sum(e["amount"] for e in incoming)
    total_blocked_out = sum(int(e.get("blocked_amount", 0) or 0) for e in outgoing)
    total_blocked_in = sum(int(e.get("blocked_amount", 0) or 0) for e in incoming)
    duration = recording_duration_seconds(session, outgoing)
    inactivity_timeout = 5.0
    rolling_window_seconds = 15.0
    try:
        cfg_for_stats = json.loads((session / "config_used.json").read_text(encoding="utf-8"))
        inactivity_timeout = float(cfg_for_stats.get("analysis", {}).get("combat_inactivity_timeout_seconds", 5.0))
        rolling_window_seconds = max(1.0, float(cfg_for_stats.get("live_dps", {}).get("rolling_window_seconds", 15.0)))
    except Exception:
        pass
    active_seconds = active_combat_seconds(outgoing, inactivity_timeout)
    damage_timing = session_damage_timing(outgoing)
    session_duration = float(damage_timing.get("session_duration_seconds") or 0.0)
    session_dps = (total_dealt / session_duration) if session_duration > 0 else None
    recording_dps = (total_dealt / duration) if duration > 0 else 0.0
    active_dps = (total_dealt / active_seconds) if active_seconds > 0 else 0.0
    sneak_hits = sum(1 for e in outgoing if e.get("mode") == "sneak")
    sneak_share = (100.0 * total_sneak / total_dealt) if total_dealt else 0.0
    sneak_avg = (total_sneak / sneak_hits) if sneak_hits else 0.0
    sneak_per_min = (60.0 * sneak_hits / duration) if duration > 0 else 0.0

    lines = []
    lines.append("DDO COMBAT REPORT v4")
    lines.append("=" * 78)
    lines.append("")
    lines.append("OVERALL")
    lines.append("-" * 78)
    lines.append(f"Total damage dealt:          {fmt(total_dealt)}")
    lines.append(f"  Regular damage:            {fmt(total_regular)}")
    lines.append(f"  Sneak attack damage:       {fmt(total_sneak)}")
    lines.append(f"Healing done to targets:     {fmt(total_healing_to_targets)}")
    lines.append(f"Net target HP loss:          {fmt(total_net_hp_loss)}")
    lines.append(f"Total damage taken:          {fmt(total_taken)}")
    lines.append(f"Player attacks parsed:       {len(attacks)}")
    lines.append(f"Grazing hits:                {sum(d['grazes'] for d in attack_stats.values())}")
    lines.append(f"Confirmed critical hits:     {sum(d['confirmed'] for d in attack_stats.values())}")
    lines.append(f"Blocked outgoing damage:     {fmt(total_blocked_out)}")
    lines.append(f"Recording duration:          {duration:.2f} s")
    lines.append(f"Recording DPS:               {recording_dps:,.1f}")
    if session_dps is not None:
        lines.append(f"Damage span, first-last:     {damage_timing['damage_span_seconds']:.2f} s")
        lines.append(f"Typical damage-batch gap:    {damage_timing['typical_batch_interval_seconds']:.3f} s")
        lines.append(f"Fencepost-adjusted session:  {session_duration:.2f} s")
        lines.append(f"Session DPS:                 {session_dps:,.1f}")
    else:
        lines.append("Session DPS:                 N/A (need at least two damage batches)")
    lines.append(f"Active combat time:          {active_seconds:.2f} s")
    lines.append(f"Active combat DPS:           {active_dps:,.1f}")
    lines.append(f"Rolling DPS/APM window:      {rolling_window_seconds:g} s")
    lines.append(f"Sneak attacks:               {sneak_hits}")
    lines.append(f"Sneak share of damage:       {sneak_share:.1f}%")
    lines.append(f"Average sneak damage:        {sneak_avg:,.1f}")
    lines.append(f"Sneak attacks/min:           {sneak_per_min:.1f}")
    lines.append("")

    lines.append("ATTACK / CRITICAL STATISTICS BY TARGET")
    lines.append("-" * 128)
    lines.append(
        f"{'Target':<28}{'Attacks':>8}{'Hits':>7}{'Graze':>7}{'Miss':>7}{'Threats':>9}"
        f"{'Crits':>7}{'Fail conf':>10}{'Graze %':>9}{'Crit %':>9}{'Conf %':>9}{'Observed confirm':>17}"
    )
    for target, d in sorted(attack_stats.items(), key=lambda kv: (-kv[1]['attacks'], kv[0].lower())):
        lines.append(
            f"{target[:28]:<28}{d['attacks']:>8}{d['hits']:>7}{d['grazes']:>7}{d['misses']:>7}{d['threats']:>9}"
            f"{d['confirmed']:>7}{d['failed']:>10}{d['graze_rate']:>8.1f}%{d['crit_rate']:>8.1f}%{d['confirm_rate']:>8.1f}%"
            f"{d['observed_confirm']:>17}"
        )
    if not attack_stats:
        lines.append("(No player attack-roll lines recognized.)")
    lines.append("")
    lines.append("Observed confirm is evidence-based: e.g. <=186 means 186 succeeded, but no failure proves the exact threshold.")
    lines.append("Natural 1/20 confirmation rolls are excluded from threshold bounds; they still count in crit/confirm rates.")
    lines.append("Target defenses and debuffs can change during combat; conflicting observations are reported as varying.")
    lines.append("")

    lines.append("DAMAGE BY TARGET")
    lines.append("-" * 78)
    lines.append(
        f"{'Target':<28}{'Regular':>12}{'Sneak':>12}{'Damage':>12}"
        f"{'Healing':>10}{'Net HP':>12}{'Hits':>7}"
    )
    for target, d in sorted(
        target_stats.items(), key=lambda kv: (-kv[1]["damage"], kv[0].lower())
    ):
        lines.append(
            f"{target[:28]:<28}{fmt(d['regular']):>12}{fmt(d['sneak']):>12}"
            f"{fmt(d['damage']):>12}{fmt(d['healing']):>10}"
            f"{fmt(d['net_hp_loss']):>12}{d['hits']:>7}"
        )
    if not target_stats:
        lines.append("(No outgoing damage recognized.)")
    lines.append("")

    lines.append("OUTGOING DAMAGE MITIGATION / BLOCKING")
    lines.append("-" * 78)
    lines.append(f"Damage dealt:                {fmt(total_dealt)}")
    lines.append(f"Damage blocked/resisted:     {fmt(total_blocked_out)}")
    pre_total = total_dealt + total_blocked_out
    lines.append(f"Pre-mitigation total:        {fmt(pre_total)}")
    lines.append(f"Blocked share:               {(100.0 * total_blocked_out / pre_total) if pre_total else 0.0:.1f}%")
    if total_blocked_out:
        lines.append("")
        lines.append(f"{'Target':<25}{'Reduction':<20}{'Dmg type':<12}{'Dealt':>11}{'Blocked':>11}{'Hits':>7}")
        mitigation_report = defaultdict(lambda: {"dealt": 0, "blocked": 0, "hits": 0})
        for e in outgoing:
            blocked = int(e.get("blocked_amount", 0) or 0)
            if blocked <= 0:
                continue
            key = (
                e.get("target", "Unknown"),
                e.get("reduction_type", "") or "Unknown",
                e.get("damage_type", "unknown"),
            )
            d = mitigation_report[key]
            d["dealt"] += int(e.get("amount", 0) or 0)
            d["blocked"] += blocked
            d["hits"] += 1
        for (target, reduction, dtype), d in sorted(
            mitigation_report.items(),
            key=lambda kv: (-kv[1]["blocked"], kv[0][0].lower(), kv[0][1].lower())
        ):
            lines.append(
                f"{target[:25]:<25}{reduction[:20]:<20}{dtype[:12]:<12}"
                f"{fmt(d['dealt']):>11}{fmt(d['blocked']):>11}{d['hits']:>7}"
            )
    lines.append("")

    lines.append("ALL OUTGOING DAMAGE BY TYPE")
    lines.append("-" * 78)
    lines.append(
        f"{'Type':<20}{'Regular':>15}{'Sneak':>15}{'Total':>15}{'Hits':>8}"
    )
    for dtype, d in sorted(
        overall_type.items(), key=lambda kv: (-kv[1]["total"], kv[0])
    ):
        lines.append(
            f"{dtype:<20}{fmt(d['regular']):>15}{fmt(d['sneak']):>15}"
            f"{fmt(d['total']):>15}{d['hits']:>8}"
        )
    if not overall_type:
        lines.append("(No outgoing damage recognized.)")
    lines.append("")

    # Full per-target type detail in the report file.
    for target, tdata in sorted(
        target_type.items(),
        key=lambda kv: (-target_stats[kv[0]]["damage"], kv[0].lower()),
    ):
        lines.append(f"TARGET DETAIL: {target}")
        lines.append("-" * 78)
        lines.append(
            f"{'Type':<20}{'Regular':>15}{'Sneak':>15}{'Total':>15}{'Hits':>8}"
        )
        for dtype, d in sorted(
            tdata.items(), key=lambda kv: (-kv[1]["total"], kv[0])
        ):
            lines.append(
                f"{dtype:<20}{fmt(d['regular']):>15}{fmt(d['sneak']):>15}"
                f"{fmt(d['total']):>15}{d['hits']:>8}"
            )
        lines.append("")

    lines.append("DAMAGE TAKEN BY TYPE")
    lines.append("-" * 78)
    lines.append(f"{'Type':<28}{'Total':>15}{'Hits':>8}")
    for dtype, d in sorted(
        incoming_type.items(), key=lambda kv: (-kv[1]["total"], kv[0])
    ):
        lines.append(f"{dtype:<28}{fmt(d['total']):>15}{d['hits']:>8}")
    if not incoming_type:
        lines.append("(No incoming damage recognized.)")
    lines.append("")

    lines.append("DAMAGE TAKEN BY SOURCE")
    lines.append("-" * 78)
    lines.append(f"{'Source':<40}{'Total':>15}{'Hits':>8}")
    for source, d in sorted(
        incoming_source.items(), key=lambda kv: (-kv[1]["total"], kv[0].lower())
    ):
        lines.append(f"{source[:40]:<40}{fmt(d['total']):>15}{d['hits']:>8}")
    if not incoming_source:
        lines.append("(No incoming damage recognized.)")
    lines.append("")

    lines.append("DEFENSIVE ATTACK OUTCOMES BY SOURCE")
    lines.append("-" * 78)
    defense_stats = defaultdict(lambda: {
        "regular_miss": 0, "dodge": 0, "blur": 0, "displacement": 0, "deflect": 0, "other": 0
    })
    for e in defensive_misses:
        defense_stats[e.get("source", "Unknown")][e.get("reason", "other")] += 1
    direct_hits_by_source = defaultdict(int)
    for e in incoming:
        source = canonical_target(e.get("source", ""))
        if source:
            direct_hits_by_source[source] += 1
    if defense_stats:
        lines.append(
            f"{'Source':<28}{'Hits':>7}{'Reg miss':>9}{'Dodge':>8}{'Blur':>7}"
            f"{'Displ':>8}{'Deflect':>9}{'Other':>7}{'Miss %':>9}"
        )
        for source, d in sorted(defense_stats.items(), key=lambda kv: (-sum(kv[1].values()), kv[0].lower())):
            direct_hits = direct_hits_by_source.get(source, 0)
            misses_total = sum(d.values())
            attempts = direct_hits + misses_total
            miss_pct = 100.0 * misses_total / attempts if attempts else 0.0
            lines.append(
                f"{source[:28]:<28}{direct_hits:>7}{d['regular_miss']:>9}{d['dodge']:>8}{d['blur']:>7}"
                f"{d['displacement']:>8}{d['deflect']:>9}{d['other']:>7}{miss_pct:>8.1f}%"
            )
    else:
        lines.append("(No explicit enemy-miss/avoidance lines recognized.)")
    lines.append("")

    lines.append("OCR / PARSER CHECKS")
    lines.append("-" * 78)
    lines.append(f"Numeric OCR corrections applied: {len(corrections)}")
    lines.append(f"Damage-like lines not parsed:     {len(suspicious)}")
    lines.append("")
    lines.append("TECHNICAL")
    lines.append("-" * 78)
    lines.append(f"Saved video frames analyzed:      {frame_no}")
    lines.append(f"Visual log lines reconstructed:   {visual_line_count}")
    lines.append(f"OCR text lines:                   {len(texts)}")
    lines.append(f"Outgoing damage events:           {len(outgoing)}")
    lines.append(f"Incoming damage events:           {len(incoming)}")
    lines.append(f"Healing-to-target events:         {len(healing)}")
    lines.append(f"Player attack rolls:              {len(attacks)}")
    lines.append(f"Critical confirmation rolls:      {len(confirms)}")
    lines.append(f"Incoming miss/avoidance events:   {len(defensive_misses)}")
    lines.append(f"DDO focus interruptions:          {len(focus_interruptions)}")
    lines.append(f"DDO focus interruption seconds:   {focus_interruption_seconds:.3f}")
    lines.append("")
    lines.append("A pre-capture baseline is used when available; legacy sessions exclude the initial visible frame.")
    if focus_interruptions:
        lines.append("Frames after DDO-focus interruptions start fresh reconstruction segments; combat while DDO was hidden may be missing.")
    lines.append("Raw OCR lines and line PNGs are retained for verification.")

    report_text = "\n".join(lines) + "\n"
    report_path = session / "COMBAT_REPORT.txt"
    report_path.write_text(report_text, encoding="utf-8")

    # Detailed all events
    events_path = session / "damage_events.csv"
    combined = []
    for e in outgoing:
        combined.append({
            "line": e["line"], "elapsed_seconds": e.get("elapsed_seconds"),
            "event": "damage_dealt", "mode": e["mode"],
            "source": e["source"], "target": e["target"],
            "type": e["damage_type"], "amount": e["amount"],
            "blocked_amount": e.get("blocked_amount", 0),
            "pre_mitigation_amount": e.get("pre_mitigation_amount", e["amount"]),
            "reduction_type": e.get("reduction_type", ""),
            "reduction_class": e.get("reduction_class", ""),
            "ocr_token": e["ocr_token"], "parsed_token": e["parsed_token"],
            "text": e["text"],
        })
    for e in incoming:
        combined.append({
            "line": e["line"], "elapsed_seconds": e.get("elapsed_seconds"),
            "event": "damage_taken", "mode": "",
            "source": e["source"], "target": e["target"],
            "type": e["damage_type"], "amount": e["amount"],
            "blocked_amount": e.get("blocked_amount", 0),
            "pre_mitigation_amount": e.get("pre_mitigation_amount", e["amount"]),
            "reduction_type": e.get("reduction_type", ""),
            "reduction_class": e.get("reduction_class", ""),
            "ocr_token": e["ocr_token"], "parsed_token": e["parsed_token"],
            "text": e["text"],
        })
    for h in healing:
        combined.append({
            "line": h["line"], "elapsed_seconds": h.get("elapsed_seconds"),
            "event": "healing_to_target", "mode": "",
            "source": h["source"], "target": h["target"],
            "type": "healing", "amount": h["amount"],
            "blocked_amount": 0, "pre_mitigation_amount": h["amount"],
            "ocr_token": h["ocr_token"], "parsed_token": h["parsed_token"],
            "text": h["text"],
        })
    combined.sort(key=lambda e: e["line"])

    with events_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                "line", "elapsed_seconds", "event", "mode", "source",
                "target", "type", "amount", "blocked_amount", "pre_mitigation_amount",
                "reduction_type", "reduction_class", "ocr_token", "parsed_token", "text"
            ],
        )
        w.writeheader()
        w.writerows(combined)

    # Attack and critical-confirmation events.
    attack_events_path = session / "attack_events.csv"
    attack_rows = list(attacks) + list(confirms)
    attack_rows.sort(key=lambda e: e["line"])
    with attack_events_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                "line", "elapsed_seconds", "event", "target", "roll", "bonus",
                "total", "outcome", "confirmed", "linked_attack_line", "text"
            ],
        )
        w.writeheader()
        w.writerows(attack_rows)

    crit_path = session / "crit_by_target.csv"
    with crit_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "target", "attacks", "hits", "grazing_hits", "misses", "critical_threats",
            "confirm_attempts", "confirmed_crits", "failed_confirms",
            "grazing_rate_percent", "crit_rate_percent", "crit_on_hits_percent", "threat_rate_percent",
            "confirm_rate_percent", "highest_failed_confirm_total",
            "lowest_successful_confirm_total", "observed_confirm",
            "highest_miss_roll", "lowest_full_hit_roll",
            "highest_miss_total", "lowest_full_hit_total", "observed_hit_total"
        ])
        for target, d in sorted(attack_stats.items(), key=lambda kv: (-kv[1]["attacks"], kv[0].lower())):
            w.writerow([
                target, d["attacks"], d["hits"], d["grazes"], d["misses"], d["threats"],
                d["confirm_attempts"], d["confirmed"], d["failed"],
                f"{d['graze_rate']:.3f}", f"{d['crit_rate']:.3f}", f"{d['crit_on_hits']:.3f}",
                f"{d['threat_rate']:.3f}", f"{d['confirm_rate']:.3f}",
                "" if d["highest_failed"] is None else d["highest_failed"],
                "" if d["lowest_success"] is None else d["lowest_success"],
                d["observed_confirm"],
                "" if d["highest_miss_roll"] is None else d["highest_miss_roll"],
                "" if d["lowest_full_hit_roll"] is None else d["lowest_full_hit_roll"],
                "" if d["highest_miss_total"] is None else d["highest_miss_total"],
                "" if d["lowest_full_hit_total"] is None else d["lowest_full_hit_total"],
                d["observed_hit_total"],
            ])

    apm_path = session / "apm_rolling.csv"
    apm_rows = build_apm_rows(attacks, rolling_window_seconds)
    with apm_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["elapsed_seconds", "capture_elapsed_seconds", "rolling_apm", "window_seconds", "window_attacks"])
        for t, capture_t, apm, count in apm_rows:
            w.writerow([f"{t:.3f}", f"{capture_t:.6f}", f"{apm:.3f}", f"{rolling_window_seconds:g}", count])

    # Configurable rolling DPS time series. t=0 is the first parsed outgoing
    # damage event. DPS is damage in the preceding selected window divided by
    # that window length.
    dps_path = session / "dps_rolling.csv"
    timed_outgoing = [
        e for e in outgoing
        if e.get("elapsed_seconds") is not None
    ]
    timed_outgoing.sort(key=lambda e: float(e["elapsed_seconds"]))

    dps_rows = []
    if timed_outgoing:
        first_t = float(timed_outgoing[0]["elapsed_seconds"])
        last_t = float(timed_outgoing[-1]["elapsed_seconds"])
        rel_events = [
            (float(e["elapsed_seconds"]) - first_t, int(e["amount"]))
            for e in timed_outgoing
        ]

        step = 0.25
        t = 0.0
        left = 0
        right = 0
        rolling_sum = 0

        while t <= (last_t - first_t) + 1e-9:
            while right < len(rel_events) and rel_events[right][0] <= t + 1e-9:
                rolling_sum += rel_events[right][1]
                right += 1

            cutoff = t - rolling_window_seconds
            while left < right and rel_events[left][0] <= cutoff:
                rolling_sum -= rel_events[left][1]
                left += 1

            dps_rows.append((t, first_t + t, rolling_sum / rolling_window_seconds))
            t += step

        # Ensure the exact final event time is represented.
        final_rel = last_t - first_t
        if not dps_rows or abs(dps_rows[-1][0] - final_rel) > 1e-6:
            window_sum = sum(
                amount for et, amount in rel_events
                if final_rel - rolling_window_seconds < et <= final_rel + 1e-9
            )
            dps_rows.append((final_rel, first_t + final_rel, window_sum / rolling_window_seconds))

    with dps_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "elapsed_seconds",
            "capture_elapsed_seconds",
            "rolling_dps",
            "window_seconds",
        ])
        for t, capture_t, dps in dps_rows:
            w.writerow([
                f"{t:.3f}",
                f"{capture_t:.6f}",
                f"{dps:.3f}",
                f"{rolling_window_seconds:g}",
            ])

    target_path = session / "damage_by_target.csv"
    with target_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "target", "regular_damage", "sneak_attack_damage", "total_damage",
            "healing_done_to_target", "net_target_hp_loss", "damage_hits"
        ])
        for target, d in sorted(
            target_stats.items(), key=lambda kv: (-kv[1]["damage"], kv[0].lower())
        ):
            w.writerow([
                target, d["regular"], d["sneak"], d["damage"],
                d["healing"], d["net_hp_loss"], d["hits"]
            ])

    type_path = session / "outgoing_damage_by_type.csv"
    with type_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["damage_type", "regular", "sneak_attack", "total", "hits"])
        for dtype, d in sorted(
            overall_type.items(), key=lambda kv: (-kv[1]["total"], kv[0])
        ):
            w.writerow([dtype, d["regular"], d["sneak"], d["total"], d["hits"]])
        w.writerow(["TOTAL", total_regular, total_sneak, total_dealt, len(outgoing)])

    target_type_path = session / "damage_by_target_and_type.csv"
    with target_type_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["target", "damage_type", "regular", "sneak_attack", "total", "hits"])
        for target, tdata in sorted(target_type.items()):
            for dtype, d in sorted(tdata.items(), key=lambda kv: (-kv[1]["total"], kv[0])):
                w.writerow([
                    target, dtype, d["regular"], d["sneak"], d["total"], d["hits"]
                ])

    correction_path = session / "ocr_numeric_corrections.csv"
    with correction_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(
            f, fieldnames=["line", "field", "original", "corrected", "text"]
        )
        w.writeheader()
        w.writerows(corrections)

    suspicious_path = session / "unparsed_damage_like_lines.csv"
    with suspicious_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["line", "ocr_text"])
        w.writerows(suspicious)

    # Blocking / mitigation detail.
    mitigation_path = session / "mitigation_by_target_and_type.csv"
    mitigation = defaultdict(lambda: {"dealt": 0, "blocked": 0, "hits": 0, "affected_hits": 0})
    for e in outgoing:
        blocked = int(e.get("blocked_amount", 0) or 0)
        if blocked <= 0:
            continue
        reduction = e.get("reduction_type", "") or "Unknown DR"
        rclass = e.get("reduction_class", "") or "Damage reduction"
        key = (e.get("target", "Unknown"), reduction, rclass, e.get("damage_type", "unknown"))
        d = mitigation[key]
        d["dealt"] += int(e.get("amount", 0) or 0)
        d["blocked"] += blocked
        d["hits"] += 1
        if blocked > 0:
            d["affected_hits"] += 1
    with mitigation_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["target", "reduction_type", "reduction_class", "damage_type", "dealt", "blocked", "pre_mitigation", "blocked_percent", "hits", "affected_hits"])
        for (target, reduction, rclass, dtype), d in sorted(
            mitigation.items(),
            key=lambda kv: (-(kv[1]["blocked"]), -(kv[1]["dealt"] + kv[1]["blocked"]), kv[0][0].lower(), kv[0][1].lower(), kv[0][3])
        ):
            pre = d["dealt"] + d["blocked"]
            pct = 100.0 * d["blocked"] / pre if pre else 0.0
            w.writerow([target, reduction, rclass, dtype, d["dealt"], d["blocked"], pre, f"{pct:.3f}", d["hits"], d["affected_hits"]])

    # d20 distribution by target.
    roll_path = session / "attack_roll_distribution.csv"
    roll_stats = defaultdict(lambda: defaultdict(lambda: {"miss": 0, "grazing_hit": 0, "hit": 0, "critical_threat": 0, "other": 0}))
    for a in attacks:
        roll = a.get("roll")
        if roll is None:
            continue
        roll_stats[a.get("target", "Unknown")][int(roll)][a.get("outcome", "other")] += 1
    with roll_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["target", "roll", "miss", "grazing_hit", "hit", "critical_threat", "other", "total"])
        for target in sorted(roll_stats, key=str.lower):
            for roll in sorted(roll_stats[target]):
                d = roll_stats[target][roll]
                total = sum(d.values())
                w.writerow([target, roll, d["miss"], d["grazing_hit"], d["hit"], d["critical_threat"], d["other"], total])

    # Explicit enemy miss/avoidance events and a compact by-source summary.
    defense_events_path = session / "incoming_defense_events.csv"
    with defense_events_path.open("w", newline="", encoding="utf-8") as f:
        fields = ["line", "elapsed_seconds", "event", "source", "target", "reason", "reason_text", "text"]
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader(); w.writerows(defensive_misses)

    defense_by_source_path = session / "incoming_defense_by_source.csv"
    defense_by_source = defaultdict(lambda: {
        "regular_miss": 0, "dodge": 0, "blur": 0, "displacement": 0, "deflect": 0, "other": 0
    })
    for e in defensive_misses:
        defense_by_source[e.get("source", "Unknown")][e.get("reason", "other")] += 1
    direct_hits_by_source = defaultdict(int)
    for e in incoming:
        source = canonical_target(e.get("source", ""))
        if source:
            direct_hits_by_source[source] += 1
    with defense_by_source_path.open("w", newline="", encoding="utf-8") as f:
        fields = ["source", "direct_hits", "regular_miss", "dodge", "blur", "displacement", "deflect", "other", "total_misses", "observed_direct_attacks", "miss_percent"]
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for source, d in sorted(defense_by_source.items(), key=lambda kv: (-sum(kv[1].values()), kv[0].lower())):
            direct_hits = direct_hits_by_source.get(source, 0)
            misses_total = sum(d.values())
            attempts = direct_hits + misses_total
            row = {"source": source, "direct_hits": direct_hits, **d}
            row.update({
                "total_misses": misses_total,
                "observed_direct_attacks": attempts,
                "miss_percent": f"{(100.0 * misses_total / attempts) if attempts else 0.0:.3f}",
            })
            w.writerow(row)

    # One normalized event stream for future statistics/debugging.
    normalized_path = session / "normalized_events.csv"
    norm = []
    for e in outgoing:
        norm.append({
            "elapsed_seconds": e.get("elapsed_seconds"), "kind": "damage_dealt",
            "source": e.get("source", "You"), "target": e.get("target", ""),
            "mode": e.get("mode", ""), "damage_type": e.get("damage_type", ""),
            "amount": e.get("amount", 0), "blocked_amount": e.get("blocked_amount", 0),
            "reduction_type": e.get("reduction_type", ""), "reduction_class": e.get("reduction_class", ""),
            "roll": "", "bonus": "", "total_roll": "", "outcome": "", "line": e.get("line", ""), "text": e.get("text", ""),
        })
    for e in incoming:
        norm.append({
            "elapsed_seconds": e.get("elapsed_seconds"), "kind": "damage_taken",
            "source": e.get("source", ""), "target": "You", "mode": "",
            "damage_type": e.get("damage_type", ""), "amount": e.get("amount", 0),
            "blocked_amount": e.get("blocked_amount", 0), "reduction_type": e.get("reduction_type", ""), "reduction_class": e.get("reduction_class", ""),
            "roll": "", "bonus": "", "total_roll": "", "outcome": "", "line": e.get("line", ""), "text": e.get("text", ""),
        })
    for h in healing:
        norm.append({
            "elapsed_seconds": h.get("elapsed_seconds"), "kind": "healing_to_target",
            "source": "You", "target": h.get("target", ""), "mode": "", "damage_type": "healing",
            "amount": h.get("amount", 0), "blocked_amount": 0, "reduction_type": "", "reduction_class": "",
            "roll": "", "bonus": "", "total_roll": "", "outcome": "", "line": h.get("line", ""), "text": h.get("text", ""),
        })
    for a in attacks:
        norm.append({
            "elapsed_seconds": a.get("elapsed_seconds"), "kind": "attack",
            "source": "You", "target": a.get("target", ""), "mode": "", "damage_type": "",
            "amount": "", "blocked_amount": "", "reduction_type": "", "reduction_class": "",
            "roll": a.get("roll", ""), "bonus": a.get("bonus", ""),
            "total_roll": a.get("total", ""), "outcome": a.get("outcome", ""), "line": a.get("line", ""), "text": a.get("text", ""),
        })
    for c in confirms:
        norm.append({
            "elapsed_seconds": c.get("elapsed_seconds"), "kind": "crit_confirm",
            "source": "You", "target": c.get("target", ""), "mode": "", "damage_type": "",
            "amount": "", "blocked_amount": "", "reduction_type": "", "reduction_class": "",
            "roll": c.get("roll", ""), "bonus": c.get("bonus", ""),
            "total_roll": c.get("total", ""), "outcome": c.get("outcome", ""), "line": c.get("line", ""), "text": c.get("text", ""),
        })
    for e in defensive_misses:
        norm.append({
            "elapsed_seconds": e.get("elapsed_seconds"), "kind": "incoming_miss",
            "source": e.get("source", ""), "target": "You", "mode": "", "damage_type": "",
            "amount": "", "blocked_amount": "", "reduction_type": "", "reduction_class": "",
            "roll": "", "bonus": "", "total_roll": "", "outcome": e.get("reason", "other"),
            "line": e.get("line", ""), "text": e.get("text", ""),
        })
    norm.sort(key=lambda r: (float(r["elapsed_seconds"]) if r.get("elapsed_seconds") is not None else 1e99, int(r.get("line") or 0)))
    with normalized_path.open("w", newline="", encoding="utf-8") as f:
        fields = ["elapsed_seconds", "kind", "source", "target", "mode", "damage_type", "amount", "blocked_amount", "reduction_type", "reduction_class", "roll", "bonus", "total_roll", "outcome", "line", "text"]
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader(); w.writerows(norm)

    # Compact machine-readable summary, also consumed by the background overlay.
    live_event_count = csv_row_count(session / "live_damage_events.csv")
    live_corrections = 0
    try:
        with (session / "live_damage_events.csv").open("r", encoding="utf-8", newline="") as f:
            live_corrections = sum(1 for r in csv.DictReader(f) if str(r.get("correction_applied", "")).strip())
    except Exception:
        pass
    summary = {
        "total_damage": total_dealt,
        "regular_damage": total_regular,
        "sneak_damage": total_sneak,
        "sneak_hits": sneak_hits,
        "sneak_share_percent": sneak_share,
        "average_sneak_damage": sneak_avg,
        "sneak_attacks_per_minute": sneak_per_min,
        "blocked_outgoing": total_blocked_out,
        "blocked_incoming": total_blocked_in,
        "recording_duration_seconds": duration,
        "recording_dps": recording_dps,
        # Keep elapsed_dps for backwards compatibility with older GUI builds;
        # its historical meaning is recording-duration DPS.
        "elapsed_dps": recording_dps,
        "session_duration_seconds": session_duration,
        "session_damage_span_seconds": damage_timing.get("damage_span_seconds"),
        "session_typical_batch_interval_seconds": damage_timing.get("typical_batch_interval_seconds"),
        "session_damage_batches": damage_timing.get("batch_count", 0),
        "session_dps": session_dps,
        "active_combat_seconds": active_seconds,
        "active_combat_dps": active_dps,
        "inactivity_timeout_seconds": inactivity_timeout,
        "rolling_window_seconds": rolling_window_seconds,
        "data_quality": {
            "visual_lines_reconstructed": visual_line_count,
            "ocr_text_lines": len(texts),
            "post_damage_events": len(outgoing),
            "live_damage_events": live_event_count,
            "post_numeric_corrections": len(corrections),
            "live_numeric_corrections": live_corrections,
            "suspicious_unparsed_damage_lines": len(suspicious),
            "event_count_agreement": (live_event_count == len(outgoing)) if live_event_count else None,
            "focus_interruptions": len(focus_interruptions),
            "focus_interruption_seconds": focus_interruption_seconds,
        },
    }
    (session / "analysis_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")

    return {
        "report_text": report_text,
        "report_path": report_path,
        "events_path": events_path,
        "dps_path": dps_path,
        "target_path": target_path,
        "type_path": type_path,
        "target_type_path": target_type_path,
        "correction_path": correction_path,
        "suspicious_path": suspicious_path,
        "total_dealt": total_dealt,
        "total_taken": total_taken,
    }


# ---------------------------------------------------------------------------
# Post-analysis timing log
# ---------------------------------------------------------------------------

def write_post_analysis_timings(session, timings, total_seconds, details=None):
    details = details or {}
    path = session / "post_analysis_timings.csv"
    accounted = sum(seconds for _, seconds in timings)
    residual = max(0.0, total_seconds - accounted)
    rows = list(timings)
    if residual > 0.000001:
        rows.append(("other_overhead", residual))
        details.setdefault("other_overhead", "progress output, small file I/O and Python overhead")
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["phase", "seconds", "percent_of_total", "details"])
        for phase, seconds in rows:
            pct = (seconds / total_seconds * 100.0) if total_seconds > 0 else 0.0
            w.writerow([phase, f"{seconds:.6f}", f"{pct:.2f}", details.get(phase, "")])
        w.writerow(["TOTAL", f"{total_seconds:.6f}", "100.00", details.get("TOTAL", "")])


# ---------------------------------------------------------------------------
# Main video -> lines -> OCR -> parse workflow
# ---------------------------------------------------------------------------

def main():
    total_t0 = time.perf_counter()
    timings = []
    timing_details = {}
    session = locate_session()
    if not session:
        print("No combat recording found.")
        raise SystemExit(1)

    setup_t0 = time.perf_counter()

    cfg_path = session / "config_used.json"
    if not cfg_path.exists():
        cfg_path = CONFIG_PATH
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))

    # Keep the recording-time capture geometry from the session, but let the
    # current GUI line-height choice control post-analysis and re-analysis.
    try:
        current_cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        current_analysis = current_cfg.get("analysis", {})
        session_analysis = cfg.setdefault("analysis", {})
        for key in (
            "auto_line_height",
            "line_height",
            "line_height_mode",
            "last_detected_line_height",
            "line_height_min",
            "line_height_max",
        ):
            if key in current_analysis:
                session_analysis[key] = current_analysis[key]
        line_height_settings_source = "current_config"
    except Exception:
        line_height_settings_source = "session_config_fallback"

    if indra_ocr is None:
        print("Indra OCR is not installed or could not be imported.")
        print("Run SETUP DDO Combat Analyzer.sh.")
        raise SystemExit(2)

    ffmpeg = find_ffmpeg(str(cfg.get("ffmpeg_path", "")).strip())
    if not ffmpeg:
        print("FFmpeg was not found.")
        raise SystemExit(3)

    video = session / "combat_changed_gray_ffv1.mkv"

    # saved_frame -> capture elapsed time. These timestamps let reconstructed
    # Combat Log lines retain their approximate original event time.
    saved_frame_times = {}
    total_saved_frames = 0
    capture_index = session / "capture_index.csv"
    if capture_index.exists():
        try:
            with capture_index.open("r", encoding="utf-8", newline="") as f:
                for row in csv.DictReader(f):
                    if str(row.get("saved", "")).strip() == "1":
                        total_saved_frames += 1
                        try:
                            sf = int(row.get("saved_frame", "") or 0)
                            et = float(row.get("elapsed_seconds", "") or 0.0)
                            if sf > 0:
                                saved_frame_times[sf] = et
                        except Exception:
                            pass
        except Exception:
            total_saved_frames = 0
            saved_frame_times = {}

    focus_interruptions = []
    focus_resume_frames = set()
    focus_interruption_seconds = 0.0
    focus_path = session / "focus_interruptions.csv"
    if focus_path.exists():
        try:
            with focus_path.open("r", encoding="utf-8", newline="") as f:
                for row in csv.DictReader(f):
                    try:
                        duration_focus = float(row.get("duration_seconds", "") or 0.0)
                    except Exception:
                        duration_focus = 0.0
                    focus_interruption_seconds += max(0.0, duration_focus)
                    focus_interruptions.append(row)
                    try:
                        resume_frame = int(row.get("resume_saved_frame", "") or 0)
                        if resume_frame > 0:
                            focus_resume_frames.add(resume_frame)
                    except Exception:
                        pass
        except Exception:
            focus_interruptions = []
            focus_resume_frames = set()
            focus_interruption_seconds = 0.0

    timings.append(("setup_and_input_discovery", time.perf_counter() - setup_t0))
    timing_details["setup_and_input_discovery"] = f"saved_frames={total_saved_frames}; focus_interruptions={len(focus_interruptions)}"

    print("PROGRESS|PREPARE|1|1", flush=True)

    width = int(cfg["capture"]["width"])
    height = int(cfg["capture"]["height"])
    frame_bytes = width * height
    acfg = cfg.setdefault("analysis", {})
    auto_line_height = bool(acfg.get("auto_line_height", True))
    configured_line_height = int(acfg.get("line_height", 16))
    if auto_line_height:
        # A previous good Auto result is a better fallback than a generic value.
        try:
            configured_line_height = int(acfg.get("last_detected_line_height", configured_line_height))
        except Exception:
            pass
    line_height = configured_line_height

    lineheight_t0 = time.perf_counter()

    # Pre-scan a handful of saved frames and choose the strongest confident
    # periodic text-spacing measurement. Manual mode skips this phase entirely.
    if auto_line_height:
        probe_cmd = [
            ffmpeg, "-hide_banner", "-loglevel", "error",
            "-i", str(video),
            "-frames:v", "12",
            "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1",
        ]
        probe = subprocess.Popen(
            probe_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
        )
        lh_votes = []
        for _ in range(12):
            pdata = probe.stdout.read(frame_bytes)
            if len(pdata) < frame_bytes:
                break
            pgray = np.frombuffer(pdata, dtype=np.uint8).reshape((height, width)).copy()
            dh, ds, dm, dok = detect_line_height_from_gray(
                pgray, configured_line_height,
                int(acfg.get("line_height_min", 10)),
                int(acfg.get("line_height_max", 22)),
            )
            if dok:
                lh_votes.append((ds + dm, dh, ds, dm))
        try:
            probe.stdout.close()
        except Exception:
            pass
        try:
            probe.wait(timeout=2)
        except Exception:
            try:
                probe.kill()
            except Exception:
                pass
        if lh_votes:
            # Prefer the most frequently detected height; require repeated
            # agreement before overriding the configured fallback.
            counts = {}
            for _, h, _, _ in lh_votes:
                counts[h] = counts.get(h, 0) + 1
            best_count = max(counts.values())
            if best_count >= 3:
                contenders = {h for h, c in counts.items() if c == best_count}
                best_vote = max((v for v in lh_votes if v[1] in contenders), key=lambda x: x[0])
                line_height = int(best_vote[1])
    acfg["line_height"] = int(line_height)
    if auto_line_height:
        acfg["last_detected_line_height"] = int(line_height)
        # Persist the last successful Auto result for the next run, while
        # preserving all other user settings. The GUI disables this setting
        # during analysis, so there is no competing write from the UI.
        try:
            disk_cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            disk_analysis = disk_cfg.setdefault("analysis", {})
            disk_analysis["last_detected_line_height"] = int(line_height)
            disk_analysis["line_height"] = int(line_height)
            tmp_cfg = CONFIG_PATH.with_name(CONFIG_PATH.name + ".tmp")
            tmp_cfg.write_text(json.dumps(disk_cfg, indent=2), encoding="utf-8")
            tmp_cfg.replace(CONFIG_PATH)
        except Exception:
            pass
    try:
        (session / "line_height_used.json").write_text(
            json.dumps({
                "configured": configured_line_height,
                "used": line_height,
                "auto": auto_line_height,
                "mode": "auto" if auto_line_height else "manual",
            }, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass
    timings.append(("line_height_detection", time.perf_counter() - lineheight_t0))
    timing_details["line_height_detection"] = (
        f"mode={'auto' if auto_line_height else 'manual'}; "
        f"configured={configured_line_height}; used={line_height}; "
        f"probe={'used' if auto_line_height else 'skipped'}; "
        f"settings_source={line_height_settings_source}"
    )

    print()
    print("DDO COMBAT ANALYZER - ANALYSIS")
    print("-------------------------------")
    print(f"Session: {session.name}")
    print("Reconstructing visual log lines...")

    reconstruction_t0 = time.perf_counter()
    # Detailed reconstruction profiling.  These accumulators intentionally
    # cover the full reconstruction phase without changing its behaviour.
    recon_profile = {
        "video_decode_read": 0.0,
        "frame_preprocessing": 0.0,
        "whole_frame_alignment": 0.0,
        "grid_phase_detection": 0.0,
        "linewise_alignment": 0.0,
        "scroll_decision_and_line_extraction": 0.0,
        "line_png_encode_write": 0.0,
        "reconstruction_logging_progress": 0.0,
    }

    cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error",
        "-i", str(video),
        "-f", "rawvideo", "-pix_fmt", "gray", "pipe:1",
    ]
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
    )

    # Per-line PNGs are a diagnostic artifact only; OCR consumes visual_lines
    # directly from RAM.  Keeping them disabled avoids thousands of pointless
    # filesystem writes followed by deletion, with no effect on OCR quality.
    write_debug_line_images = bool(
        cfg.get("analysis", {}).get("write_debug_line_images", False)
    )
    line_dir = session / "line_images"
    if write_debug_line_images:
        line_dir.mkdir(exist_ok=True)

    align_path = session / "alignment.csv"
    align_f = align_path.open("w", newline="", encoding="utf-8")
    align_w = csv.writer(align_f)
    align_w.writerow([
        "saved_frame", "shift_px", "new_lines",
        "alignment_score", "zero_score", "decision", "grid_phase",
        "whole_frame_confident", "whole_frame_margin", "whole_frame_second_score",
        "linewise_ran", "linewise_shift_px", "linewise_score", "linewise_second_score"
    ])

    prev_feat = None
    prev_gray = None
    prev_phase = None
    baseline_used = False
    baseline_path = session / "baseline_gray.png"
    if baseline_path.exists():
        try:
            bdata = np.fromfile(str(baseline_path), dtype=np.uint8)
            bgray = cv2.imdecode(bdata, cv2.IMREAD_GRAYSCALE)
            if bgray is not None and bgray.shape == (height, width):
                prev_gray = bgray
                prev_feat = text_feature(bgray)
                prev_phase = best_grid_phase(bgray, line_height, feat=prev_feat)
                baseline_used = True
        except Exception:
            baseline_used = False
            prev_feat = None
            prev_gray = None
            prev_phase = None
    visual_lines = []
    visual_line_meta = []  # (source_saved_frame, elapsed_seconds)
    frame_no = 0
    last_phase = prev_phase if baseline_used else None
    adaptive_linewise = bool(cfg.get("analysis", {}).get("adaptive_linewise", True))
    linewise_run_count = 0
    linewise_skip_count = 0
    linewise_override_count = 0
    whole_confident_count = 0
    zero_first_fast_count = 0
    zero_first_full_search_count = 0

    # A text row can be clipped by the lower capture boundary.  Keep at most
    # one such newest row pending and recover it from the next scrolling frame,
    # when it has moved up by `shift` pixels and is fully visible.
    pending_bottom_line = None
    deferred_bottom_count = 0
    recovered_bottom_count = 0
    dropped_bottom_count = 0
    eof_flushed_bottom_count = 0
    eof_partial_line_indices = set()  # 0-based visual-line indices

    while True:
        _t = time.perf_counter()
        data = proc.stdout.read(frame_bytes)
        recon_profile["video_decode_read"] += time.perf_counter() - _t
        if len(data) < frame_bytes:
            break

        frame_no += 1
        _t = time.perf_counter()
        gray = np.frombuffer(data, dtype=np.uint8).reshape((height, width)).copy()
        feat = text_feature(gray)
        recon_profile["frame_preprocessing"] += time.perf_counter() - _t

        if frame_no in focus_resume_frames:
            # A pending clipped row cannot be tracked safely across a focus
            # interruption because intervening Combat Log motion was not
            # recorded.
            if pending_bottom_line is not None:
                dropped_bottom_count += 1
                pending_bottom_line = None
            # DDO was not foreground before this saved frame. Pixels shown by
            # other applications were intentionally not captured, so this frame
            # starts a fresh visual segment. Do not compare it with the pre-pause
            # Combat Log and accidentally invent a large scroll.
            _t = time.perf_counter()
            phase = best_grid_phase(gray, line_height, feat=feat)
            recon_profile["grid_phase_detection"] += time.perf_counter() - _t
            last_phase = phase
            _t = time.perf_counter()
            align_w.writerow([
                frame_no, 0, 0, 1.0, 1.0, "focus-resume baseline", phase,
                True, 1.0, 0.0, False, "", "", ""
            ])
            recon_profile["reconstruction_logging_progress"] += time.perf_counter() - _t
            prev_feat = feat
            prev_gray = gray
            prev_phase = phase
            continue

        if prev_feat is None:
            _t = time.perf_counter()
            phase = best_grid_phase(gray, line_height, feat=feat)
            recon_profile["grid_phase_detection"] += time.perf_counter() - _t
            last_phase = phase
            _t = time.perf_counter()
            align_w.writerow([
                frame_no, 0, 0, 1.0, 1.0, "initial excluded (legacy session)", phase,
                True, 1.0, 0.0, False, "", "", ""
            ])
            recon_profile["reconstruction_logging_progress"] += time.perf_counter() - _t
            prev_feat = feat
            prev_gray = gray
            prev_phase = phase
            continue

        _t = time.perf_counter()
        (
            shift, score, zero, decision,
            whole_confident, whole_margin, whole_second_score,
            zero_fast_path,
        ) = estimate_shift(prev_feat, feat, cfg)
        recon_profile["whole_frame_alignment"] += time.perf_counter() - _t
        if whole_confident:
            whole_confident_count += 1
        if zero_fast_path:
            zero_first_fast_count += 1
        else:
            zero_first_full_search_count += 1

        _t = time.perf_counter()
        phase_relocked = False
        # Manual line-height mode normally reuses the established phase for
        # speed, but an uncertain whole-frame match is exactly where the DDO
        # log may have transitioned from append-only to scrolling. Recheck the
        # phase on those frames so manual mode cannot get stuck on a sparse
        # baseline either.
        should_measure_phase = bool(
            auto_line_height or last_phase is None or not whole_confident
        )
        if not should_measure_phase:
            phase = last_phase
        else:
            phase_now, phase_now_score, _phase_second = best_grid_phase_with_score(
                gray, line_height, feat=feat
            )
            if last_phase is None:
                phase = phase_now
            else:
                d = min(
                    (phase_now - last_phase) % line_height,
                    (last_phase - phase_now) % line_height,
                )
                if d <= 2:
                    phase = phase_now
                else:
                    # A partially filled DDO Combat Log can change its row
                    # phase abruptly when it becomes full and begins scrolling.
                    # Accept a larger jump only when the current frame provides
                    # overwhelming visual evidence that the new phase is better.
                    old_phase_score = grid_phase_score(gray, line_height, last_phase, feat=feat)
                    abs_gain = old_phase_score - phase_now_score
                    rel_gain = (
                        phase_now_score / old_phase_score
                        if old_phase_score > 1e-9 else 1.0
                    )
                    strong_relock = bool(
                        phase_now_score < old_phase_score
                        and abs_gain >= 0.020
                        and rel_gain <= 0.60
                    )
                    phase = phase_now if strong_relock else last_phase
                    phase_relocked = strong_relock
            last_phase = phase
        recon_profile["grid_phase_detection"] += time.perf_counter() - _t

        # Conservative line-by-line verification.  With adaptive verification
        # enabled, skip it only when whole-frame matching is exceptionally
        # unambiguous.  Any uncertain whole-frame result still takes the exact
        # same expensive fallback path as before.
        linewise_ran = False
        lw_shift = None
        lw_score = None
        lw_second = None
        lw_confident = False
        if (not adaptive_linewise) or (not whole_confident):
            max_shift_px = min(
                int(cfg.get("analysis", {}).get("max_scroll_pixels", 384)),
                gray.shape[0] - 48,
            )
            _t = time.perf_counter()
            lw_shift, lw_score, lw_second, lw_confident = estimate_shift_linewise(
                prev_gray,
                gray,
                prev_phase,
                phase,
                line_height,
                max_shift_px,
            )
            recon_profile["linewise_alignment"] += time.perf_counter() - _t
            linewise_ran = True
            linewise_run_count += 1
        else:
            linewise_skip_count += 1

        _t = time.perf_counter()
        if phase_relocked:
            decision = decision + "+phase-relock"
        if linewise_ran and lw_confident and lw_shift != shift:
            shift = lw_shift
            decision = decision + "+linewise"
            linewise_override_count += 1
        crop_entries = []  # (crop, source_frame, source_elapsed_seconds)

        # Recover the bottom row deferred from an earlier frame.  When the log
        # scrolls by `shift`, the exact same row moves upward by that many
        # pixels and becomes a normal full-height cell.  Preserve its original
        # source timestamp so DPS timing is not shifted to the recovery frame.
        if pending_bottom_line is not None and shift > 0:
            recovered_y0 = int(pending_bottom_line["y0"] - shift)
            recovered_y1 = recovered_y0 + int(line_height)
            if 0 <= recovered_y0 and recovered_y1 <= gray.shape[0]:
                recovered_crop = gray[recovered_y0:recovered_y1].copy()
                if float(np.mean(feat[recovered_y0:recovered_y1])) >= 0.006:
                    crop_entries.append((
                        recovered_crop,
                        pending_bottom_line["frame_no"],
                        pending_bottom_line["elapsed_seconds"],
                    ))
                    recovered_bottom_count += 1
                    decision = decision + "+recover-bottom"
                else:
                    dropped_bottom_count += 1
                pending_bottom_line = None
            else:
                # The row moved outside the capture before a full-height copy
                # could be recovered. This should be extremely rare with the
                # normal DDO scroll rate, but never OCR the clipped fallback.
                dropped_bottom_count += 1
                pending_bottom_line = None

        crops, deferred_y0 = crop_new_lines(
            gray, shift, phase, line_height, feat=feat
        )
        for crop in crops:
            crop_entries.append((
                crop,
                frame_no,
                saved_frame_times.get(frame_no),
            ))

        if deferred_y0 is not None:
            pending_bottom_line = {
                "y0": int(deferred_y0),
                "frame_no": frame_no,
                "elapsed_seconds": saved_frame_times.get(frame_no),
            }
            deferred_bottom_count += 1
            decision = decision + "+defer-bottom"

        if not crop_entries and shift == 0:
            append_crops = crop_blank_to_filled_lines(
                prev_feat, gray, feat, phase, line_height
            )
            if append_crops:
                for crop in append_crops:
                    crop_entries.append((
                        crop,
                        frame_no,
                        saved_frame_times.get(frame_no),
                    ))
                decision = decision + "+append"
        recon_profile["scroll_decision_and_line_extraction"] += time.perf_counter() - _t

        _t = time.perf_counter()
        for crop, source_frame_no, source_elapsed_seconds in crop_entries:
            idx = len(visual_lines) + 1
            visual_lines.append(crop)
            visual_line_meta.append((
                source_frame_no,
                source_elapsed_seconds,
            ))
            if write_debug_line_images:
                ok, enc = cv2.imencode(".png", crop)
                if ok:
                    enc.tofile(str(line_dir / f"{idx:06d}.png"))
        recon_profile["line_png_encode_write"] += time.perf_counter() - _t

        _t = time.perf_counter()
        align_w.writerow([
            frame_no, shift, len(crop_entries),
            f"{score:.5f}", f"{zero:.5f}", decision, phase,
            whole_confident,
            "" if whole_margin is None else f"{whole_margin:.5f}",
            "" if whole_second_score is None else f"{whole_second_score:.5f}",
            linewise_ran,
            "" if lw_shift is None else lw_shift,
            "" if lw_score is None else f"{lw_score:.5f}",
            "" if lw_second is None else f"{lw_second:.5f}",
        ])
        prev_feat = feat
        prev_gray = gray
        prev_phase = phase

        if frame_no % 5 == 0 or (total_saved_frames and frame_no == total_saved_frames):
            total_for_progress = total_saved_frames or max(frame_no, 1)
            print(
                f"PROGRESS|RECONSTRUCT|{frame_no}|{total_for_progress}|{len(visual_lines)}",
                flush=True,
            )
        recon_profile["reconstruction_logging_progress"] += time.perf_counter() - _t

    # EOF edge case: the newest row may still be clipped by the lower capture
    # boundary.  It can never be recovered from a later scrolling frame because
    # recording has ended.  OCR this one pending fragment as a marked fallback;
    # only this marked row receives the narrow damaqe->damage repair later.
    if pending_bottom_line is not None and prev_gray is not None and prev_feat is not None:
        y0 = int(pending_bottom_line.get("y0", prev_gray.shape[0]))
        y1 = int(prev_gray.shape[0])
        partial_h = y1 - y0
        min_partial_h = max(8, int(math.ceil(float(line_height) * 0.70)))
        if 0 <= y0 < y1 and min_partial_h <= partial_h < int(line_height):
            level = float(np.mean(prev_feat[y0:y1]))
            if level >= 0.006:
                eof_crop = prev_gray[y0:y1].copy()
                eof_idx = len(visual_lines)
                visual_lines.append(eof_crop)
                visual_line_meta.append((
                    pending_bottom_line.get("frame_no"),
                    pending_bottom_line.get("elapsed_seconds"),
                ))
                eof_partial_line_indices.add(eof_idx)
                eof_flushed_bottom_count += 1
                _t = time.perf_counter()
                if write_debug_line_images:
                    ok, enc = cv2.imencode(".png", eof_crop)
                    if ok:
                        enc.tofile(str(line_dir / f"{eof_idx + 1:06d}.png"))
                recon_profile["line_png_encode_write"] += time.perf_counter() - _t
        pending_bottom_line = None

    proc.stdout.close()
    stderr = proc.stderr.read().decode("utf-8", errors="replace")
    proc.stderr.close()
    proc.wait()
    align_f.close()

    if proc.returncode != 0:
        print(stderr)
        raise SystemExit(4)

    reconstruction_total = time.perf_counter() - reconstruction_t0
    measured_recon = sum(recon_profile.values())
    recon_profile["reconstruction_setup_teardown_overhead"] = max(0.0, reconstruction_total - measured_recon)

    for phase_name in (
        "video_decode_read",
        "frame_preprocessing",
        "whole_frame_alignment",
        "grid_phase_detection",
        "linewise_alignment",
        "scroll_decision_and_line_extraction",
        "line_png_encode_write",
        "reconstruction_logging_progress",
        "reconstruction_setup_teardown_overhead",
    ):
        seconds = recon_profile[phase_name]
        timings.append((phase_name, seconds))
        avg_ms = (seconds / frame_no * 1000.0) if frame_no else 0.0
        timing_details[phase_name] = f"frames={frame_no}; avg_ms_per_frame={avg_ms:.3f}"

    timing_details["whole_frame_alignment"] += (
        f"; zero_first={'on' if cfg.get('analysis', {}).get('zero_first_enabled', True) else 'off'}"
        f"; threshold={float(cfg.get('analysis', {}).get('zero_first_threshold', 0.995)):.5f}"
        f"; fast_hits={zero_first_fast_count}; full_searches={zero_first_full_search_count}"
    )
    timing_details["linewise_alignment"] += (
        f"; adaptive={'on' if adaptive_linewise else 'off'}"
        f"; ran={linewise_run_count}; skipped={linewise_skip_count}"
        f"; overrides={linewise_override_count}"
        f"; whole_confident={whole_confident_count}"
    )
    timing_details["grid_phase_detection"] += (
        "; phase_mode=fixed-with-uncertain-frame-recheck" if not auto_line_height
        else "; phase_mode=dynamic"
    )
    timing_details["line_png_encode_write"] += (
        f"; debug_png={'on' if write_debug_line_images else 'off'}"
        f"; visual_lines={len(visual_lines)}"
        f"; bottom_deferred={deferred_bottom_count}"
        f"; bottom_recovered={recovered_bottom_count}"
        f"; bottom_dropped={dropped_bottom_count}"
        f"; bottom_eof_flushed={eof_flushed_bottom_count}"
        f"; bottom_pending_at_end={1 if pending_bottom_line is not None else 0}"
    )
    timing_details["reconstruction_setup_teardown_overhead"] += f"; reconstruction_total={reconstruction_total:.6f}s"

    print(f"Visual reconstruction complete: {len(visual_lines)} new lines.")
    print("Running Indra post-analysis OCR...")

    thread_setting = cfg.get("live_dps", {}).get("indra_threads", "auto")
    threads = resolve_indra_threads(thread_setting)
    batch_max = int(cfg.get("live_dps", {}).get("ocr_batch_max_lines", 12))
    batch_max = max(1, min(32, batch_max))

    model_load_t0 = time.perf_counter()
    try:
        converter = indra_ocr.load(threads=threads, mode="ocr")
    except Exception as exc:
        print(f"Indra OCR could not be loaded: {type(exc).__name__}: {exc}")
        raise SystemExit(5)
    timings.append(("indra_model_load", time.perf_counter() - model_load_t0))
    timing_details["indra_model_load"] = (
        f"threads={threads}; setting={thread_setting}; physical_cores={physical_core_count()}; batch_max={batch_max}"
    )

    ocr_t0 = time.perf_counter()
    texts = [""] * len(visual_lines)
    line_times = [meta[1] for meta in visual_line_meta]
    for start_i in range(0, len(visual_lines), batch_max):
        batch = visual_lines[start_i:start_i + batch_max]
        batch_texts = ocr_indra_batch(converter, batch)
        texts[start_i:start_i + len(batch_texts)] = batch_texts
        done = min(start_i + len(batch), len(visual_lines))
        if done % (batch_max * 2) == 0 or done == len(visual_lines):
            print(f"PROGRESS|OCR|{done}|{max(1, len(visual_lines))}", flush=True)

    timings.append(("indra_ocr", time.perf_counter() - ocr_t0))
    timing_details["indra_ocr"] = f"visual_lines={len(visual_lines)}; batch_max={batch_max}"

    log_write_t0 = time.perf_counter()
    log_path = session / "combat_log.txt"
    log_path.write_text("\n".join(texts) + "\n", encoding="utf-8")
    timings.append(("combat_log_write", time.perf_counter() - log_write_t0))
    timing_details["combat_log_write"] = f"ocr_lines={len(texts)}"

    print("PROGRESS|PARSE|1|1", flush=True)
    # Preserve the validated raw-row damage path, but recover the one safe
    # wrap form DDO commonly uses for long sneak/damage messages.  This avoids
    # the earlier regression where generic message reassembly merged unrelated
    # damage rows.
    damage_parse_t0 = time.perf_counter()
    damage_source_texts = list(texts)
    if eof_partial_line_indices:
        prior_texts = [t for i, t in enumerate(texts) if i not in eof_partial_line_indices]
        for eof_idx in sorted(eof_partial_line_indices):
            if 0 <= eof_idx < len(damage_source_texts):
                damage_source_texts[eof_idx] = normalize_eof_partial_damage_row(
                    damage_source_texts[eof_idx], prior_texts
                )
    damage_texts, damage_times, damage_line_numbers = reassemble_wrapped_damage_rows(
        damage_source_texts, line_times
    )
    outgoing, incoming, healing, corrections, suspicious = parse_combat_events(
        damage_texts, damage_times, damage_line_numbers
    )
    timings.append(("damage_healing_parse", time.perf_counter() - damage_parse_t0))
    timing_details["damage_healing_parse"] = (
        f"outgoing={len(outgoing)}; incoming={len(incoming)}; healing={len(healing)}; "
        f"suspicious={len(suspicious)}; eof_partial_rows={len(eof_partial_line_indices)}"
    )

    attack_parse_t0 = time.perf_counter()
    logical_texts, logical_times, logical_line_numbers = reassemble_wrapped_combat_messages(
        texts, line_times
    )
    attacks, confirms = parse_attack_crit_events(
        logical_texts, logical_times, logical_line_numbers
    )
    # Enemy miss/avoidance messages are normally one visual row. Parse them
    # from raw OCR rows rather than generic message reassembly, because a noisy
    # Combat prefix on the following row can otherwise be mistaken for a wrap.
    defensive_misses = parse_incoming_defense_events(
        texts, line_times, list(range(1, len(texts) + 1))
    )
    timings.append(("attack_crit_parse", time.perf_counter() - attack_parse_t0))
    timing_details["attack_crit_parse"] = (
        f"attacks={len(attacks)}; confirms={len(confirms)}; defensive_misses={len(defensive_misses)}"
    )

    report_t0 = time.perf_counter()
    result = create_reports(
        session, texts, outgoing, incoming, healing, attacks, confirms, defensive_misses,
        corrections, suspicious, frame_no, len(visual_lines)
    )
    timings.append(("report_and_graph_generation", time.perf_counter() - report_t0))
    timing_details["report_and_graph_generation"] = "CSV/TXT reports and DPS graph data"

    cleanup_t0 = time.perf_counter()
    # Keep the share build clean: remove post-analysis diagnostic artifacts.
    for debug_name in [
        "alignment.csv",
        "ocr_lines.csv",
        "ocr_numeric_corrections.csv",
        "unparsed_damage_like_lines.csv",
    ]:
        try:
            (session / debug_name).unlink(missing_ok=True)
        except Exception:
            pass
    try:
        shutil.rmtree(session / "line_images", ignore_errors=True)
    except Exception:
        pass

    timings.append(("cleanup", time.perf_counter() - cleanup_t0))
    total_seconds = time.perf_counter() - total_t0
    timing_details["TOTAL"] = f"session={session.name}"
    write_post_analysis_timings(session, timings, total_seconds, timing_details)

    print("PROGRESS|DONE|1|1", flush=True)
    print("Analysis complete.", flush=True)


if __name__ == "__main__":
    main()
