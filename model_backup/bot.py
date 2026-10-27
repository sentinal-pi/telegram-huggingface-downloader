import asyncio
import json
import logging
import math
import os
import shutil
import sys
import time
from contextlib import suppress
from functools import wraps
from pathlib import Path

from dotenv import load_dotenv
from filelock import FileLock, Timeout
from telegram import BotCommand
from telegram.error import TelegramError
from telegram.ext import Application, CommandHandler, MessageHandler, filters

from .core import Store, parse_repo
from .retry import RetryPolicy

HELP = '''🤖 AI Model Backup — Hugging Face
ส่ง URL หน้าโมเดล หรือ owner/model ได้โดยตรง
ส่งหลายโมเดลได้ โดยแยกบรรทัดละหนึ่งโมเดล (คิวรวมสูงสุด 20 งาน)
บอตจะดาวน์โหลดไฟล์ทั้งหมดจาก main ให้อัตโนมัติ
/download owner/model [revision] [pattern]
ตัวอย่าง: /download Qwen/Qwen2.5-0.5B-Instruct
เลือกไฟล์: /download owner/model main *.gguf
ใช้ URL หน้าโมเดลแทน owner/model ได้
/status [job_id] — ภาพรวมคิวและความคืบหน้าระดับไฟล์
/cancel job_id — ยกเลิกงาน (เก็บไฟล์บางส่วนไว้)
/retry job_id — ลองงานที่ล้มเหลว/ยกเลิกอีกครั้ง
/list [page] หรือ /models [page] — โมเดลที่โหลดสำเร็จทั้งหมด แบ่งหน้า
/disk — พื้นที่ว่าง
/whoami — Telegram ID ของคุณ

ไฟล์เก็บในเครื่องที่รันบอต พร้อม backup-manifest.json และ SHA-256
หากเลือก pattern จะสำรองเฉพาะไฟล์ที่ตรงกัน
โมเดล gated/private ต้องตั้ง HF_TOKEN ที่มีสิทธิ์ก่อน'''


def protected(handler):
    @wraps(handler)
    async def wrapped(update, context):
        if not update.effective_message or not update.effective_user or not update.effective_chat:
            return
        if update.effective_chat.type != 'private' or update.effective_user.id not in context.application.bot_data['allowed']:
            await update.effective_message.reply_text('ไม่ได้รับอนุญาตให้ใช้บอตนี้')
            return
        await handler(update, context)
    return wrapped


async def whoami(update, context):
    if update.effective_chat and update.effective_chat.type == 'private' and update.effective_user:
        await update.effective_message.reply_text(f'Telegram user ID: {update.effective_user.id}')


@protected
async def help_command(update, context):
    await update.effective_message.reply_text(HELP)


@protected
async def enqueue(update, context):
    await enqueue_request(update, context, context.args)


@protected
async def download_text(update, context):
    entries = [line.strip() for line in update.effective_message.text.splitlines() if line.strip()]
    if len(entries) <= 1:
        await enqueue_request(update, context, entries)
        return
    store = context.application.bot_data['store']
    seen = set()
    results = []
    added = 0
    for index, entry in enumerate(entries, 1):
        try:
            repo = parse_repo(entry)
            if repo in seen:
                results.append(f'ข้ามรายการซ้ำ: {repo}')
                continue
            seen.add(repo)
            job_id = queue_job(store, update.effective_chat.id, repo, 'main', None)
            results.append(f'✅ #{job_id}: {repo}')
            added += 1
        except ValueError as exc:
            results.append(f'❌ บรรทัดที่ {index}: {entry}\n{exc}')
    await reply_sections(update, [f'เพิ่มเข้าคิว {added} งาน (revision: main, ไฟล์: ทั้งหมด)',
                                  *results, 'ดูคิว: /status'])


