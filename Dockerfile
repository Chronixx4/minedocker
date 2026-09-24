# Produktionsimage: schlank, nicht-privilegiert (Default), mit Healthcheck
FROM python:3.12-slim

LABEL org.opencontainers.image.title="mc-dashboard" \
      org.opencontainers.image.description="Web-Dashboard zur Verwaltung von Minecraft-Server-Instanzen (Docker)" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.source="https://github.com/chronixx4/minedocker"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Angezeigte Version im Dashboard (Release-Workflow setzt das Build-Arg,
# z. B. APP_VERSION=v1.7.5); Default für lokale Builds: dev
ARG APP_VERSION=dev
ENV APP_VERSION=${APP_VERSION}

WORKDIR /app

# tzdata: ohne das Paket hätte TZ (z. B. Europe/Berlin für Scheduler-Zeiten)
# keine Wirkung — slim-Images liefern keine Zoneinfo-Dateien mit. Vor den
# COPY-Schritten platziert, bleibt der Layer über Code-Änderungen hinweg
# gecacht (reläuft nur bei Wechsel des Base-Images).
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY static ./static

# UID/GID 1000 = Standard-Benutzer im itzg/minecraft-server-Image,
# damit die Zugriffsrechte auf das gemeinsame /data-Volume passen.
# /app bleibt root-eigentümlich und nur lesend (UID 1000 kann lesen:
# Default-Rechte 0644/0755) — der App-User braucht keine Schreibrechte
# im Code-Verzeichnis, alle Laufzeitdaten liegen unter /data.
RUN useradd --uid 1000 --create-home dashboard \
    && mkdir -p /data \
    && chown 1000:1000 /data

USER 1000:1000

ENV MODS_DIR=/data/mods \
    DASHBOARD_HOST=0.0.0.0 \
    DASHBOARD_PORT=8080

EXPOSE 8080

# start-period 30 s: beim Kaltstart lädt die App gespiegelte Jobs und
# History-Datenbank; langsame Hosts (NAS/ZimaOS) dürfen dafür Zeit brauchen.
# Fehlversuche in der Startphase zählen nicht als "unhealthy".
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=4)"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
