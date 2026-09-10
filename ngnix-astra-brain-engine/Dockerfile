# Cloud edition backend.
#
# Vercel cannot host this: its functions are request/response and do not support
# WebSockets, and every conversation here runs over /ws/session with streamed
# binary PCM. This image is for a host that keeps a process alive and proxies
# WebSockets — Azure Container Apps, Azure App Service (Web App for Containers),
# Fly.io, Render or Railway all work.
#
# Only the cloud edition's dependencies are installed. The offline engines
# (faster-whisper, piper-tts, onnxruntime) are deliberately absent, which keeps
# the image small.
#
#   docker build -t tars-brain ./ngnix-astra-brain-engine
#   docker run -p 8000:8000 --env-file ngnix-astra-brain-engine/.env tars-brain

FROM python:3.12-slim

# Fail fast and log immediately rather than buffering behind the platform.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY brain ./brain
COPY seed_corpus.jsonl ./
COPY docker-entrypoint.sh ./
RUN chmod +x docker-entrypoint.sh

# Run as a non-root user. The corpus is written at startup, so the working
# directory has to be writable by that user.
RUN useradd --create-home --uid 10001 tars && chown -R tars:tars /app
USER tars

EXPOSE 8000

# The platform supplies PORT on most hosts; 8000 is the local default.
ENV PORT=8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD python -c "import os,urllib.request;urllib.request.urlopen(f'http://127.0.0.1:{os.getenv(\"PORT\",\"8000\")}/health',timeout=4)"

ENTRYPOINT ["./docker-entrypoint.sh"]
