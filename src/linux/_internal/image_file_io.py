"""Read grayscale image files even when Windows paths contain Unicode characters."""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np


def read_gray_image(path):
    """Return grayscale uint8 ndarray or None for missing or invalid PNG."""
    try:
        raw = Path(path).read_bytes()
        if not raw:
            return None
        return cv2.imdecode(np.frombuffer(raw, dtype=np.uint8),
                            cv2.IMREAD_GRAYSCALE)
    except (OSError, ValueError, cv2.error):
        return None
