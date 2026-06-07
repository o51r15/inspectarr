FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# data/ holds inspectarr.db and inspectarr.log.json — mount this as a volume
VOLUME ["/app/data"]

# Default: single scan run. Pair with a cron container or systemd timer
# for scheduled execution. --daemon loop mode coming in v2.
CMD ["python", "watchdog.py"]
