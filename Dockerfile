FROM python:3.12-slim

# Create a non-root user for the app
RUN groupadd -r inspectarr && useradd -r -g inspectarr -d /app inspectarr

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# data/ holds inspectarr.db and inspectarr.log.json — mount this as a volume
VOLUME ["/app/data"]

# Ensure the non-root user can write to data/
RUN mkdir -p /app/data && chown -R inspectarr:inspectarr /app

USER inspectarr

# Web UI + built-in scheduler daemon
EXPOSE 8585

# Default entry point: web UI with the scheduler.
# For a one-off CLI scan instead, run:
#   docker exec <container> python inspectarr.py --dry-run
CMD ["python", "web.py"]
