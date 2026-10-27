import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from model_backup.bot import download_text, enqueue, retry
from model_backup.core import Store, atomic_json


class DuplicateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / 'jobs.db')
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(lambda: self.store.db.close())

    def request(self, args=None, text='owner/model'):
        update = SimpleNamespace(effective_message=SimpleNamespace(text=text, reply_text=AsyncMock()),
                                 effective_user=SimpleNamespace(id=42),
                                 effective_chat=SimpleNamespace(id=42, type='private'))
        context = SimpleNamespace(application=SimpleNamespace(bot_data={
            'allowed': {42}, 'store': self.store, 'state': self.root}), args=args or [])
        return update, context

    async def test_queued_running_and_completed_jobs_reject_same_request(self):
        for status in ('queued', 'running', 'completed'):
            repo = f'owner/model-{status}'
            job_id = self.store.add(42, repo, 'main', None)
            self.store.update(job_id, status)
            update, context = self.request(text=f'https://huggingface.co/{repo}/')
            before = self.store.db.execute('SELECT count(*) FROM jobs').fetchone()[0]
            await download_text(update, context)
            self.assertEqual(self.store.db.execute('SELECT count(*) FROM jobs').fetchone()[0], before)
            message = update.effective_message.reply_text.await_args.args[0]
            self.assertIn(f'รายการซ้ำ: {repo}', message)
            self.assertIn(f'/status {job_id}', message)

    async def test_duplicate_is_detected_after_database_reopen(self):
        job_id = self.store.add(42, 'owner/model', 'main', None)
        self.store.update(job_id, 'completed')
        self.store.db.close()
        self.store = Store(self.root / 'jobs.db')
        update, context = self.request(args=['owner/model'])
        await enqueue(update, context)
        self.assertIn('รายการซ้ำ', update.effective_message.reply_text.await_args.args[0])
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM jobs').fetchone()[0], 1)

    async def test_revision_and_pattern_changes_are_separate_backups(self):
        self.store.add(42, 'owner/model', 'main', None)
        for args in (['owner/model', 'v2'], ['owner/model', 'main', '*.gguf']):
            update, context = self.request(args=args)
            await enqueue(update, context)
            self.assertIn('เพิ่มงาน', update.effective_message.reply_text.await_args.args[0])
        update, context = self.request(args=['owner/model', 'main', '*.gguf'])
        await enqueue(update, context)
        self.assertIn('รายการซ้ำ', update.effective_message.reply_text.await_args.args[0])
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM jobs').fetchone()[0], 3)

    async def test_failed_and_cancelled_history_does_not_block_new_request(self):
        for state in ('failed', 'cancelled'):
            repo = f'owner/model-{state}'
            job_id = self.store.add(42, repo, 'main', None)
            self.store.update(job_id, state)
            update, context = self.request(text=repo)
            await download_text(update, context)
            self.assertIn('เพิ่มงาน', update.effective_message.reply_text.await_args.args[0])

    async def test_retry_is_blocked_by_another_matching_job_without_losing_error(self):
        existing = self.store.add(42, 'owner/model', 'main', None)
        job_id = self.store.add(42, 'owner/model', 'main', None)
        self.store.update(job_id, 'failed', 'previous error')
        progress = self.root / f'{job_id}.json'
        atomic_json(progress, {'phase': 'failed', 'error': 'previous error'})
        update, context = self.request(args=[str(job_id)])
        await retry(update, context)
        self.assertIn(f'/status {existing}', update.effective_message.reply_text.await_args.args[0])
        self.assertEqual(self.store.get(job_id)['status'], 'failed')
        self.assertTrue(progress.exists())
        self.store.update(existing, 'cancelled')
        await retry(update, context)
        self.assertEqual(self.store.get(job_id)['status'], 'queued')

    async def test_other_users_duplicates_do_not_expose_job_details(self):
        job_id = self.store.add(99, 'owner/model', 'main', None)
        self.store.update(job_id, 'completed', '/private/path')
        update, context = self.request()
        await download_text(update, context)
        message = update.effective_message.reply_text.await_args.args[0]
        self.assertIn('รายการซ้ำ', message)
        self.assertNotIn(f'#{job_id}', message)
        self.assertNotIn('/private/path', message)
        self.assertNotIn('/status ', message)

    async def test_duplicate_is_reported_before_queue_full(self):
        for index in range(100):
            self.store.add(42, f'owner/model-{index}', 'main', None)
        update, context = self.request(text='owner/model-0')
        await download_text(update, context)
        message = update.effective_message.reply_text.await_args.args[0]
        self.assertIn('รายการซ้ำ', message)
        self.assertNotIn('คิวเต็ม', message)


if __name__ == '__main__':
    unittest.main()
