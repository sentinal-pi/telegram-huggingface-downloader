import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from huggingface_hub import set_client_factory
from huggingface_hub.errors import (GatedRepoError, HfHubHTTPError, RemoteEntryNotFoundError,
                                    RepositoryNotFoundError, RevisionNotFoundError)

from model_backup.core import Store
from model_backup.retry import configure_http_client
from model_backup.worker import error_message, main


class ErrorMessageTests(unittest.TestCase):
    job = {'repo': 'owner/model', 'revision': 'missing-tag'}

    def upstream_error(self, error_type, status):
        response = httpx.Response(status, request=httpx.Request(
            'GET', 'https://huggingface.co/owner/model?token=secret-token'))
        return error_type('Raw upstream details: secret-token', response=response)

    def test_missing_repository_has_clear_model_name_and_no_raw_error(self):
        message = error_message(self.upstream_error(RepositoryNotFoundError, 404), self.job)
        self.assertIn('ไม่พบโมเดล: owner/model', message)
        self.assertNotIn('secret-token', message)

    def test_ambiguous_401_does_not_claim_repository_definitely_absent(self):
        message = error_message(self.upstream_error(RepositoryNotFoundError, 401), self.job)
        self.assertIn('ไม่พบโมเดลหรือไม่มีสิทธิ์เข้าถึง', message)
        self.assertIn('HF_TOKEN', message)

    def test_gated_revision_and_missing_file_are_distinguished(self):
        cases = [(GatedRepoError, 403, 'ไม่มีสิทธิ์เข้าถึงโมเดล gated'),
                 (RevisionNotFoundError, 404, 'ไม่พบ revision: missing-tag'),
                 (RemoteEntryNotFoundError, 404, 'ไม่พบไฟล์ที่ต้องดาวน์โหลด'),
                 (HfHubHTTPError, 403, 'ไม่มีสิทธิ์เข้าถึงโมเดล')]
        for error_type, code, expected in cases:
            with self.subTest(error_type=error_type):
                message = error_message(self.upstream_error(error_type, code), self.job)
                self.assertIn(expected, message)
                self.assertNotIn('ไม่พบโมเดล:', message)
                self.assertNotIn('secret-token', message)

    def test_wrapped_missing_repository_is_classified(self):
        error = RuntimeError('Download metadata failed')
        error.__cause__ = self.upstream_error(RepositoryNotFoundError, 404)
        self.assertIn('ไม่พบโมเดล: owner/model', error_message(error, self.job))

    def test_unknown_failures_are_redacted_but_local_validation_is_preserved(self):
        self.assertNotIn('secret-token', error_message(RuntimeError('secret-token'), self.job))
        self.assertEqual(error_message(ValueError('พื้นที่ว่างไม่พอ'), self.job), 'พื้นที่ว่างไม่พอ')

    @patch('model_backup.retry.time.sleep')
    @patch('model_backup.worker.configure_http_client')
    def test_worker_persists_not_found_from_real_sdk_without_rate_limit_retry(self, configure, sleep):
        def server(request):
            if '/api/models/' in request.url.path:
                return httpx.Response(404, headers={'X-Error-Code': 'RepoNotFound'},
                                      json={'error': 'Repository not found: secret-token'})
            return httpx.Response(200)
        set_client_factory(lambda: httpx.Client(transport=httpx.MockTransport(server)))
        try:
            with tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                store = Store(root / 'jobs.sqlite3')
                job = store.add(42, 'owner/model', 'main', None)
                store.db.close()
                with patch.dict(os.environ, {'STATE_DIR': str(root), 'BACKUP_DIR': str(root / 'backups')}, clear=True), \
                     patch('sys.argv', ['worker', str(job)]), self.assertRaises(SystemExit) as stopped:
                    main()
                self.assertEqual(stopped.exception.code, 1)
                result = json.loads((root / f'{job}.json').read_text())
                self.assertEqual(result['phase'], 'failed')
                self.assertIn('ไม่พบโมเดล: owner/model', result['error'])
                self.assertNotIn('secret-token', result['error'])
                sleep.assert_not_called()
        finally:
            configure_http_client()


if __name__ == '__main__':
    unittest.main()
