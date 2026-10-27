FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HOME=/tmp \
    BACKUP_DIR=/data/models \
    STATE_DIR=/data/state \
    HF_HOME=/data/state/huggingface

WORKDIR /app
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt
COPY model_backup/ ./model_backup/
# NAS uploads can leave source directories at 0700 and files at 0600.
# Normalize image permissions before switching to the runtime UID/GID.
RUN chmod 755 /app \
    && find /app/model_backup -type d -exec chmod 755 {} + \
    && find /app/model_backup -type f -exec chmod 644 {} +

USER 1026:100
CMD ["python", "-m", "model_backup.bot"]
