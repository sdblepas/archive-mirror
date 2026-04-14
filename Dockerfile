# ─── Build stage ────────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends gcc \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --upgrade pip \
 && pip install --prefix=/install --no-cache-dir -r requirements.txt


# ─── Runtime stage ──────────────────────────────────────────────────────────
FROM python:3.12-slim

LABEL org.opencontainers.image.title="archive-mirror" \
      org.opencontainers.image.description="Continuous mirror of an Internet Archive collection" \
      org.opencontainers.image.source="https://github.com/sdblepas/archive-mirror"

RUN useradd --system --uid 1000 --create-home mirror

WORKDIR /app

COPY --from=builder /install /usr/local
COPY src/ ./src/

RUN mkdir -p /data/music /data/state \
 && chown -R mirror:mirror /data

USER mirror

# Healthcheck reads HEALTH_PORT / WEB_PORT from the environment so it works
# regardless of which port the operator configures.
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD python -c "\
import urllib.request, sys, os; \
port = os.getenv('WEB_PORT', os.getenv('HEALTH_PORT', '6547')); \
r = urllib.request.urlopen(f'http://localhost:{port}/health', timeout=5); \
sys.exit(0 if r.status == 200 else 1)"

EXPOSE 6547

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

CMD ["python", "-m", "src.main"]
