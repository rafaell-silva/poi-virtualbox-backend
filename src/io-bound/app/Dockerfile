FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    WORKERS=1 \
    IO_DIR=/data/io

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

# Roda como usuario nao-root (UID 1000 compativel com usuario ubuntu na VM)
RUN useradd -m -u 1000 appuser && mkdir -p /data/io && chown -R appuser:appuser /data
USER appuser

EXPOSE 8000

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port 8000 --workers ${WORKERS:-1} --proxy-headers --forwarded-allow-ips=*"]
