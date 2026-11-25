# M-08: pin base image digest for reproducibility.
#
# This must be the multi-platform INDEX digest, not one platform's image.
# The previous pin (sha256:f0c6bc1a..., Python 3.12.2 on Debian bookworm,
# built 2024-02) was a single amd64 manifest: it could not build for arm64,
# and it froze the base two years behind on OS and Python security fixes.
#
# Pinned 2026-11-25: python:3.12-slim = 3.12.16 on Debian trixie.
# To refresh: docker buildx imagetools inspect python:3.12-slim
#             (use the top-level "Digest:", whose MediaType is an index).
FROM python:3.12-slim@sha256:191083cdf6c7bda8c76158983163ead5d5cf7b77cf3033e805bb0f5296f51b4b

# Create a non-root user for the app
RUN groupadd -r inspectarr && useradd -r -g inspectarr -d /app inspectarr

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# data/ holds inspectarr.db and inspectarr.log.json — mount this as a volume
VOLUME ["/app/data"]

# M-09: restrict chown to /app/data only — source code stays root-owned
RUN mkdir -p /app/data && chown -R inspectarr:inspectarr /app/data

USER inspectarr

# Healthcheck probes /api/health, which reports process, database and
# scheduler state and returns 503 when a core component has failed.
# It must NOT point at "/" -- that route is behind basic auth, so an
# auth-enabled deployment would report unhealthy and be restarted forever.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8585/api/health')" || exit 1

# Web UI + built-in scheduler daemon
EXPOSE 8585

# Default entry point: web UI with the scheduler.
# For a one-off CLI scan instead, run:
#   docker exec <container> python inspectarr.py --dry-run
CMD ["python", "web.py"]
