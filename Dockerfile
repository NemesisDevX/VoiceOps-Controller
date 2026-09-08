# syntax=docker/dockerfile:1
# VoiceOps Controller — production image.
# Lean, non-root, slim Python 3.12 base. Only the FastAPI app package is copied in;
# tests/, scripts/, and dev tooling are intentionally excluded from the image.

FROM python:3.12-slim

# Keep Python from writing .pyc files / buffering stdout, and keep pip quiet & cache-free.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install dependencies first so this layer is cached across app code changes.
# requirements.txt is installed in full for simplicity (pytest/httpx are lightweight
# and not a meaningful size/security cost); no compiler toolchain is required.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Only the application package is needed at runtime (backend + static HUD assets).
COPY app/ ./app/

# Create an unprivileged, home-less system user and drop root before serving traffic.
RUN groupadd --system voiceops && \
    useradd --system --gid voiceops --no-create-home --shell /usr/sbin/nologin voiceops && \
    chown -R voiceops:voiceops /app
USER voiceops

EXPOSE 8000

# Relies on GET /health (see app/api/v1/endpoints.py) for liveness.
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).status == 200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
