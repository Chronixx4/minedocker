# Produktionsimage: schlank, nicht-privilegiert (Default), mit Healthcheck
FROM python:3.12-slim

LABEL org.opencontainers.image.title="mc-dashboard" \
      org.opencontainers.image.description="Web-Dashboard zur Verwaltung von Minecraft-Server-Instanzen (Docker)" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.source="https://github.com/local/minedocker"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY static ./static

# UID/GID 1000 = Standard-Benutzer im itzg/minecraft-server-Image,
# damit die Zugriffsrechte auf das gemeinsame /data-Volume passen.
RUN useradd --uid 1000 --create-home dashboard \
    && mkdir -p /data \
    && chown -R 1000:1000 /data /app

USER 1000:1000

ENV MODS_DIR=/data/mods \
    DASHBOARD_HOST=0.0.0.0 \
    DASHBOARD_PORT=8080

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=4)"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
