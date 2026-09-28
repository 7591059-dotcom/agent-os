FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    APP_DATA_DIR=/data

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --gid 10001 agentos \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin agentos \
    && mkdir /data \
    && chown 10001:10001 /data \
    && chmod 0700 /data

COPY app /app/app
COPY scripts /app/scripts
USER 10001:10001
EXPOSE 8000
STOPSIGNAL SIGTERM

# A single process owns the durable dispatcher, Telegram polling and SQLite lock.
# VPS Compose routes through Caddy; local Compose overrides proxy trust and binds loopback.
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers", "--forwarded-allow-ips", "*", "--no-access-log", "--timeout-graceful-shutdown", "40"]
