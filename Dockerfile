FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    CRASH_DIGEST_CONFIG=/app/config.yaml

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY config.yaml .
COPY src ./src

# Cloud Run job default: all enabled products. Override args per job if needed.
ENTRYPOINT ["python", "-m", "crash_digest"]
