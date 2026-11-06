"""Direct recognition preprocessing and Unicode file reading (no model weights)."""
import sys
import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / '_internal'))
from direct_indra_onnx import line_to_tensor
from image_file_io import read_gray_image


class DirectRecognizerTests(unittest.TestCase):
    def test_blank_crop(self):
        self.assertIsNone(line_to_tensor(np.zeros((14, 500), dtype=np.uint8), 32, 1))
        self.assertIsNone(line_to_tensor(np.full((14, 500), 255, dtype=np.uint8), 32, 1))

    def test_row_shape_and_polarity(self):
        gray = np.zeros((14, 350), dtype=np.uint8)
        cv2.putText(gray, '12345', (8, 11), cv2.FONT_HERSHEY_SIMPLEX, .35, 255, 1)
        a = line_to_tensor(gray, 32, 1)
        b = line_to_tensor(255-gray, 32, 1)
        self.assertIsNotNone(a)
        self.assertIsNotNone(b)
        self.assertEqual(a[0].dtype, np.float32)
        self.assertEqual(a[0].shape[0], 32)
        self.assertEqual(a[0].shape[1] % 8, 0)
        self.assertLessEqual(abs(a[1] - b[1]), 2)

    def test_unicode_png_path(self):
        gray = np.zeros((14, 100), dtype=np.uint8)
        cv2.putText(gray, '123', (2, 11), cv2.FONT_HERSHEY_SIMPLEX, .35, 255, 1)
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'JanBSørensen' / 'billede_æå.png'
            p.parent.mkdir(parents=True)
            ok, encoded = cv2.imencode('.png', gray)
            self.assertTrue(ok)
            p.write_bytes(encoded.tobytes())
            self.assertTrue(np.array_equal(gray, read_gray_image(p)))


if __name__ == '__main__':
    unittest.main()
