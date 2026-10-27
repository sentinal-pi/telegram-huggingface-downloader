import hashlib
import json
import os
import re
import sqlite3
from pathlib import Path
from urllib.parse import urlparse


def parse_repo(value):
    if value.startswith('https://'):
        url = urlparse(value)
        if url.netloc != 'huggingface.co' or url.query or url.fragment:
            raise ValueError('รองรับ URL https://huggingface.co/owner/model เท่านั้น')
        value = url.path.strip('/')
    if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]*/[A-Za-z0-9_][A-Za-z0-9_.-]*', value):
        raise ValueError('ระบุโมเดลเป็น owner/model หรือ URL หน้าโมเดล Hugging Face')
    if '..' in value or '--' in value or any(p.endswith('.') for p in value.split('/')):
        raise ValueError('ชื่อ repository ไม่ถูกต้อง')
    return value


def safe_path(root, name):
    path = (root / name).resolve()
    if not path.is_relative_to(root.resolve()) or path == root.resolve():
        raise ValueError('Unsafe file path')
    return path


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(8 * 1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def atomic_json(path, value):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temp, path)


class Store:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute('''CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL, repo TEXT NOT NULL,
            revision TEXT NOT NULL, pattern TEXT, status TEXT NOT NULL DEFAULT 'queued',
            detail TEXT NOT NULL DEFAULT '', created TEXT DEFAULT CURRENT_TIMESTAMP)''')
        self.db.commit()

    def add(self, chat, repo, revision, pattern):
        cur = self.db.execute('INSERT INTO jobs(chat_id,repo,revision,pattern) VALUES(?,?,?,?)',
                              (chat, repo, revision, pattern))
        self.db.commit()
        return cur.lastrowid

    def update(self, job_id, status, detail=''):
        self.db.execute('UPDATE jobs SET status=?,detail=? WHERE id=?', (status, detail, job_id))
        self.db.commit()

    def get(self, job_id):
        return self.db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()

    def find_duplicate(self, repo, revision, pattern, exclude_id=0):
        return self.db.execute(
            "SELECT * FROM jobs WHERE repo=? AND revision=? AND pattern IS ? AND id!=? "
            "AND status IN ('queued','running','completed') "
            "ORDER BY CASE status WHEN 'running' THEN 0 WHEN 'queued' THEN 1 ELSE 2 END,id DESC LIMIT 1",
            (repo, revision, pattern, exclude_id)).fetchone()

    def recent(self, chat):
        return self.db.execute('SELECT * FROM jobs WHERE chat_id=? ORDER BY id DESC LIMIT 10', (chat,)).fetchall()

    def next(self):
        return self.db.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY id LIMIT 1").fetchone()

    def recover(self):
        self.db.execute("UPDATE jobs SET status='queued',detail='Resuming after restart' WHERE status='running'")
        self.db.commit()
