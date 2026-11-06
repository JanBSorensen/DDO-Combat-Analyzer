"""One-time upgrade defaults for the 2026-11-06 release.

The auto-updater preserves config.json. Override ONLY Light/Direct/continuous
once, then respect any later user changes (including Full/Indra Full).
"""
from __future__ import annotations

import json
from pathlib import Path

MIGRATION_KEY = 'light_direct_defaults_20261106'


def apply_release_defaults(config_path):
    """Return True when existing config was migrated and atomically saved."""
    path = Path(config_path)
    if not path.is_file():
        return False
    config = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(config, dict):
        raise ValueError('Configuration root must be a JSON object')
    migrations = config.setdefault('release_migrations', {})
    if not isinstance(migrations, dict):
        raise ValueError('release_migrations must be an object')
    if migrations.get(MIGRATION_KEY) is True:
        return False
    config.setdefault('performance', {})['mode'] = 'light'
    live = config.setdefault('live_dps', {})
    live['ocr_engine'] = 'indra_direct'
    live['continuous_mode'] = True
    migrations[MIGRATION_KEY] = True
    pending = path.with_name(path.name + '.new')
    try:
        pending.write_text(json.dumps(config, indent=2, ensure_ascii=False) + '\n',
                           encoding='utf-8')
        pending.replace(path)
    finally:
        pending.unlink(missing_ok=True)
    return True
