# syntax=docker/dockerfile:1
# VoiceOps Controller — production-ready multi-stage image.
#
# Stage 1 (builder) installs all Python dependencies into a dedicated virtual environment.
# Stage 2 (runtime) copies only that venv and the application code, drops to a non-root
# user, and exposes the FastAPI service on port 8000.

FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

COPY requirements.txt .
RUN python -m venv /opt/venv && \
    /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY app/ ./app/

RUN groupadd --system voiceops && \
    useradd --system --gid voiceops --no-create-home --shell /usr/sbin/nologin voiceops && \
    chown -R voiceops:voiceops /app

USER voiceops

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).status == 200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
