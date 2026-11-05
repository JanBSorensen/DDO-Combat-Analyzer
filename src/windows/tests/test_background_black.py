import sys
from pathlib import Path
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / '_internal'))
from combat_log_background import BackgroundMonitor, classify_background


def synthetic_log(background=0):
    x = np.full((500, 780), int(background), dtype=np.uint8)
    # Multiple rows of white text in the left Combat Log column; each line
    # is narrow, which leaves most of the frame representing the background.
    for y in range(12, 480, 18):
        x[y:y+4, 9:100] = 225
        x[y:y+4, 105:240] = 225
    return x


class BackgroundTests(unittest.TestCase):
    def test_solid_black_with_text(self):
        self.assertEqual(classify_background(synthetic_log(0)).verdict, 'black')

    def test_gray_background_with_text_warns(self):
        self.assertEqual(classify_background(synthetic_log(70)).verdict, 'nonblack')

    def test_loading_screen_without_chat_not_misreported(self):
        self.assertEqual(classify_background(np.full((500, 780), 95, dtype=np.uint8)).verdict, 'unknown')
        self.assertEqual(classify_background(np.zeros((500, 780), dtype=np.uint8)).verdict, 'unknown')

    def test_requires_three_samples_with_interval(self):
        monitor = BackgroundMonitor(sample_seconds=2.0, confirm_samples=3)
        bad = synthetic_log(70)
        self.assertIsNone(monitor.inspect(bad, now=0)[0])
        self.assertEqual(monitor.inspect(bad, now=1)[1], None)
        self.assertIsNone(monitor.inspect(bad, now=2)[0])
        self.assertEqual(monitor.inspect(bad, now=4)[0], 'warning')
        self.assertEqual(monitor.state, 'warning')
        good = synthetic_log(0)
        self.assertIsNone(monitor.inspect(good, now=6)[0])
        self.assertIsNone(monitor.inspect(good, now=8)[0])
        self.assertEqual(monitor.inspect(good, now=10)[0], 'black')
        self.assertFalse(monitor.state == 'warning')

    def test_unknown_breaks_streak_without_clearing_warning(self):
        monitor = BackgroundMonitor(sample_seconds=0.1, confirm_samples=3)
        bad = synthetic_log(70)
        for t in [0,1,2]:
            monitor.inspect(bad,now=t)
        self.assertEqual(monitor.state,'warning')
        monitor.inspect(np.zeros((500, 780),dtype=np.uint8),now=3)
        self.assertEqual(monitor.state,'warning')
        monitor.inspect(synthetic_log(0),now=4)
        self.assertEqual(monitor.state,'warning')

    def test_reset_after_geometry_change(self):
        monitor=BackgroundMonitor(sample_seconds=0.1,confirm_samples=3)
        for t in [0,1,2]: monitor.inspect(synthetic_log(70),now=t)
        monitor.reset()
        self.assertEqual(monitor.state,'unknown')
        self.assertFalse(monitor.ever_warned)


if __name__ == '__main__':
    unittest.main()
