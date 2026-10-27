import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from model_backup.core import Store, atomic_json, digest, parse_repo, safe_path
from model_backup.verify import verify
from model_backup.worker import download


class BackupTests(unittest.TestCase):
    def test_repo_input_rejects_traversal_and_other_hosts(self):
        self.assertEqual(parse_repo('https://huggingface.co/owner/model'), 'owner/model')
        for value in ['../model', 'owner/../file', 'https://evil.test/owner/model', 'owner/model;ls', 'owner/model/tree/main']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_repo(value)

    def test_path_cannot_escape_backup(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for name in ['../escape', '/etc/passwd', '.']:
                with self.subTest(name=name), self.assertRaises(ValueError):
                    safe_path(root, name)
            (root / 'link').symlink_to(root.parent, target_is_directory=True)
            with self.assertRaises(ValueError):
                safe_path(root, 'link/escape')

    def test_queue_survives_restart_and_cancel_stays_cancelled(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / 'jobs.db'
            store = Store(path)
            first = store.add(7, 'owner/model', 'main', None)
            second = store.add(7, 'owner/other', 'main', '*.gguf')
            store.update(first, 'running')
            store.update(second, 'cancelled')
            store.db.close()
            store = Store(path)
            store.recover()
            self.assertEqual(store.next()['id'], first)
            self.assertEqual(store.get(second)['status'], 'cancelled')
            store.db.close()

    def test_verify_detects_modified_and_missing_files(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            weight = root / 'weights.bin'
            weight.write_bytes(b'original')
            atomic_json(root / 'backup-manifest.json', {'files': [
                {'path': weight.name, 'size': 8, 'sha256': digest(weight)}]})
            self.assertEqual(verify(root), [])
            weight.write_bytes(b'changed!')
            self.assertEqual(verify(root), ['weights.bin'])
            weight.unlink()
            self.assertEqual(verify(root), ['weights.bin'])

    @patch('model_backup.worker.hf_hub_download')
    @patch('model_backup.worker.snapshot_download')
    def test_download_pins_commit_and_builds_verifiable_manifest(self, snapshot, fetch):
        commit = 'a' * 40
        snapshot.return_value = [SimpleNamespace(commit_hash=commit, filename='config.json', file_size=2)]
        def fake_fetch(**kwargs):
            self.assertEqual(kwargs['revision'], commit)
            path = kwargs['local_dir'] / kwargs['filename']
            path.write_bytes(b'{}')
            return str(path)
        fetch.side_effect = fake_fetch
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            progress = root / 'progress.json'
            download({'id': 1, 'repo': 'owner/model', 'revision': 'main', 'pattern': None}, root, progress, 1, 0)
            result = json.loads(progress.read_text())
            self.assertEqual(result['phase'], 'completed')
            self.assertEqual(verify(Path(result['path'])), [])

    @patch('model_backup.worker.hf_hub_download')
    @patch('model_backup.worker.snapshot_download')
    def test_size_and_disk_limits_stop_before_transfer(self, snapshot, fetch):
        snapshot.return_value = [SimpleNamespace(commit_hash='b' * 40, filename='large.bin', file_size=1024**3)]
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            job = {'id': 1, 'repo': 'owner/model', 'revision': 'main', 'pattern': None}
            with self.assertRaises(ValueError):
                download(job, root, root / 'progress.json', 0.5, 0)
            with patch('model_backup.worker.shutil.disk_usage', return_value=SimpleNamespace(free=10)):
                with self.assertRaises(ValueError):
                    download(job, root, root / 'progress.json', 2, 1)
            fetch.assert_not_called()


if __name__ == '__main__':
    unittest.main()
