"""Isolated download process: can be terminated without blocking Telegram."""
import argparse
import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from huggingface_hub import hf_hub_download, snapshot_download
from huggingface_hub.errors import (GatedRepoError, RemoteEntryNotFoundError,
                                    RepositoryNotFoundError, RevisionNotFoundError)

from .core import Store, atomic_json, digest, safe_path
from .retry import RetryPolicy, configure_http_client, with_rate_limit_retry


def error_message(error, job):
    """Translate upstream failures without exposing request URLs or credentials."""
    exc = error
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, GatedRepoError):
            return (f"ไม่มีสิทธิ์เข้าถึงโมเดล gated: {job['repo']}\n"
                    'ขอสิทธิ์หรือยอมรับเงื่อนไขบนหน้าโมเดล แล้วตั้ง HF_TOKEN ของบัญชีที่มีสิทธิ์')
        if isinstance(exc, RevisionNotFoundError):
            return (f"ไม่พบ revision: {job['revision']} ของโมเดล {job['repo']}\n"
                    'ตรวจชื่อ branch, tag หรือ commit แล้วส่งคำสั่งดาวน์โหลดใหม่')
        if isinstance(exc, RemoteEntryNotFoundError):
            return f"ไม่พบไฟล์ที่ต้องดาวน์โหลดในโมเดล: {job['repo']}\nไฟล์อาจถูกลบหรือไม่อยู่ใน revision นี้"
        response = getattr(exc, 'response', None)
        code = getattr(response, 'status_code', None)
        if isinstance(exc, RepositoryNotFoundError):
            if code == 401:
                # HF also returns 401 for nonexistent repos; do not claim certainty.
                return (f"ไม่พบโมเดลหรือไม่มีสิทธิ์เข้าถึง: {job['repo']}\n"
                        'ตรวจชื่อหรือ URL หากเป็น private/gated ให้ตรวจ HF_TOKEN และสิทธิ์ของบัญชี')
            return (f"ไม่พบโมเดล: {job['repo']}\n"
                    'ตรวจชื่อหรือ URL โมเดลอาจถูกลบ เปลี่ยนชื่อ หรือเป็น private ที่บัญชีไม่มีสิทธิ์เข้าถึง')
        if code in (401, 403):
            return f"ไม่มีสิทธิ์เข้าถึงโมเดล: {job['repo']}\nตรวจ HF_TOKEN และสิทธิ์ของบัญชี"
        if code == 404:
            return f"ไม่พบโมเดลหรือข้อมูลที่ร้องขอ: {job['repo']}\nตรวจชื่อโมเดล revision และไฟล์ที่ต้องดาวน์โหลด"
        exc = exc.__cause__ or exc.__context__
    return str(error) if isinstance(error, ValueError) else 'ดาวน์โหลดล้มเหลว ตรวจเครือข่ายและลองใหม่ภายหลัง'


def download(job, root, progress, max_gb, reserve_gb):
    policy = RetryPolicy.from_env()
    token = os.getenv('HF_TOKEN') or False
    plan = with_rate_limit_retry(
        lambda: snapshot_download(repo_id=job['repo'], revision=job['revision'],
                                  allow_patterns=job['pattern'], token=token, dry_run=True),
        progress, {'phase': 'planning'}, policy)
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
        def fetch():
            if shutil.disk_usage(root).free < item.file_size + reserve_gb * 1024**3:
                raise ValueError('พื้นที่ว่างต่ำกว่าเกณฑ์ หยุดดาวน์โหลด')
            return hf_hub_download(repo_id=job['repo'], filename=item.filename,
                                   revision=commit, local_dir=dest, token=token)
        downloaded = Path(with_rate_limit_retry(
            fetch, progress, {'phase': 'downloading', 'file': item.filename,
                              'done': index, 'count': len(plan), 'bytes': total}, policy))
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
    store = Store(state / 'jobs.sqlite3')
    try:
        job = store.get(args.job_id)
    finally:
        store.db.close()
    progress = state / f'{args.job_id}.json'
    try:
        configure_http_client()
        download(job, Path(os.environ['BACKUP_DIR']), progress,
                 float(os.getenv('MAX_MODEL_GB', '100')), float(os.getenv('MIN_FREE_GB', '5')))
    except Exception as exc:
        # Do not expose URLs, request headers, tokens, or raw upstream exceptions.
        message = error_message(exc, job)
        atomic_json(progress, {'phase': 'failed', 'error': message[:500]})
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