def queue_job(store, chat, repo, revision, pattern):
    pending = store.db.execute("SELECT count(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0]
    if pending >= 20:
        raise ValueError('คิวเต็ม (20 งาน) รอให้บางงานเสร็จก่อน แล้วส่งรายการนี้ใหม่')
    return store.add(chat, repo, revision, pattern)


async def enqueue_request(update, context, args):
    try:
        if not 1 <= len(args) <= 3:
            raise ValueError('ใช้ /download owner/model [revision] [pattern]')
        repo = parse_repo(args[0])
        revision = args[1] if len(args) >= 2 else 'main'
        pattern = args[2] if len(args) == 3 else None
        store = context.application.bot_data['store']
        job_id = queue_job(store, update.effective_chat.id, repo, revision, pattern)
        await update.effective_message.reply_text(f'เพิ่มงาน #{job_id}: {repo}\nrevision: {revision}\nไฟล์: {pattern or "ทั้งหมด"}\nตรวจสอบด้วย /status {job_id}')
    except ValueError as exc:
        await update.effective_message.reply_text(str(exc))


def owned_job(update, context):
    if len(context.args) != 1 or not context.args[0].isdigit() or len(context.args[0]) > 12:
        return None
    job = context.application.bot_data['store'].get(int(context.args[0]))
    return job if job and job['chat_id'] == update.effective_chat.id else None


def progress_for(data, job_id):
    try:
        return json.loads((data['state'] / f'{job_id}.json').read_text())
    except (OSError, ValueError):
        return {}


@protected
async def status(update, context):
    data = context.application.bot_data
    store = data['store']
    chat = update.effective_chat.id
    if context.args:
        job = owned_job(update, context)
        if not job:
            await update.effective_message.reply_text('ไม่พบงาน ใช้ /status job_id')
            return
        rows = [job]
        lines = []
    else:
        counts = dict(store.db.execute(
            'SELECT status,count(*) FROM jobs WHERE chat_id=? GROUP BY status', (chat,)).fetchall())
        lines = ['📥 สถานะดาวน์โหลด\n'
                 f"กำลังทำงาน {counts.get('running', 0)} | รอคิว {counts.get('queued', 0)}\n"
                 f"สำเร็จ {counts.get('completed', 0)} | ล้มเหลว {counts.get('failed', 0)} | ยกเลิก {counts.get('cancelled', 0)}"]
        rows = store.db.execute(
            "SELECT * FROM jobs WHERE chat_id=? AND status IN ('running','queued') "
            "ORDER BY CASE status WHEN 'running' THEN 0 ELSE 1 END,id", (chat,)).fetchall()
        if not rows:
            lines.append('ไม่มีงานกำลังดาวน์โหลดหรือรอคิว')
            rows = store.recent(chat)[:5]
        lines.append('ดูโมเดลที่สำเร็จทั้งหมด: /list\nดูรายละเอียดงาน: /status job_id')
    for job in rows:
        line = f"#{job['id']} {job['repo']} — {job['status']}"
        if context.args or job['status'] == 'running':
            p = progress_for(data, job['id'])
            if job['detail']:
                line += f"\n{job['detail']}"
            if p:
                done, count = p.get('done', 0), p.get('count', 0)
                if count:
                    line += f"\nไฟล์ที่เสร็จ {done}/{count} ({done / count * 100:.1f}% ตามจำนวนไฟล์)"
                phases = {'downloading': 'ดาวน์โหลด', 'sha256': 'ตรวจ SHA-256',
                          'completed': 'สำเร็จ', 'failed': 'ล้มเหลว',
                          'planning': 'ตรวจรายการไฟล์', 'rate_limited': 'ติด Rate Limit — รอลองใหม่อัตโนมัติ'}
                line += f"\nขั้นตอน: {phases.get(p.get('phase'), p.get('phase', 'เตรียมงาน'))}"
                if p.get('phase') == 'rate_limited':
                    remaining = max(0, math.ceil(p.get('retry_at', 0) - time.time()))
                    line += f"\nลองใหม่ใน {remaining} วินาที (ครั้ง {p['retry_number']}/{p['retry_limit']})"
                if 'bytes' in p:
                    line += f"\nขนาดไฟล์ทั้งหมด: {p['bytes'] / 1024**3:.3f} GiB"
                line += f"\n{p.get('file', p.get('path', p.get('error', '')))}"
            elif job['status'] == 'running':
                line += '\nกำลังตรวจรายการไฟล์และพื้นที่ว่าง'
        lines.append(line)
    await reply_sections(update, lines)


async def reply_sections(update, sections):
    # Split long reports instead of silently dropping jobs at Telegram's limit.
    text = '\n\n'.join(sections)
    while text:
        await update.effective_message.reply_text(text[:3500])
        text = text[3500:]


@protected
async def cancel(update, context):
    data = context.application.bot_data
    job = owned_job(update, context)
    if not job or job['status'] not in ('queued', 'running'):
        await update.effective_message.reply_text('ใช้ /cancel job_id สำหรับงานที่อยู่ในคิวหรือกำลังทำงาน')
        return
    data['store'].update(job['id'], 'cancelled')
    if data.get('active_id') == job['id']:
        process = data.get('process')
        if process and process.returncode is None:
            with suppress(ProcessLookupError):
                process.terminate()
    await update.effective_message.reply_text(f"ยกเลิกงาน #{job['id']} แล้ว เก็บไฟล์บางส่วนไว้สำหรับ /retry")


@protected
async def retry(update, context):
    data = context.application.bot_data
    job = owned_job(update, context)
    if not job or job['status'] not in ('failed', 'cancelled') or data.get('active_id') == job['id']:
        await update.effective_message.reply_text('ใช้ /retry job_id ของงานที่ล้มเหลวหรือยกเลิกแล้ว (รอ process เดิมหยุดก่อน)')
        return
    (data['state'] / f"{job['id']}.json").unlink(missing_ok=True)
    data['store'].update(job['id'], 'queued')
    await update.effective_message.reply_text(f"นำงาน #{job['id']} กลับเข้าคิวแล้ว")


@protected
async def list_backups(update, context):
    if context.args and (len(context.args) != 1 or not context.args[0].isdigit()
                         or len(context.args[0]) > 9 or int(context.args[0]) < 1):
        await update.effective_message.reply_text('ใช้ /list [page] เช่น /list 2')
        return
    page = int(context.args[0]) if context.args else 1
    data = context.application.bot_data
    db = data['store'].db
    chat = update.effective_chat.id
    total, models = db.execute(
        "SELECT count(*),count(DISTINCT repo) FROM jobs WHERE chat_id=? AND status='completed'", (chat,)).fetchone()
    if not total:
        await update.effective_message.reply_text('ยังไม่มีไฟล์สำรองที่เสร็จสมบูรณ์')
        return
    pages = (total + 4) // 5
    if page > pages:
        await update.effective_message.reply_text(f'มีทั้งหมด {pages} หน้า ใช้ /list 1 ถึง /list {pages}')
        return
    rows = db.execute(
        "SELECT * FROM jobs WHERE chat_id=? AND status='completed' ORDER BY id DESC LIMIT 5 OFFSET ?",
        (chat, (page - 1) * 5)).fetchall()
    lines = [f'📦 โมเดล {models} รายการ / ไฟล์สำรอง {total} ชุด\nหน้า {page}/{pages}']
    for job in rows:
        p = progress_for(data, job['id'])
        line = f"#{job['id']} {job['repo']}\nrevision: {job['revision']} | ไฟล์: {job['pattern'] or 'ทั้งหมด'}"
        if p.get('phase') == 'completed':
            line += f"\n{p.get('count', '?')} ไฟล์ | {p.get('bytes', 0) / 1024**3:.3f} GiB"
        line += f"\nตำแหน่ง: {job['detail']}"
        if not job['detail'] or not (Path(job['detail']) / 'backup-manifest.json').is_file():
            line += '\n⚠️ ไม่พบ manifest ที่ตำแหน่งเดิม (ไฟล์อาจถูกย้ายหรือลบ)'
        lines.append(line)
    navigation = []
    if page > 1:
        navigation.append(f'ก่อนหน้า: /list {page - 1}')
    if page < pages:
        navigation.append(f'ถัดไป: /list {page + 1}')
    lines.append('\n'.join(navigation + ['รายละเอียด: /status job_id']))
    await reply_sections(update, lines)


@protected
async def disk(update, context):
    usage = shutil.disk_usage(context.application.bot_data['root'])
    await update.effective_message.reply_text(f'พื้นที่ว่าง {usage.free / 1024**3:.2f} / {usage.total / 1024**3:.2f} GiB')


async def notify(app, chat, message):
    try:
        await app.bot.send_message(chat, message[:4000])
    except TelegramError:
        logging.warning('Cannot deliver notification; job status remains available in /status')


async def run_queue(app):
    data = app.bot_data
    store = data['store']
    while True:
        job = store.next()
        if not job:
            await asyncio.sleep(1)
            continue
        store.update(job['id'], 'running')
        data['active_id'] = job['id']
        try:
            # No shell; arguments and repository names cannot execute commands.
            process = await asyncio.create_subprocess_exec(
                sys.executable, '-m', 'model_backup.worker', str(job['id']),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            data['process'] = process
            if store.get(job['id'])['status'] == 'cancelled' and process.returncode is None:
                with suppress(ProcessLookupError):
                    process.terminate()
            await notify(app, job['chat_id'], f"เริ่มงาน #{job['id']}: {job['repo']}")
            code = await process.wait()
            if store.get(job['id'])['status'] == 'cancelled':
                continue
            p = progress_for(data, job['id'])
            ok = code == 0 and p.get('phase') == 'completed'
            detail = p.get('path', '') if ok else p.get('error', 'Worker หยุดโดยไม่คาดคิด')
            store.update(job['id'], 'completed' if ok else 'failed', detail)
            await notify(app, job['chat_id'], f"งาน #{job['id']} {'สำเร็จ ✅' if ok else 'ล้มเหลว'}\n{detail}")
        except asyncio.CancelledError:
            raise
        except Exception:
            store.update(job['id'], 'failed', 'ไม่สามารถเริ่ม worker ได้')
            await notify(app, job['chat_id'], f"งาน #{job['id']} ล้มเหลว: ไม่สามารถเริ่ม worker ได้")
        finally:
            data['active_id'] = None
            data['process'] = None


async def startup(app):
    app.bot_data['store'].recover()
    await app.bot.set_my_commands([BotCommand(name, description) for name, description in [
        ('download', 'Download a Hugging Face model'), ('status', 'Check download jobs'),
        ('cancel', 'Cancel a job'), ('retry', 'Retry a job'), ('list', 'List all backups by page'),
        ('models', 'List downloaded models'),
        ('disk', 'Free disk space'), ('help', 'Usage'), ('whoami', 'Your Telegram ID')]])
    app.bot_data['worker_task'] = asyncio.create_task(run_queue(app))


async def shutdown(app):
    process = app.bot_data.get('process')
    task = app.bot_data.get('worker_task')
    if task:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
    if process and process.returncode is None:
        with suppress(ProcessLookupError):
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), 10)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
    app.bot_data['store'].db.close()


