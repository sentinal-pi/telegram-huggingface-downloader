# Telegram AI Model Backup

บอต Python สำหรับดาวน์โหลดโมเดลจาก **Hugging Face** ลงเครื่องที่รันบอต เช่น Mac, Linux หรือเซิร์ฟเวอร์ เพื่อเก็บสำรอง ไม่ต้องมี GPU และไม่ต้องเปิดพอร์ตขาเข้า

**ติดตั้งบน Synology DSM:** ดู [คู่มือ Synology](SYNOLOGY.md) มี Dockerfile, Compose และตัวอย่าง `.env` สำหรับ Container Manager

## เริ่มใช้งาน

ใช้ Python 3.11 ขึ้นไป:

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

1. เปิด [@BotFather](https://t.me/BotFather) ใน Telegram ใช้ `/newbot` แล้วนำ token ใส่ `TELEGRAM_BOT_TOKEN` ใน `.env`
2. ใส่ Telegram **user ID แบบตัวเลข** ของคุณใน `ALLOWED_USER_IDS` หากมีหลายคนคั่นด้วย comma (ไม่ใช่ username) ดู ID ได้จาก Telegram client หรือบอตตรวจ ID ที่คุณเชื่อถือ
3. ตั้ง `BACKUP_DIR` เป็นที่เก็บไฟล์ เช่น `/Volumes/Backup/AIModels` รองรับ path ที่มีช่องว่าง หากเป็น external drive ต้อง mount ก่อนเริ่มบอต
4. ถ้าเป็นโมเดล private/gated ให้ใส่ read token ใน `HF_TOKEN` และขอสิทธิ์/ยอมรับเงื่อนไขบนหน้าโมเดลก่อน
5. รันจากโฟลเดอร์โปรเจกต์:

```sh
python -m model_backup.bot
```

เปิดแชตส่วนตัวกับบอต กด Start แล้วส่งคำสั่ง เมื่อบอตทำงานแล้ว `/whoami` แสดง ID ผู้ส่งได้แม้ยังไม่ได้รับสิทธิ์ แก้ `ALLOWED_USER_IDS` แล้วเริ่มบอตใหม่

เก็บ token ไว้ใน `.env` บนเครื่อง ไม่ส่งผ่านแชตหรือ commit เข้า Git ไฟล์ `.gitignore` ตั้งค่าไว้ให้แล้ว

## คำสั่ง

ส่ง **URL หน้าโมเดล** หรือ **ชื่อ repository แบบ `owner/model`** ในแชตได้โดยตรง ไม่ต้องใส่ `/download` ส่งครั้งละหนึ่งรายการหรือหลายรายการโดยแยกบรรทัดละหนึ่งโมเดล เช่น:

```text
https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct
```

หรือ:

```text
Qwen/Qwen2.5-0.5B-Instruct
```

ทั้งสองแบบจะนำโมเดลเข้าคิวและดาวน์โหลดไฟล์ทั้งหมดจาก `main` ใช้ `/download` หากต้องการระบุ revision หรือ pattern และเริ่มบอตใหม่หลังอัปเดตโค้ด

ส่งหลายโมเดลในข้อความเดียวได้ และผสม URL กับชื่อ repo ได้:

```text
https://huggingface.co/owner/model-one
owner/model-two
owner/model-three
```

บอตตอบสรุปหมายเลขงานของแต่ละรายการ ข้าม repo ที่ซ้ำกันในข้อความเดียว และแจ้งรายการที่ไม่ถูกต้องโดยยังรับรายการอื่น คิวรวมสูงสุด 20 งาน (รวมงานกำลังทำและงานของผู้ใช้คนอื่น) ถ้าเกินจะรับเท่าที่เหลือและแจ้งรายการที่ต้องส่งใหม่ ทำทีละโมเดลตามลำดับ ไม่ข้ามโมเดลซ้ำจากข้อความก่อนหน้า เพราะอาจเป็นการสำรองชุดใหม่

| คำสั่ง | การทำงาน |
| --- | --- |
| `/download owner/model` | สำรองไฟล์ทุกไฟล์ใน repository ที่ main |
| `/download owner/model revision` | เลือก branch, tag หรือ commit |
| `/download owner/model main *.gguf` | สำรองเฉพาะไฟล์ที่ตรงกับ pattern |
| `/download https://huggingface.co/owner/model` | ใช้ URL หน้าโมเดล |
| `/status` หรือ `/status 1` | ดูสรุปสถานะ งานที่กำลังทำทั้งหมด คิวรอ หรือรายละเอียดรายงาน |
| `/cancel 1` | ยกเลิกงาน เก็บไฟล์ที่ดาวน์โหลดไว้ |
| `/retry 1` | นำงาน failed/cancelled กลับเข้าคิว |
| `/list` หรือ `/models` | แสดงโมเดลที่สำเร็จทั้งหมด แบ่งหน้าละ 5 ชุด พร้อมจำนวนไฟล์ ขนาด และตำแหน่ง |
| `/list 2` หรือ `/models 2` | ดูหน้าที่ 2 (มีคำสั่งหน้าถัดไป/ก่อนหน้าแสดงท้ายรายการ) |
| `/disk` | ดูพื้นที่ว่างในดิสก์เก็บโมเดล |
| `/whoami` | แสดง Telegram user ID |
| `/help` | ดูวิธีใช้ |

รับคำสั่งดาวน์โหลดเฉพาะ user ID ที่อนุญาตและเฉพาะแชตส่วนตัว ทำทีละงาน คิวสูงสุด 20 งาน แจ้งเตือนเมื่อเริ่มและเสร็จ/ล้มเหลว ใช้ `/status` ดูงานที่กำลังทำพร้อมคิวทั้งหมด และ `/status ID` ดูรายละเอียดรายงาน รวมจำนวนไฟล์ที่เสร็จ ไฟล์ปัจจุบัน ขั้นตอน และขนาดรวม เปอร์เซ็นต์คำนวณตามจำนวนไฟล์ ไม่ใช่จำนวนไบต์ภายในไฟล์ขนาดใหญ่ รายการแสดงเฉพาะงานของผู้ใช้ที่ส่งคำสั่ง

## ไฟล์สำรองและการตรวจสอบ

```text
backups/
  owner/model/<commit>/job-1/
    config.json
    ...weights and other repository files...
    backup-manifest.json
```

ก่อนดาวน์โหลดจะ resolve revision เป็น commit แล้วใช้ commit เดียวกันกับทุกไฟล์ manifest บันทึก repo, revision, commit, pattern, ขนาด และ SHA-256 ของไฟล์ทุกไฟล์ เก็บแยกตามงานเพื่อไม่ให้การเลือก pattern หรือ revision ต่างกันปะปนกัน

ตรวจไฟล์โดยไม่ใช้อินเทอร์เน็ต:

```sh
python -m model_backup.verify "/absolute/path/to/backups/owner/model/COMMIT/job-1"
```

คำสั่งคืน exit code 0 เมื่อทุกไฟล์ตรงกับ manifest และคืนค่าไม่ใช่ 0 เมื่อไฟล์เสียหาย/หายไป SHA-256 นี้ใช้ตรวจการเปลี่ยนแปลงหลังสำรอง ไม่ใช่ลายเซ็นรับรองจากผู้สร้างโมเดล

ถ้าจะย้ายข้อมูลสำรอง ให้คัดลอกทั้งโฟลเดอร์ `job-ID` รวม manifest ไปยังดิสก์อีกลูก สามารถโหลดโมเดลจาก path นี้ผ่านไลบรารีที่รองรับโมเดลนั้น เช่น `from_pretrained(local_path, local_files_only=True)` โดยต้องมี runtime/dependencies ที่จำเป็นในเครื่องด้วย บอตไม่รันโค้ดหรือโหลด weights ของโมเดล

## ข้อควรรู้

- `MAX_MODEL_GB` ค่าเริ่มต้น 100 GiB จำกัดขนาดรวมไฟล์ที่เลือก และ `MIN_FREE_GB` ค่าเริ่มต้น 5 GiB เป็นพื้นที่ที่ต้องเหลือ ตรวจพื้นที่ก่อนเริ่มและก่อนแต่ละไฟล์ โปรแกรมอื่นยังอาจใช้พื้นที่ระหว่างดาวน์โหลดได้
- ดาวน์โหลดทุกไฟล์อาจรวม weights หลายรูปแบบ ทำให้ใช้พื้นที่มาก ใช้ pattern หากต้องการบางรูปแบบเท่านั้น แต่ไฟล์ที่เลือกอาจไม่ครบสำหรับโหลดโมเดล
- งานที่กำลังทำอยู่เมื่อปิดบอตจะกลับเข้าคิวเมื่อเปิดใหม่ Hugging Face ใช้ข้อมูลใน `.cache/huggingface` ภายในโฟลเดอร์งานสำหรับใช้ไฟล์เดิม/ดาวน์โหลดต่อเมื่อรองรับ อย่าลบโฟลเดอร์นี้ระหว่างงาน
- `/retry` จะ resolve revision อีกครั้ง ถ้า branch เปลี่ยนจะสร้างโฟลเดอร์ commit ใหม่ หากต้องการชุดเดิมแน่นอน ให้ระบุ commit SHA ในคำสั่งดาวน์โหลด
- ข้อมูลคิวอยู่ใน `state/jobs.sqlite3` ควรเก็บ `state/` ไว้ด้วย `/list` อ่านประวัติจากฐานข้อมูล นับจำนวน repo ไม่ซ้ำและแยกไฟล์สำรองแต่ละงาน พร้อมเตือนเมื่อไม่พบ manifest ที่ตำแหน่งเดิม การแสดงรายการไม่ได้ตรวจ SHA-256 ของทุกไฟล์ ใช้คำสั่ง verify เมื่อต้องการตรวจจริง
- เปิดบอตได้ครั้งละหนึ่ง instance ต่อ `STATE_DIR` และใช้ token เดียวกับ polling instance เดียวเท่านั้น เมื่อเริ่มใหม่จะทิ้งคำสั่ง Telegram ที่ค้างขณะออฟไลน์ แต่คิวที่บันทึกแล้วอยู่ครบ
- เครื่องต้องเปิดและไม่ sleep ระหว่างดาวน์โหลด บน macOS ใช้ `caffeinate -i .venv/bin/python -m model_backup.bot` ได้ หยุดด้วย Ctrl+C
- ไม่มีการลบโมเดลอัตโนมัติและไม่มีการส่งไฟล์โมเดลผ่าน Telegram
- รองรับไฟล์ใน model repository ของ Hugging Face ไม่รวม external URLs, Git history หรือ dependency ที่อยู่นอก repository

## ทดสอบ

```sh
python -m unittest discover -s tests -v
```

อ้างอิง: [Hugging Face download API](https://huggingface.co/docs/huggingface_hub/en/package_reference/file_download), [python-telegram-bot](https://docs.python-telegram-bot.org/en/stable/)
