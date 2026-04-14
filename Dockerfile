# ─── Build stage ────────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build

# Install only what's needed to compile wheels (mutagen is pure Python,
# but httpx needs hpack if http2 is used)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --upgrade pip \
 && pip install --prefix=/install --no-cache-dir -r requirements.txt


# ─── Runtime stage ──────────────────────────────────────────────────────────
FROM python:3.12-slim

LABEL org.opencontainers.image.title="archive-mirror" \
      org.opencontainers.image.description="Continuous mirror of an Internet Archive collection" \
      org.opencontainers.image.source="https://github.com/your-org/archive-mirror"

# Create a non-root user for the service
RUN useradd --system --uid 1000 --create-home mirror

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy source
COPY src/ ./src/

# Create data directories owned by the service user
RUN mkdir -p /data/music /data/state \
 && chown -R mirror:mirror /data

USER mirror

# Health check via the built-in HTTP endpoint
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python - <<'EOF'
import urllib.request, sys
try:
    r = urllib.request.urlopen("http://localhost:8080/health", timeout=5)
    sys.exit(0 if r.status == 200 else 1)
except Exception:
    sys.exit(1)
EOF

EXPOSE 8080

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

CMD ["python", "-m", "src.main"]
