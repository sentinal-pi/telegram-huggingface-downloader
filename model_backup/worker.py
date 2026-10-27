"""Isolated download process: can be terminated without blocking Telegram."""
import argparse
import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from huggingface_hub import hf_hub_download, snapshot_download

from .core import Store, atomic_json, digest, safe_path


def download(job, root, progress, max_gb, reserve_gb):
    token = os.getenv('HF_TOKEN') or False
    plan = snapshot_download(repo_id=job['repo'], revision=job['revision'],
                             allow_patterns=job['pattern'], token=token, dry_run=True)
    if not plan:
        raise ValueError('ไม่พบไฟล์ที่ตรงกับ pattern')
    commit = plan[0].commit_hash
    if not re.fullmatch(r'[a-fA-F0-9]{40,64}', commit):
        raise ValueError('Invalid upstream commit hash')
    # The job directory keeps filtered backups independent and safely resumable.
    dest = root / job['repo'] / commit / f"job-{job['id']}"
    dest.mkdir(parents=True, exist_ok=True)
    total = sum(f.file_size for f in plan)
    if total > max_gb * 1024**3:
        raise ValueError(f'โมเดลขนาด {total / 1024**3:.2f} GiB เกิน MAX_MODEL_GB')
    if shutil.disk_usage(root).free < total + reserve_gb * 1024**3:
        raise ValueError('พื้นที่ว่างไม่พอสำหรับไฟล์ทั้งหมดและพื้นที่สำรอง')
    files = []
    for index, item in enumerate(plan):
        target = safe_path(dest, item.filename)
        if item.filename == 'backup-manifest.json' or item.filename.startswith('.cache/huggingface/'):
            raise ValueError('Repository contains reserved backup/cache filename')
        atomic_json(progress, {'phase': 'downloading', 'file': item.filename,
                              'done': index, 'count': len(plan), 'bytes': total})
        if shutil.disk_usage(root).free < item.file_size + reserve_gb * 1024**3:
            raise ValueError('พื้นที่ว่างต่ำกว่าเกณฑ์ หยุดดาวน์โหลด')
        downloaded = Path(hf_hub_download(repo_id=job['repo'], filename=item.filename,
                                         revision=commit, local_dir=dest, token=token))
        if downloaded.resolve() != target or target.stat().st_size != item.file_size:
            raise ValueError(f'ขนาดไฟล์ไม่ถูกต้อง: {item.filename}')
        atomic_json(progress, {'phase': 'sha256', 'file': item.filename,
                              'done': index, 'count': len(plan), 'bytes': total})
        files.append({'path': item.filename, 'size': target.stat().st_size, 'sha256': digest(target)})
    manifest = {'repo': job['repo'], 'revision': job['revision'], 'commit': commit,
                'pattern': job['pattern'], 'created_utc': datetime.now(timezone.utc).isoformat(),
                'files': files}
    atomic_json(dest / 'backup-manifest.json', manifest)
    atomic_json(progress, {'phase': 'completed', 'done': len(plan), 'count': len(plan),
                          'bytes': total, 'path': str(dest)})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('job_id', type=int)
    args = parser.parse_args()
    state = Path(os.environ['STATE_DIR'])
    job = Store(state / 'jobs.sqlite3').get(args.job_id)
    progress = state / f'{args.job_id}.json'
    try:
        download(job, Path(os.environ['BACKUP_DIR']), progress,
                 float(os.getenv('MAX_MODEL_GB', '100')), float(os.getenv('MIN_FREE_GB', '5')))
    except Exception as exc:
        # Do not expose URLs, request headers, tokens, or raw upstream exceptions.
        message = str(exc) if isinstance(exc, ValueError) else 'ดาวน์โหลดล้มเหลว ตรวจ repo, revision, สิทธิ์ HF_TOKEN และเครือข่าย'
        atomic_json(progress, {'phase': 'failed', 'error': message[:500]})
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
