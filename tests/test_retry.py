import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
from huggingface_hub import hf_hub_download, set_client_factory

from model_backup.retry import (RateLimited, RetryPolicy, check_rate_limit, configure_http_client,
                                rate_limit_headers, retry_delay, with_rate_limit_retry)
from model_backup.verify import verify
from model_backup.worker import download


class RetryTests(unittest.TestCase):
    @patch('model_backup.retry.time.sleep')
    def test_real_hf_sdk_http_429_then_success_with_mock_server(self, sleep):
        commit = 'b' * 40
        requests = []
        def server(request):
            if not request.url.path.endswith('/config.json'):
                # SDK telemetry can run in a background thread; it is not a download attempt.
                return httpx.Response(200)
            requests.append(request.method)
            if len(requests) == 1:
                return httpx.Response(429, headers={'Retry-After': '0'})
            return httpx.Response(200, headers={'X-Repo-Commit': commit, 'ETag': '"abc123"',
                                               'Content-Length': '2'}, content=b'{}')
        set_client_factory(lambda: httpx.Client(transport=httpx.MockTransport(server),
                           follow_redirects=True, event_hooks={'response': [check_rate_limit]}))
        try:
            with tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                result = with_rate_limit_retry(
                    lambda: hf_hub_download(repo_id='owner/model', filename='config.json', revision=commit,
                                            local_dir=root / 'backup', token=False),
                    root / 'status.json', {'phase': 'downloading'}, RetryPolicy())
                self.assertEqual(Path(result).read_bytes(), b'{}')
                self.assertEqual(requests, ['HEAD', 'HEAD', 'GET'])
                sleep.assert_called_once()
        finally:
            configure_http_client()

    @patch('model_backup.retry.random.uniform', return_value=0)
    @patch('model_backup.retry.time.time', return_value=1000)
    def test_server_hints_dates_and_fallback_backoff(self, clock, jitter):
        policy = RetryPolicy(retries=5, base_seconds=30, max_seconds=60)
        self.assertEqual(retry_delay({}, 0, policy), 31)
        self.assertEqual(retry_delay({}, 1, policy), 61)
        self.assertEqual(retry_delay({}, 5, policy), 61)
        # Server cooldowns must not be shortened by our fallback cap.
        self.assertEqual(retry_delay({'retry-after': '900'}, 0, policy), 901)
        self.assertEqual(retry_delay({'retry-after': 'Thu, 01 Jan 1970 00:20:00 GMT'}, 0, policy), 201)
        self.assertEqual(retry_delay({'ratelimit': '"api";r=0;t=300'}, 0, policy), 301)
        self.assertEqual(retry_delay({'retry-after': '60', 'ratelimit': '"api";r=0;t=300'}, 0, policy), 301)
        for invalid in ['invalid', '-3', 'nan', 'inf', '']:
            self.assertEqual(retry_delay({'retry-after': invalid}, 0, policy), 31)

    @patch('model_backup.retry.time.sleep')
    def test_http_429_hook_retries_and_updates_status(self, sleep):
        with tempfile.TemporaryDirectory() as folder:
            progress = Path(folder) / 'status.json'
            attempts = []
            def handler(request):
                attempts.append(request)
                return httpx.Response(429, headers={'Retry-After': '90'}) if len(attempts) == 1 else httpx.Response(200)
            def waiting(seconds):
                p = json.loads(progress.read_text())
                self.assertEqual(p['phase'], 'rate_limited')
                self.assertEqual(p['retry_number'], 1)
                self.assertEqual(p['done'], 2)
                self.assertGreaterEqual(seconds, 91)
            sleep.side_effect = waiting
            with httpx.Client(transport=httpx.MockTransport(handler),
                              event_hooks={'response': [check_rate_limit]}) as client:
                response = with_rate_limit_retry(lambda: client.get('https://huggingface.co/test'), progress,
                                                  {'phase': 'downloading', 'done': 2}, RetryPolicy())
            self.assertEqual(response.status_code, 200)
            self.assertEqual(len(attempts), 2)
            self.assertEqual(json.loads(progress.read_text())['phase'], 'downloading')

    @patch('model_backup.retry.time.sleep')
    def test_exhaustion_is_bounded_and_does_not_expose_raw_errors(self, sleep):
        with tempfile.TemporaryDirectory() as folder:
            operation = Mock(side_effect=RateLimited({'retry-after': '0'}))
            with self.assertRaisesRegex(ValueError, 'หลังลองใหม่ 2 ครั้ง'):
                with_rate_limit_retry(operation, Path(folder) / 'status.json', {'phase': 'planning'}, RetryPolicy(retries=2))
            self.assertEqual(operation.call_count, 3)
            self.assertEqual(sleep.call_count, 2)

    @patch('model_backup.retry.time.sleep')
    def test_other_errors_fail_immediately(self, sleep):
        with tempfile.TemporaryDirectory() as folder:
            response = httpx.Response(401, request=httpx.Request('GET', 'https://huggingface.co/test'))
            operation = Mock(side_effect=httpx.HTTPStatusError('Unauthorized', request=response.request, response=response))
            with self.assertRaises(httpx.HTTPStatusError):
                with_rate_limit_retry(operation, Path(folder) / 'status.json', {}, RetryPolicy())
            operation.assert_called_once()
            sleep.assert_not_called()

    def test_wrapped_http_and_native_xet_rate_limits_are_detected(self):
        response = httpx.Response(429, headers={'Retry-After': '42'}, request=httpx.Request('GET', 'https://huggingface.co/test'))
        wrapped = RuntimeError('metadata failed')
        wrapped.__cause__ = httpx.HTTPStatusError('rate limit', request=response.request, response=response)
        self.assertEqual(rate_limit_headers(wrapped)['retry-after'], '42')
        self.assertEqual(rate_limit_headers(RuntimeError('HTTP status client error (429 Too Many Requests)')), {})
        self.assertIsNone(rate_limit_headers(RuntimeError('Cannot download weights-429.bin')))

    @patch('model_backup.retry.time.sleep')
    @patch('model_backup.worker.hf_hub_download')
    @patch('model_backup.worker.snapshot_download')
    def test_planning_and_file_retries_preserve_completed_files_and_commit(self, snapshot, fetch, sleep):
        commit = 'a' * 40
        plan = [SimpleNamespace(commit_hash=commit, filename=name, file_size=2)
                for name in ('config.json', 'weights.bin')]
        snapshot.side_effect = [RateLimited({}), plan]
        calls = []
        def fake_fetch(**kwargs):
            self.assertEqual(kwargs['revision'], commit)
            name = kwargs['filename']
            calls.append(name)
            if name == 'weights.bin' and calls.count(name) == 1:
                raise RateLimited({})
            path = kwargs['local_dir'] / name
            path.write_bytes(b'{}')
            return str(path)
        fetch.side_effect = fake_fetch
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            progress = root / 'progress.json'
            download({'id': 1, 'repo': 'owner/model', 'revision': 'main', 'pattern': None}, root, progress, 1, 0)
            self.assertEqual(calls, ['config.json', 'weights.bin', 'weights.bin'])
            result = json.loads(progress.read_text())
            self.assertEqual(result['phase'], 'completed')
            self.assertEqual(verify(Path(result['path'])), [])

    def test_retry_configuration_validation(self):
        for env in [{'HF_MAX_RETRIES': '-1'}, {'HF_MAX_RETRIES': '101'},
                    {'HF_RETRY_BASE_SECONDS': 'nan'}, {'HF_RETRY_MAX_SECONDS': '0'},
                    {'HF_RETRY_BASE_SECONDS': '900', 'HF_RETRY_MAX_SECONDS': '30'}]:
            with self.subTest(env=env), patch.dict(os.environ, env, clear=True), self.assertRaises(ValueError):
                RetryPolicy.from_env()
        with patch.dict(os.environ, {'HF_MAX_RETRIES': '0'}, clear=True):
            self.assertEqual(RetryPolicy.from_env().retries, 0)


if __name__ == '__main__':
    unittest.main()
