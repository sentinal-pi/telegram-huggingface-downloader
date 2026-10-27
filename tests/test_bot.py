import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from model_backup.bot import cancel, download_text, enqueue, list_backups, retry, shutdown, status
from model_backup.core import Store, atomic_json


class BotTests(unittest.IsolatedAsyncioTestCase):
    async def test_rate_limit_status_displays_wait_and_attempt(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = Store(root / 'jobs.db')
            job = store.add(42, 'owner/model', 'main', None)
            store.update(job, 'running')
            atomic_json(root / f'{job}.json', {'phase': 'rate_limited', 'retry_number': 2,
                        'retry_limit': 5, 'retry_at': 1060, 'done': 1, 'count': 3})
            update, context = self.report_context(root, store, [str(job)])
            with patch('model_backup.bot.time.time', return_value=1000):
                await status(update, context)
            text = update.effective_message.reply_text.await_args.args[0]
            self.assertIn('รอลองใหม่อัตโนมัติ', text)
            self.assertIn('ลองใหม่ใน 60 วินาที (ครั้ง 2/5)', text)
            store.db.close()

    async def test_cancel_terminates_worker_during_rate_limit_sleep(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = Store(root / 'jobs.db')
            job = store.add(42, 'owner/model', 'main', None)
            store.update(job, 'running')
            update, context = self.report_context(root, store, [str(job)])
            script = '''
import sys
from pathlib import Path
from model_backup.retry import RateLimited, RetryPolicy, with_rate_limit_retry
def operation():
    raise RateLimited({'retry-after': '3600'})
with_rate_limit_retry(operation, Path(sys.argv[1]), {'phase': 'planning'}, RetryPolicy())
'''
            progress = root / f'{job}.json'
            process = await asyncio.create_subprocess_exec(sys.executable, '-c', script, str(progress))
            try:
                async def wait_for_cooldown():
                    while not progress.exists() or json.loads(progress.read_text()).get('phase') != 'rate_limited':
                        await asyncio.sleep(0.01)
                await asyncio.wait_for(wait_for_cooldown(), 10)
                context.application.bot_data.update(active_id=job, process=process)
                await cancel(update, context)
                await asyncio.wait_for(process.wait(), 5)
                self.assertNotEqual(process.returncode, 0)
                self.assertEqual(store.get(job)['status'], 'cancelled')
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.wait()
                store.db.close()

    def report_context(self, root, store, args=None):
        context = SimpleNamespace(application=SimpleNamespace(bot_data={
            'allowed': {42}, 'store': store, 'state': root}), args=args or [])
        update = SimpleNamespace(effective_message=SimpleNamespace(reply_text=AsyncMock()),
                                 effective_user=SimpleNamespace(id=42),
                                 effective_chat=SimpleNamespace(id=42, type='private'))
        return update, context

    async def test_model_pages_reach_all_backups_and_exclude_other_users(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            store = Store(root / 'jobs.db')
            for i in range(12):
                job_id = store.add(42, f'owner/model-{i}', 'main', None)
                store.update(job_id, 'completed', str(root / f'backup-{i}'))
            other = store.add(99, 'other/private-model', 'main', None)
            store.update(other, 'completed', '/other')
            seen = set()
            for page in range(1, 4):
                update, context = self.report_context(root, store, [str(page)])
                await list_backups(update, context)
                text = '\n'.join(call.args[0] for call in update.effective_message.reply_text.await_args_list)
                self.assertIn(f'หน้า {page}/3', text)
                self.assertIn('โมเดล 12 รายการ / ไฟล์สำรอง 12 ชุด', text)
                self.assertNotIn('other/private-model', text)
                for line in text.splitlines():
                    if line.startswith('#'):
                        seen.add(int(line.split()[0][1:]))
                        repo = line.split()[1]
                        self.assertIn(f'หน้าโมเดล: https://huggingface.co/{repo}', text)
            self.assertEqual(seen, set(range(1, 13)))
            for invalid in ['0', '-1', 'no', '4', '99999999999999999999']:
                update, context = self.report_context(root, store, [invalid])
                await list_backups(update, context)
                update.effective_message.reply_text.assert_awaited_once()
            store.db.close()

    async def test_status_keeps_running_job_visible_with_progress_and_full_queue(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            store = Store(root / 'jobs.db')
            active = store.add(42, 'owner/active', 'main', None)
            store.update(active, 'running')
            atomic_json(root / f'{active}.json', {'phase': 'downloading', 'done': 2,
                        'count': 4, 'file': 'weights.safetensors', 'bytes': 1024**3})
            for i in range(99):
                store.add(42, f'owner/queued-{i}', 'main', None)
            hidden = store.add(99, 'other/hidden', 'main', None)
            update, context = self.report_context(root, store)
            await status(update, context)
            text = '\n'.join(call.args[0] for call in update.effective_message.reply_text.await_args_list)
            self.assertIn('กำลังทำงาน 1 | รอคิว 99', text)
            self.assertIn('2/4 (50.0% ตามจำนวนไฟล์)', text)
            self.assertIn('weights.safetensors', text)
            for i in range(99):
                self.assertIn(f'owner/queued-{i} — queued', text)
            self.assertNotIn('other/hidden', text)
            update, context = self.report_context(root, store, [str(hidden)])
            await status(update, context)
            self.assertIn('ไม่พบงาน', update.effective_message.reply_text.await_args.args[0])
            store.db.close()

    async def test_unauthorized_users_and_groups_cannot_enqueue(self):
        store = MagicMock()
        data = {'allowed': {42}, 'store': store}
        context = SimpleNamespace(application=SimpleNamespace(bot_data=data), args=['owner/model'])
        for user, chat_type in [(99, 'private'), (42, 'group')]:
            message = SimpleNamespace(reply_text=AsyncMock())
            update = SimpleNamespace(effective_message=message, effective_user=SimpleNamespace(id=user),
                                     effective_chat=SimpleNamespace(id=42, type=chat_type))
            for handler in [enqueue, download_text]:
                message.reply_text.reset_mock()
                await handler(update, context)
                message.reply_text.assert_awaited_once()
        store.add.assert_not_called()

    async def test_plain_urls_and_repo_names_enqueue_with_default_revision(self):
        repo = 'ChoLuckyHexSexySuccubusQueen135Age/pornworksAnimeDesireNSFW_v02-lora-Model'
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / 'jobs.db')
            context = SimpleNamespace(application=SimpleNamespace(bot_data={'allowed': {42}, 'store': store}))
            for text in [repo, f'  https://huggingface.co/{repo}\n']:
                update = SimpleNamespace(effective_message=SimpleNamespace(text=text, reply_text=AsyncMock()),
                                         effective_user=SimpleNamespace(id=42),
                                         effective_chat=SimpleNamespace(id=42, type='private'))
                await download_text(update, context)
                job = store.recent(42)[0]
                self.assertEqual(job['repo'], repo)
                self.assertEqual(job['revision'], 'main')
                self.assertIsNone(job['pattern'])
            self.assertEqual(len(store.recent(42)), 1)
            self.assertIn('รายการซ้ำ', update.effective_message.reply_text.await_args.args[0])
            store.db.close()

    async def test_invalid_plain_text_does_not_enqueue(self):
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / 'jobs.db')
            context = SimpleNamespace(application=SimpleNamespace(bot_data={'allowed': {42}, 'store': store}))
            for text in ['hello', 'https://example.com/owner/model', 'owner/model owner/other']:
                update = SimpleNamespace(effective_message=SimpleNamespace(text=text, reply_text=AsyncMock()),
                                         effective_user=SimpleNamespace(id=42),
                                         effective_chat=SimpleNamespace(id=42, type='private'))
                await download_text(update, context)
                update.effective_message.reply_text.assert_awaited_once()
            self.assertIsNone(store.next())
            store.db.close()

    async def test_batch_mixes_urls_names_invalid_entries_and_duplicates(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            store = Store(root / 'jobs.db')
            update, context = self.report_context(root, store)
            update.effective_message.text = ('\n owner/first\nhttps://huggingface.co/owner/second\r\n'
                                             '\ninvalid\nhttps://huggingface.co/owner/first/\nowner/third\n')
            await download_text(update, context)
            rows = store.db.execute('SELECT * FROM jobs ORDER BY id').fetchall()
            self.assertEqual([row['repo'] for row in rows], ['owner/first', 'owner/second', 'owner/third'])
            self.assertTrue(all(row['revision'] == 'main' and row['pattern'] is None for row in rows))
            text = '\n'.join(call.args[0] for call in update.effective_message.reply_text.await_args_list)
            self.assertIn('เพิ่มเข้าคิว 3 งาน', text)
            self.assertIn('รายการซ้ำ: owner/first', text)
            self.assertIn('❌', text)
            self.assertIn('invalid', text)
            store.db.close()

    async def test_batch_respects_existing_queue_limit_and_reports_overflow(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            store = Store(root / 'jobs.db')
            for index in range(99):
                store.add(42, f'owner/existing-{index}', 'main', None)
            update, context = self.report_context(root, store)
            update.effective_message.text = 'owner/accepted\nowner/overflow'
            await download_text(update, context)
            self.assertEqual(store.db.execute('SELECT count(*) FROM jobs').fetchone()[0], 100)
            self.assertEqual(store.recent(42)[0]['repo'], 'owner/accepted')
            text = '\n'.join(call.args[0] for call in update.effective_message.reply_text.await_args_list)
            self.assertIn('เพิ่มเข้าคิว 1 งาน', text)
            self.assertIn('owner/overflow', text)
            self.assertIn('คิวเต็ม (100 งาน)', text)
            store.db.close()

    async def test_single_download_and_retry_respect_full_queue(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            store = Store(root / 'jobs.db')
            for index in range(100):
                job_id = store.add(42, f'owner/existing-{index}', 'main', None)
            store.update(job_id, 'running')
            failed = store.add(42, 'owner/failed', 'main', None)
            store.update(failed, 'failed')
            progress = root / f'{failed}.json'
            atomic_json(progress, {'phase': 'failed', 'error': 'previous failure'})
            update, context = self.report_context(root, store, ['owner/overflow'])
            await enqueue(update, context)
            self.assertIn('คิวเต็ม (100 งาน)', update.effective_message.reply_text.await_args.args[0])
            update, context = self.report_context(root, store, [str(failed)])
            await retry(update, context)
            self.assertIn('คิวเต็ม (100 งาน)', update.effective_message.reply_text.await_args.args[0])
            self.assertEqual(store.get(failed)['status'], 'failed')
            self.assertTrue(progress.exists())
            store.update(1, 'completed')
            await retry(update, context)
            self.assertEqual(store.get(failed)['status'], 'queued')
            self.assertFalse(progress.exists())
            self.assertEqual(store.db.execute("SELECT count(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0], 100)
            store.db.close()

    async def test_authorized_download_is_persisted(self):
        with tempfile.TemporaryDirectory() as d:
            store = Store(Path(d) / 'jobs.db')
            context = SimpleNamespace(application=SimpleNamespace(bot_data={'allowed': {42}, 'store': store}),
                                      args=['owner/model', 'main', '*.gguf'])
            update = SimpleNamespace(effective_message=SimpleNamespace(reply_text=AsyncMock()),
                                     effective_user=SimpleNamespace(id=42),
                                     effective_chat=SimpleNamespace(id=42, type='private'))
            await enqueue(update, context)
            self.assertEqual(store.next()['pattern'], '*.gguf')
            self.assertEqual(store.next()['chat_id'], 42)
            store.db.close()

    async def test_shutdown_terminates_child_even_if_queue_clears_process(self):
        process = SimpleNamespace(returncode=None, terminate=MagicMock(), wait=AsyncMock())
        data = {'process': process, 'store': SimpleNamespace(db=SimpleNamespace(close=MagicMock()))}
        ready = asyncio.Event()
        async def worker():
            try:
                ready.set()
                await asyncio.Future()
            finally:
                data['process'] = None
        data['worker_task'] = asyncio.create_task(worker())
        await ready.wait()
        await shutdown(SimpleNamespace(bot_data=data))
        process.terminate.assert_called_once()
        process.wait.assert_awaited_once()


if __name__ == '__main__':
    unittest.main()
