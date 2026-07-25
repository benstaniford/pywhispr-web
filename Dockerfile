# Multi-stage build: Builder stage
FROM python:3.11-slim AS builder

# Set working directory
WORKDIR /app

# Install build dependencies for compiling Python packages
RUN apt-get update && apt-get install -y \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

# Multi-stage build: Runtime stage
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# No additional runtime dependencies needed for basic Flask app

# Copy Python packages from builder stage
COPY --from=builder /root/.local /usr/local

# Copy application code
COPY app.py .
COPY pywhispr_client.py .
COPY tls_certs.py .
COPY run.py .
COPY gunicorn.conf.py .
COPY templates/ templates/
COPY static/ static/

ARG APP_VERSION=dev
ENV APP_VERSION=${APP_VERSION}

# The server list and the cached liveness decision live here, shared by every
# Gunicorn worker. Mount a volume over it to keep configuration across upgrades.
ENV PYWHISPR_CONFIG_PATH=/data/config.json

# The self-signed CA lives on the same volume, so it survives upgrades and each
# device only ever has to trust it once. Losing it means re-trusting everywhere.
ENV PYWHISPR_CERT_DIR=/data/certs

# Create a non-root user for security
RUN adduser --disabled-password --gecos '' appuser && \
    mkdir -p /data && \
    chown -R appuser:appuser /app /data

VOLUME /data

# Switch to non-root user
USER appuser

# 5000 plain, 5443 TLS. Phones need 5443 for the microphone; 5000 is how you
# fetch the CA certificate to trust before that will work.
EXPOSE 5000 5443

# Health check. Stays on plain HTTP deliberately: it needs no certificate and no
# --insecure, and run.py exits if either listener dies, so this still catches a
# broken HTTPS worker.
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD python -c "import requests; r=requests.get('http://localhost:5000/health', timeout=5); exit(0 if r.status_code == 200 else 1)" || exit 1

# Generates the certificates, then runs one Gunicorn master per scheme
CMD ["python", "run.py"]
