"""Offline integrity check: python -m model_backup.verify /path/to/backup"""
import argparse
import json
from pathlib import Path

from .core import digest, safe_path


def verify(root):
    manifest = json.loads((root / 'backup-manifest.json').read_text())
    failures = []
    if not manifest.get('files'):
        raise ValueError('Manifest has no files')
    for item in manifest['files']:
        path = safe_path(root, item['path'])
        if not path.is_file() or path.stat().st_size != item['size'] or digest(path) != item['sha256']:
            failures.append(item['path'])
    return failures


def main():
    parser = argparse.ArgumentParser(description='Verify local backup SHA-256 checksums (offline)')
    parser.add_argument('directory', type=Path)
    args = parser.parse_args()
    try:
        failures = verify(args.directory.resolve())
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise SystemExit(f'Invalid backup: {exc}') from None
    if failures:
        raise SystemExit('FAILED: ' + ', '.join(failures))
    print('OK: all manifest files match their SHA-256 checksums')


if __name__ == '__main__':
    main()
