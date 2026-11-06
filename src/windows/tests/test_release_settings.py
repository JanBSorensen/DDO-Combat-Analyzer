"""Light/Direct upgrade contract on Windows and Linux, without model weights."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / '_internal'))
from settings_migration import MIGRATION_KEY, apply_release_defaults


class UpgradeDefaultsTests(unittest.TestCase):
    def test_existing_full_indra_changes_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.json'
            previous = {
                'performance': {'mode': 'full', 'special': 12},
                'live_dps': {'ocr_engine': 'indra', 'continuous_mode': False, 'font_size': 36},
                'capture': {'left': 300, 'top': 200, 'width': 800, 'height': 480},
                'ui': {'first_run_guide_shown': True},
            }
            path.write_text(json.dumps(previous), encoding='utf-8')
            self.assertTrue(apply_release_defaults(path))
            actual = json.loads(path.read_text(encoding='utf-8'))
            self.assertEqual(actual['performance'], {'mode': 'light', 'special': 12})
            self.assertEqual(actual['live_dps']['ocr_engine'], 'indra_direct')
            self.assertTrue(actual['live_dps']['continuous_mode'])
            self.assertEqual(actual['live_dps']['font_size'], 36)
            self.assertEqual(actual['capture'], previous['capture'])
            self.assertEqual(actual['ui'], previous['ui'])
            self.assertTrue(actual['release_migrations'][MIGRATION_KEY])
            actual['performance']['mode'] = 'full'
            actual['live_dps']['ocr_engine'] = 'indra'
            path.write_text(json.dumps(actual), encoding='utf-8')
            self.assertFalse(apply_release_defaults(path))
            later = json.loads(path.read_text(encoding='utf-8'))
            self.assertEqual(later['performance']['mode'], 'full')
            self.assertEqual(later['live_dps']['ocr_engine'], 'indra')

    def test_legacy_config_unicode_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'JanBSørensen' / 'config.json'
            path.parent.mkdir()
            path.write_text('{"capture": {"width": 123}}', encoding='utf-8')
            self.assertTrue(apply_release_defaults(path))
            self.assertEqual(json.loads(path.read_text())['capture'], {'width': 123})
            self.assertFalse(apply_release_defaults(path))

    def test_invalid_or_missing_config_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.json'
            self.assertFalse(apply_release_defaults(path))
            path.write_text('{bad', encoding='utf-8')
            with self.assertRaises(json.JSONDecodeError):
                apply_release_defaults(path)
            self.assertEqual(path.read_text(), '{bad')


if __name__ == '__main__':
    unittest.main()
