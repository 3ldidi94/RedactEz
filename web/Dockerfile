# Build context is the repo root (see docker-compose.yml), so both the web layer
# and the redactez package are available to COPY.
FROM python:3.12-slim

# Tesseract for scanned documents (French + English language data).
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr tesseract-ocr-fra tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /app/requirements.txt
COPY web/requirements.txt /app/requirements-web.txt
RUN pip install --no-cache-dir -r /app/requirements.txt -r /app/requirements-web.txt

COPY redactez /app/redactez
COPY web /app/web

WORKDIR /app/web

# BIND=127.0.0.1 by default: with network_mode host, only the local reverse
# proxy reaches the service. Use BIND=0.0.0.0 only on a bridge network with
# port mapping (see docker-compose.yml).
ENV PORT=8020
ENV BIND=127.0.0.1
# Temp files on a tmpfs (see docker-compose.yml): documents never touch disk.
ENV TMPDIR=/tmp
EXPOSE 8020

# Unprivileged user.
RUN useradd --system --no-create-home redactez
USER redactez

# 2 workers; long timeout because OCR costs about 1 s per page (200 pages max).
CMD ["sh", "-c", "exec gunicorn --bind ${BIND}:${PORT} --workers 2 \
     --timeout 600 --access-logfile - --error-logfile - webapp:app"]
