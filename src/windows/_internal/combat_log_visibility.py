"""Conservative guard for a DDO Combat Log temporarily covered by zoning/loading UI.

The screen tracker must NEVER compare a loading screen to the Combat Log or
interpret the reappearing OLD log as new damage. This module has no OCR or GUI
dependencies and runs on small, subsampled grayscale frames.

It deliberately prefers a short unobserved interval to inventing damage when
there is no trustworthy evidence that the log was visible continuously.
"""
from __future__ import annotations

from dataclasses import dataclass
import numpy as np


@dataclass(frozen=True)
class LogVisibilityResult:
    action: str  # 'track', 'hidden', or 'rebaseline'
    transition: str = ''  # 'hidden' or 'resumed', only on transitions
    reason: str = ''
    dark_fraction: float = 0.0
    ink_fraction: float = 0.0
    left_ink_fraction: float = 0.0
    midtone_fraction: float = 0.0
    scene_change: float = 0.0


class CombatLogVisibilityGuard:
    """Recognize obvious overlays and safely rebaseline after they disappear.

    The supported/validated capture style is the nearly-black, opaque Combat
    Log background. Thresholds are deliberately broad, calibrated to *actual*
    log frames, not to a particular DDO loading-screen artwork. A very dark
    loading screen that resembles chat may require a later scene jump before
    it is recognized. Sudden, non-scroll-compatible scene changes are also
    treated as unsafe rather than as possible new damage.
    """

    def __init__(self, resume_frames=3):
        self.resume_frames = max(2, int(resume_frames))
        self.state = 'visible'
        self.resume_streak = 0
        self.good = None
        self.last_good_thumbnail = None
        self.covered_frames = 0
        self.transitions_to_hidden = 0
        self.transitions_to_visible = 0
        self.blocked_frames = 0

    def reset_for_geometry_change(self):
        self.state = 'visible'
        self.resume_streak = 0
        self.good = None
        self.last_good_thumbnail = None
        self.covered_frames = 0

    @staticmethod
    def _measurement(gray):
        if gray is None or gray.ndim != 2 or not gray.size:
            raise ValueError('Expected a nonempty grayscale Combat Log image')
        # 1/16th of the pixels: inexpensive enough for 30 FPS on older CPUs.
        thumb = gray[::4, ::4]
        left_cols = max(2, min(thumb.shape[1], 24))  # first ~96 screen pixels
        return thumb, {
            'dark': float(np.mean(thumb <= 35)),
            'ink': float(np.mean(thumb >= 95)),
            'left_ink': float(np.mean(thumb[:, :left_cols] >= 95)),
            'midtone': float(np.mean((thumb >= 36) & (thumb <= 155))),
        }

    def inspect(self, gray):
        thumb, m = self._measurement(gray)
        change = 0.0
        if self.last_good_thumbnail is not None:
            if thumb.shape == self.last_good_thumbnail.shape:
                # Pure chat scrolling changes ~10-15% of pixels in reference
                # sessions. Artwork appearing/disappearing is far larger.
                change = float(np.mean(
                    np.abs(thumb.astype(np.int16) - self.last_good_thumbnail.astype(np.int16)) > 55
                ))
            else:
                change = 1.0
        good = self.good

        # A dark, text-bearing reference lets us recognize black fade frames
        # and otherwise black loading images with a title in the middle.
        reference_text = good is not None and good['ink'] >= 0.025
        reference_left = good is not None and good['left_ink'] >= 0.015
        bad = ''
        if m['dark'] < 0.68 or m['midtone'] > 0.23:
            bad = 'loading-artwork-or-non-chat-background'
        elif reference_text and m['ink'] < max(0.010, good['ink'] * 0.18):
            bad = 'combat-log-text-disappeared'
        elif reference_left and m['left_ink'] < max(0.006, good['left_ink'] * 0.18):
            bad = 'combat-log-left-column-disappeared'
        elif good is not None and change > 0.40:
            bad = 'abrupt-full-scene-change'

        if self.state == 'visible':
            if bad:
                self.state = 'hidden'
                self.transitions_to_hidden += 1
                self.covered_frames = 1
                self.resume_streak = 0
                self.blocked_frames += 1
                return LogVisibilityResult('hidden', 'hidden', bad, m['dark'],
                                           m['ink'], m['left_ink'], m['midtone'], change)
            # Adapt slowly to normal chat layout changes, preserving a
            # reasonably text-rich example to recognize the next blackout.
            if self.good is None or (m['ink'] >= 0.025 and m['left_ink'] >= 0.015):
                self.good = m
            self.last_good_thumbnail = thumb.copy()
            return LogVisibilityResult('track', '', '', m['dark'], m['ink'],
                                       m['left_ink'], m['midtone'], change)

        # Hidden: never feed either loading frames OR return-to-log frames to
        # scroll tracking. Require several consecutive plausible chat frames.
        self.blocked_frames += 1
        self.covered_frames += 1
        if bad:
            self.resume_streak = 0
            return LogVisibilityResult('hidden', '', bad, m['dark'], m['ink'],
                                       m['left_ink'], m['midtone'], change)

        self.resume_streak += 1
        if self.resume_streak < self.resume_frames:
            return LogVisibilityResult('hidden', '', 'waiting-for-stable-combat-log',
                                       m['dark'], m['ink'], m['left_ink'], m['midtone'], change)

        # IMPORTANT: return-to-chat is a *baseline*, not a scroll/appended row.
        # There is no reliable way to timestamp unseen events during loading.
        self.state = 'visible'
        self.transitions_to_visible += 1
        self.resume_streak = 0
        self.covered_frames = 0
        self.good = m
        self.last_good_thumbnail = thumb.copy()
        return LogVisibilityResult('rebaseline', 'resumed', 'stable-combat-log-reappeared',
                                   m['dark'], m['ink'], m['left_ink'], m['midtone'], change)
