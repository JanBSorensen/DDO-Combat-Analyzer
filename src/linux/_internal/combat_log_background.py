"""Low-cost, *non-blocking* checks for a black, opaque DDO Combat Log.

The screen image cannot reveal the DDO alpha setting directly; this only
checks whether visible non-text pixels appear nearly black. The classifier is
conservative: artwork without a credible left text column is *unknown*, not a
bad background. It never alters frame visibility, OCR, or damage parsing.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import json
import os
import threading
import time

import numpy as np

WARNING_TEXT = (
    'Combat Log background does not appear solid black. Set DDO Combat Log '
    'to an opaque BLACK background. Damage/DPS and Reanalyze/Replay Live may '
    'be incomplete or zero. Recording can continue.'
)


@dataclass(frozen=True)
class BackgroundSample:
    verdict: str  # black, nonblack, unknown
    dark_fraction: float
    midtone_fraction: float
    ink_fraction: float
    left_ink_fraction: float

    def as_dict(self):
        return asdict(self)


def classify_background(gray):
    """Test only a subsample (~1/16 pixels) and require a text signature.

    Text is deliberately allowed to occupy some of the black area. Samples
    with no detectable Combat Log column are inconclusive: a loading screen
    must not trigger a 'change your background' warning.
    """
    if gray is None or getattr(gray, 'ndim', None) != 2 or not gray.size:
        return BackgroundSample('unknown', 0.0, 0.0, 0.0, 0.0)
    thumb = gray[::4, ::4]
    left_cols = max(2, min(24, thumb.shape[1]))
    dark = float(np.mean(thumb <= 35))
    mid = float(np.mean((thumb >= 36) & (thumb <= 155)))
    ink = float(np.mean(thumb >= 95))
    left = float(np.mean(thumb[:, :left_cols] >= 95))
    # A flat gray/loading screen is bright everywhere, not a left-aligned
    # text column. Require the characteristic left-weighted text signature.
    if ink < 0.035 or left < 0.075 or left <= ink * 1.18 or left > 0.80:
        verdict = 'unknown'
    elif dark >= 0.80 and mid <= 0.09:
        verdict = 'black'
    elif mid >= 0.13 or dark < 0.76:
        verdict = 'nonblack'
    else:
        verdict = 'unknown'  # gray border or transitional visual; no guessing
    return BackgroundSample(verdict, round(dark, 5), round(mid, 5),
                            round(ink, 5), round(left, 5))


class BackgroundMonitor:
    """Require sustained evidence to warn/clear; sample at most every 2s."""

    def __init__(self, sample_seconds=2.0, confirm_samples=3):
        self.sample_seconds = max(0.1, float(sample_seconds))
        self.confirm_samples = max(2, int(confirm_samples))
        self.next_sample_at = None
        self.state = 'unknown'
        self.bad_streak = 0
        self.good_streak = 0
        self.last_sample = None
        self.bad_samples = 0
        self.good_samples = 0
        self.unknown_samples = 0
        self.ever_warned = False

    def reset(self):
        self.__init__(self.sample_seconds, self.confirm_samples)

    def inspect(self, gray, now=None):
        """Returns (transition or None, sample); neutral samples do not clear warnings.

        Caller must only inspect when DDO is foreground. This doesn't change
        CombatLogVisibilityGuard state or feed OCR.
        """
        now = time.monotonic() if now is None else float(now)
        if self.next_sample_at is not None and now < self.next_sample_at:
            return None, None
        self.next_sample_at = now + self.sample_seconds
        sample = classify_background(gray)
        self.last_sample = sample
        if sample.verdict == 'unknown':
            self.unknown_samples += 1
            self.bad_streak = self.good_streak = 0
            return None, sample
        if sample.verdict == 'nonblack':
            self.bad_samples += 1
            self.bad_streak += 1
            self.good_streak = 0
            if self.bad_streak >= self.confirm_samples and self.state != 'warning':
                self.state = 'warning'
                self.ever_warned = True
                return 'warning', sample
        else:
            self.good_samples += 1
            self.good_streak += 1
            self.bad_streak = 0
            if self.good_streak >= self.confirm_samples and self.state != 'black':
                self.state = 'black'
                return 'black', sample
        return None, sample

    def as_dict(self):
        return {
            'state': self.state,
            'ever_warned': self.ever_warned,
            'bad_samples': self.bad_samples,
            'good_samples': self.good_samples,
            'unknown_samples': self.unknown_samples,
            'latest_sample': self.last_sample.as_dict() if self.last_sample else None,
            'warning': WARNING_TEXT if self.state == 'warning' else '',
        }


def inspect_saved_background(session, max_frames=12):
    """Bounded video/baseline preflight, independent of any OCR dependencies.

    Only the baseline and first few saved frames are decoded. Inconclusive
    material is never labeled as black; per-session warnings do not block
    reanalysis. Unlike monitor events, this is not real-time zoning detection.
    """
    import cv2

    session = Path(session)
    samples = []
    errors = []
    baseline = session / 'baseline_gray.png'
    if baseline.is_file():
        try:
            sample = classify_background(cv2.imread(str(baseline), cv2.IMREAD_GRAYSCALE))
            samples.append(dict(source='baseline_gray.png', **sample.as_dict()))
        except Exception as exc:
            errors.append('baseline: ' + str(exc))

    video = session / 'combat_changed_gray_ffv1.mkv'
    if video.is_file():
        cap = cv2.VideoCapture(str(video))
        try:
            if not cap.isOpened():
                errors.append('Cannot decode FFV1 video for background preflight')
            else:
                for i in range(max(0, int(max_frames))):
                    ok, frame = cap.read()
                    if not ok:
                        break
                    if len(frame.shape) == 3:
                        frame = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                    sample = classify_background(frame)
                    samples.append(dict(source=f'video-frame-{i + 1}', **sample.as_dict()))
        finally:
            cap.release()
    else:
        errors.append('No FFV1 video (Light mode or missing capture)')

    bad = sum(row['verdict'] == 'nonblack' for row in samples)
    good = sum(row['verdict'] == 'black' for row in samples)
    # A single corrupted/covered frame is insufficient evidence. Baseline is
    # a sample but must not be the only reason for a positive warning.
    if bad >= 3 and bad >= good * 2:
        status = 'warning'
    elif good >= 3 and good >= bad * 2:
        status = 'black'
    else:
        status = 'inconclusive'
    return {
        'version': 1,
        'status': status,
        'warning': WARNING_TEXT if status == 'warning' else '',
        'assumption': 'solid visually black background; opacity cannot be measured directly',
        'samples': samples,
        'samples_nonblack': bad,
        'samples_black': good,
        'samples_inconclusive': len(samples) - bad - good,
        'errors': errors,
        'scanned_first_saved_frames_only': True,
    }


def save_background_report(session, report, filename='combat_log_background_check.json'):
    """Persist one diagnostic record without changing the original video."""
    target = Path(session) / filename
    # Distinct temporary files prevent live/recorder threads from racing on a
    # shared .tmp path, while replace() keeps readers from seeing partial JSON.
    temporary = target.with_name(target.name + f'.{os.getpid()}.{threading.get_ident()}.tmp')
    temporary.write_text(json.dumps(report, indent=2), encoding='utf-8')
    temporary.replace(target)
    return target
