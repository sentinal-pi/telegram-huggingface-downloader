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

USER 1026:100
CMD ["python", "-m", "model_backup.bot"]
