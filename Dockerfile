# M-08: pin base image digest for reproducibility
FROM python:3.12-slim@sha256:f0c6bc1ab7b1ab270bbb612a31a67a7938d6171183ddce9121f04984ab3df44e

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

# M-08: healthcheck ensures container reports healthy status
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8585/')" || exit 1

# Web UI + built-in scheduler daemon
EXPOSE 8585

# Default entry point: web UI with the scheduler.
# For a one-off CLI scan instead, run:
#   docker exec <container> python inspectarr.py --dry-run
CMD ["python", "web.py"]
