FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FUND_DB_PATH=/data/fund.db \
    FUND_UPLOAD_DIR=/data/uploads

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY run.py .

RUN mkdir -p /data/uploads
VOLUME ["/data"]

EXPOSE 8000

# 单 worker + 多线程：SQLite 单文件配合 WAL，避免多进程写冲突
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "1", "--threads", "8", "--timeout", "120", "run:app"]
