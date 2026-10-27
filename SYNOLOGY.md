# ติดตั้งบน Synology DSM ด้วย Container Manager

## ค่าที่เตรียมสำหรับเครื่องของคุณ

- รุ่น **DS925+** ใช้ AMD Ryzen V1500B สถาปัตยกรรม x86-64 ตาม [ข้อมูล Synology](https://www.synology.com/en-uk/products/DS925%2B)
- DSM ของคุณ: **7.4.1-90080 Update 1**
- Compose และ `synology.env.example` ตั้ง `DOCKER_PLATFORM=linux/amd64` สำหรับรุ่นนี้แล้ว
- ตั้งค่า `PUID`, `PGID` และ path ตามเครื่องจริง ไม่จำเป็นต้องติดตั้ง Python ลง DSM โดยตรง
- NAS รุ่น ARM64 ต้องเปลี่ยน `DOCKER_PLATFORM=linux/arm64`

ใช้สำหรับ NAS ที่ติดตั้ง **Container Manager** ได้ โดยมี CPU 64-bit x86_64 หรือ ARM64 ตรวจรุ่นและแพ็กเกจใน Package Center ก่อน หากไม่มี Container Manager ให้ตรวจรุ่น/DSM ก่อนเลือกวิธีอื่น ชุดนี้ยังไม่ได้ทดสอบบน Synology จริง

เอกสาร Synology: [Container Manager Project](https://kb.synology.com/en-global/DSM/help/ContainerManager/docker_project?version=7) รองรับการจัดการแอปผ่าน Compose

## 1. เตรียมโฟลเดอร์บน NAS

ติดตั้ง Container Manager จาก Package Center แล้วใช้ File Station สร้างโฟลเดอร์โปรเจกต์:

```text
/volume1/docker/telegram-model-backup/
  Dockerfile
  docker-compose.yml
  .dockerignore
  requirements.txt
  model_backup/
  .env
  state/
```

คัดลอกไฟล์โปรเจกต์จากชุดติดตั้งลงโฟลเดอร์นี้ รวมไฟล์ซ่อน `.dockerignore` ด้วย ไม่ต้องคัดลอก `.venv`, `__pycache__` หรือ `.env` ของเครื่องเดิม

สร้าง Shared Folder ชื่อ `AIModels` ใน Control Panel → Shared Folder เพื่อเก็บโมเดล ตัวอย่างใช้ `/volume1/AIModels` หากอยู่ volume อื่นให้เปลี่ยน path ตามจริง

## 2. ตั้งค่าบอตและสิทธิ์

คัดลอก `synology.env.example` เป็น `.env` ในโฟลเดอร์เดียวกับ `docker-compose.yml` แล้วแก้ค่า:

```dotenv
TELEGRAM_BOT_TOKEN=ใส่_token_จาก_BotFather
ALLOWED_USER_IDS=ใส่_user_id_ตัวเลขของคุณ
HF_TOKEN=
DOCKER_PLATFORM=linux/amd64
PUID=1026
PGID=100
NAS_BACKUP_DIR=/volume1/AIModels
NAS_STATE_DIR=/volume1/docker/telegram-model-backup/state
MAX_MODEL_GB=100
MIN_FREE_GB=5
HF_MAX_RETRIES=5
HF_RETRY_BASE_SECONDS=30
HF_RETRY_MAX_SECONDS=600
```

`PUID=1026` และ `PGID=100` เป็นตัวอย่าง ไม่ใช่ค่าของทุก NAS ต้องใช้ UID/GID ของบัญชี DSM ที่จะเขียนไฟล์ และให้บัญชีนั้นมีสิทธิ์อ่าน/เขียน **ทั้ง AIModels และ state/** ผ่าน File Station → Properties → Permission รวมสิทธิ์ผ่านโฟลเดอร์แม่

ถ้าใช้ SSH ตรวจ UID/GID ของบัญชีด้วย:

```sh
id ชื่อบัญชี_DSM
```

ใช้ตัวเลข `uid=` กับ `gid=` ที่ได้มาเป็น `PUID` และ `PGID` ไฟล์ `.env` ต้องอ่านได้โดยผู้ดูแลที่ใช้สร้างโปรเจกต์ และควรจำกัดสิทธิ์อ่านเพราะมี Bot Token ไม่ต้องส่ง Token มาทางแชต

`HF_TOKEN` ใช้เฉพาะโมเดล private/gated ที่บัญชีคุณมีสิทธิ์ ดาวน์โหลด public model ได้โดยเว้นว่าง ตั้ง `MAX_MODEL_GB`/`MIN_FREE_GB` ตามขนาดดิสก์ โดยหน่วยเป็น GiB

## 3. สร้าง Project

1. เปิด **Container Manager → Project → Create**
2. ตั้งชื่อ เช่น `telegram-model-backup`
3. เลือก Path `/volume1/docker/telegram-model-backup`
4. ใช้ไฟล์ `docker-compose.yml` ที่อยู่ในโฟลเดอร์นั้น (ชื่อเมนูอาจต่างกันตามเวอร์ชัน)
5. สั่ง Build และ Start Project; ครั้งแรกต้องดาวน์โหลด Python image และติดตั้ง dependencies
6. ดูสถานะ container เป็น Running และเปิด Log ตรวจข้อผิดพลาด

ไม่ต้องตั้ง Web Station, reverse proxy, port forwarding หรือพอร์ตเข้า บอตเชื่อม Telegram และ Hugging Face ออกผ่านอินเทอร์เน็ต NAS ต้องออก HTTPS และ DNS ได้

หากใช้ SSH สามารถสั่งจากโฟลเดอร์โปรเจกต์แทน UI ได้:

```sh
cd /volume1/docker/telegram-model-backup
sudo docker compose up -d --build
sudo docker compose logs --tail=100 -f
```

ถ้า DSM มีคำสั่งรุ่นเก่า ใช้ `sudo docker-compose` แทน `sudo docker compose`

หยุดบอตที่เครื่องเดิมก่อนเริ่ม NAS หากใช้ Bot Token เดียวกัน เพื่อไม่ให้ Telegram polling ชนกัน

## 4. ใช้งานผ่าน Telegram

เปิดแชตส่วนตัวกับบอต กด Start แล้วส่ง URL หน้าโมเดลหรือ `owner/model` โดยตรง ส่งหลายโมเดลในข้อความเดียวได้โดยแยกบรรทัดละหนึ่งโมเดลและผสม URL กับชื่อ repo ได้ บอตจะสรุปหมายเลขงาน ข้ามรายการซ้ำในข้อความเดียว และแจ้งรายการผิด/คิวเต็มโดยไม่ทิ้งรายการอื่นที่รับได้ คิวรวมสูงสุด 20 งานและดาวน์โหลดทีละงาน

```text
Qwen/Qwen2.5-0.5B-Instruct
/status
/models
/models 2
/disk
```

- `/status` แสดงงานกำลังทำ คิว และความคืบหน้าตามจำนวนไฟล์
- `/status 1` ดูรายละเอียดงาน รวมไฟล์ปัจจุบัน
- `/models` หรือ `/list` ดูโมเดลที่สำเร็จทั้งหมดแบบแบ่งหน้า พร้อมลิงก์กดเปิดหน้าโมเดลบน Hugging Face
- `/cancel 1` ยกเลิก และ `/retry 1` ลองใหม่

เมื่อเจอ HTTP 429 (Rate Limit) บอตจะรอแล้วลองใหม่อัตโนมัติสูงสุด 5 ครั้งต่อขั้นตอน/ไฟล์ ดูเวลารอและครั้งที่ลองได้จาก `/status 1` และยกเลิกได้ระหว่างรอ ถ้าครบแล้วยังไม่สำเร็จให้รอแล้วใช้ `/retry 1` ค่า `HF_RETRY_BASE_SECONDS`/`HF_RETRY_MAX_SECONDS` ควบคุม backoff เมื่อไม่มีเวลารอจากเซิร์ฟเวอร์ หากมี `Retry-After` หรือ RateLimit reset จะรออย่างน้อยตามที่เซิร์ฟเวอร์ระบุ แม้จะเกิน max ที่ตั้งไว้ ตัวแปรเหล่านี้มีค่าเริ่มต้นจึงไม่ต้องเพิ่มใน `.env` เดิมหากไม่ต้องการปรับ

ไฟล์อยู่ใน File Station ที่:

```text
/volume1/AIModels/owner/model/<commit>/job-1/
```

ตำแหน่งที่บอตแสดงเป็น path ภายใน container: `/data/models/...` ซึ่งตรงกับ `/volume1/AIModels/...` บน NAS

## 5. ตรวจไฟล์สำรอง

จาก SSH รันใน container ที่ทำงานอยู่ โดยใช้ path ตามที่ `/list` แสดง:

```sh
cd /volume1/docker/telegram-model-backup
sudo docker compose exec model-backup python -m model_backup.verify /data/models/owner/model/COMMIT/job-1
```

ไฟล์ `backup-manifest.json` เก็บ SHA-256 สำหรับตรวจความเสียหายของไฟล์ สำรองโฟลเดอร์โมเดลและ `state/` เพิ่มไปยังปลายทางอีกแห่งได้

## รีสตาร์ต อัปเดต และย้ายจากเครื่องเดิม

- Container ตั้ง `restart: unless-stopped` เพื่อเริ่มใหม่หลัง NAS reboot หากไม่ได้สั่ง Stop ไว้ด้วยตนเอง
- งานค้างที่บันทึกใน SQLite จะกลับเข้าคิวเมื่อบอตเปิดใหม่ ไฟล์โมเดลกับฐานข้อมูลเก็บนอก container จึงคงอยู่เมื่อ rebuild
- อัปเดตโดยหยุด Project, คัดลอกโค้ดใหม่ แล้วสั่ง Build/Rebuild และ Start โดยคง `.env`, `state/` และโฟลเดอร์โมเดลไว้
- หากย้ายเฉพาะโค้ด จะไม่มีประวัติงานจากเครื่องเดิม ถ้าต้องการย้ายคิว/ประวัติด้วย ให้หยุดบอตเดิมก่อน คัดลอกทั้ง `state/` และไฟล์โมเดล แล้วปรับสิทธิ์บน NAS รายการเก่าอาจเตือนว่าไม่พบ manifest เพราะ path ในฐานข้อมูลยังเป็น path เครื่องเดิม แต่ข้อมูลไฟล์ยังอยู่และตรวจผ่าน verify ด้วย path ใหม่ได้

## แก้ปัญหา

| อาการ | ตรวจสอบ |
| --- | --- |
| ไม่มี Container Manager ใน Package Center | ตรวจรุ่น NAS และเวอร์ชัน DSM ว่ารองรับแพ็กเกจหรือไม่ |
| Container restart วน | เปิด Log ตรวจ Token, ALLOWED_USER_IDS และสิทธิ์โฟลเดอร์ |
| Permission denied: /app/model_backup/bot.py | อัปเดต Dockerfile แล้ว Build Project ใหม่ Dockerfile รุ่นปัจจุบันตั้งโฟลเดอร์โค้ดเป็น 755 และไฟล์เป็น 644 ภายใน image เพื่อให้ UID 1026:100 อ่านได้ แม้ไฟล์ต้นทางบน NAS จะเป็น 700/600 |
| Permission denied / cannot open database | ตรวจ PUID/PGID, สิทธิ์อ่านเขียน AIModels และ state รวมโฟลเดอร์แม่ |
| Telegram Conflict | หยุดบอตอีก instance ที่ใช้ Token เดียวกัน |
| No matching manifest / no matching distribution | ตรวจว่า NAS เป็น CPU 64-bit และส่งรุ่น NAS/DSM เพื่อปรับ image/dependencies |
| โมเดล private/gated โหลดไม่ได้ | ตรวจ HF_TOKEN และสิทธิ์เข้า repo/การยอมรับเงื่อนไข |
| ไม่พบโมเดล | ตรวจชื่อ owner/model หรือ URL อาจถูกลบ เปลี่ยนชื่อ หรือถูกซ่อนเป็น private |
| ไม่พบโมเดลหรือไม่มีสิทธิ์เข้าถึง | Hugging Face ตอบ 401 ซึ่งแยกชื่อที่ไม่มีจาก private ไม่ได้ ให้ตรวจชื่อและ HF_TOKEN |
| ไม่พบ revision | ตรวจชื่อ branch, tag หรือ commit แล้วส่งคำสั่งใหม่ |
| พื้นที่ไม่พอ | ตรวจ `/disk`, MAX_MODEL_GB และ MIN_FREE_GB |

รายละเอียด Compose: [Docker services reference](https://docs.docker.com/reference/compose-file/services/)
