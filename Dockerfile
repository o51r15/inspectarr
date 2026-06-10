FROM python:3.12-slim
LABEL org.opencontainers.image.source="https://github.com/o51r15/inspectarr"
LABEL org.opencontainers.image.description="Torrent watchdog for *arr ecosystems"
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# data/ holds inspectarr.db and inspectarr.log.json — mount this as a volume
VOLUME ["/app/data"]

# Web UI + built-in scheduler daemon
EXPOSE 8585

# Default entry point: web UI with the scheduler.
# For a one-off CLI scan instead, run:
#   docker exec <container> python inspectarr.py --dry-run
CMD ["python", "web.py"]