async def on_error(update, context):
    logging.error('Telegram handler failed (%s)', type(context.error).__name__)


def main():
    load_dotenv()
    token = os.getenv('TELEGRAM_BOT_TOKEN', '').strip()
    try:
        allowed = {int(v.strip()) for v in os.getenv('ALLOWED_USER_IDS', '').split(',') if v.strip()}
        if not token or not allowed or any(v <= 0 for v in allowed):
            raise ValueError()
        for key, default in [('MAX_MODEL_GB', '100'), ('MIN_FREE_GB', '5')]:
            value = float(os.getenv(key, default))
            if not math.isfinite(value) or value <= 0:
                raise ValueError()
        RetryPolicy.from_env()
    except ValueError:
        raise SystemExit('ตรวจ TELEGRAM_BOT_TOKEN, ALLOWED_USER_IDS, ค่าพื้นที่ และค่า HF_* retry ใน .env')
    root = Path(os.getenv('BACKUP_DIR', './backups')).expanduser().resolve()
    state = Path(os.getenv('STATE_DIR', './state')).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    state.mkdir(parents=True, exist_ok=True)
    os.environ.update(BACKUP_DIR=str(root), STATE_DIR=str(state), HF_HUB_DISABLE_PROGRESS_BARS='1')
    app = Application.builder().token(token).post_init(startup).post_stop(shutdown).build()
    with FileLock(str(state / 'bot.lock'), timeout=0):
        app.bot_data.update(allowed=allowed, root=root, state=state, store=Store(state / 'jobs.sqlite3'))
        for names, handler in [(['start', 'help'], help_command), (['whoami'], whoami),
                               (['download'], enqueue), (['status'], status), (['cancel'], cancel),
                               (['retry'], retry), (['list', 'models'], list_backups), (['disk'], disk)]:
            app.add_handler(CommandHandler(names, handler))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, download_text))
        app.add_error_handler(on_error)
        app.run_polling(drop_pending_updates=True)


if __name__ == '__main__':
    logging.basicConfig(level=logging.WARNING)
    try:
        main()
    except Timeout:
        raise SystemExit('มีบอตอีก instance ใช้ STATE_DIR นี้อยู่แล้ว')
