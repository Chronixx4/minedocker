# ZimaOS-Installation (App-Import) — mc-dashboard

Kurzweg: ZimaOS-UI → Apps → benutzerdefinierte App installieren →
Docker-Compose-Import → die YAML unten einfügen. Der SSH-/Build-Weg ist in
`SERVER-SETUP.md` §11 (Weg 1) beschrieben; dieser Import hier nutzt das
fertige Image `ghcr.io/chronixx4/minedocker:latest`, das GitHub Actions bei
jedem Push auf `main` baut (Workflow `docker-publish.yml`).

**Wichtig (einmalig):** Nach dem ersten Push ist das GHCR-Package privat —
auf GitHub unter „Packages" → `minedocker` → Package settings →
Visibility **Public** stellen, sonst kann ZimaOS ohne Login nicht ziehen.

## Vor dem Import anpassen

- **DOCKER_GID (`group_add`):** Default 999. Auf dem ZimaOS-Gerät ermitteln
  mit `stat -c '%g' /var/run/docker.sock` (Docker Desktop/WSL2: 0). Falscher
  Wert = Dashboard meldet „Permission denied" beim Socket-Zugriff.
- **DASHBOARD_API_KEY:** setzen (z. B. `openssl rand -hex 32`) — schützt
  alle API-Routen außer `/api/health`. Leer = alle Routen offen (nur LAN ok).
- **TZ:** für Scheduler-Zeiten (täglicher Neustart, geplante Backups)
  entkommentieren, sonst gilt UTC.
- **Port 8080 belegt?** Host-Seite des `ports`-Eintrags ändern.
- Instanz-Ports 25570+ veröffentlicht das Dashboard selbst auf dem Host.

## docker-compose.yml (ZimaOS-Import)

```yaml
services:
  init:
    image: alpine:3.20
    user: "0:0"
    restart: on-failure
    volumes:
      - /DATA/AppData/mc-dashboard/data:/data
    command: |
      uid=$$(stat -c %u /data) || true
      [ "$$uid" = "1000" ] && exit 0
      if [ -n "$$(ls -A /data 2>/dev/null)" ] && [ ! -d /data/instances ]; then
        echo "FATAL: /data enthaelt Daten ohne instances/ - Pfad zeigt vermutlich auf ein fremdes Verzeichnis. Kein chown, Abbruch." >&2
        exit 1
      fi
      chown -R 1000:1000 /data
    mem_limit: 64m
    cpus: 0.25

  dashboard:
    image: ghcr.io/chronixx4/minedocker:latest
    container_name: mc-dashboard
    restart: unless-stopped
    depends_on:
      init:
        condition: service_completed_successfully
    environment:
      DASHBOARD_API_KEY: ""     # setzen! z. B. `openssl rand -hex 32`
      CORS_ORIGINS: "*"
      CF_API_KEY: ""            # CurseForge-API-Key (console.curseforge.com)
      INSTANCES_DIR: "/data/instances"
      INSTANCES_PORT_BASE: "25570"
      INSTANCES_MEMORY: "2G"
      INSTANCES_HOST_DIR: ""
      ALERT_WEBHOOK_URL: ""
      TELEGRAM_BOT_TOKEN: ""
      TELEGRAM_CHAT_ID: ""
      ALERT_EVENTS: "crash,start,stop"
      # TZ: "Europe/Berlin"
    ports:
      - "8080:8080"
    volumes:
      - /DATA/AppData/mc-dashboard/data:/data
      - /var/run/docker.sock:/var/run/docker.sock
    user: "1000:1000"
    group_add:
      - "999"                   # DOCKER_GID (stat -c '%g' /var/run/docker.sock)
    mem_limit: 1g
    cpus: 1.0
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"

  backup:
    image: alpine:3.20
    container_name: mc-backup
    restart: unless-stopped
    volumes:
      - /DATA/AppData/mc-dashboard/data:/data:ro
      - /DATA/AppData/mc-dashboard/backups:/backups
    command: >
      sh -c "while true; do
               ts=$$(date +%F-%H%M);
               tar -czf /backups/mcdata-$$ts.tgz -C /data . &&
               ln -sf mcdata-$$ts.tgz /backups/latest.tgz &&
               echo backup mcdata-$$ts.tgz ok;
               find /backups -name 'mcdata-*.tgz' -mtime +14 -delete;
               sleep 6h;
             done"
    mem_limit: 128m
    cpus: 0.25
    logging:
      driver: json-file
      options:
        max-size: "1m"
        max-file: "3"
```

## Nach der Installation

- App öffnen → `http://<zima-ip>:8080` → Tab „Server" → Instanz erstellen.
- Der `init`-Service setzt bei leerem/neuem `/data` die Ownership
  (1000:1000); ein FATAL-Abbruch bedeutet, dass der Datenpfad auf ein
  fremdes Verzeichnis zeigt (Diagnose: `docker compose logs init`).
- Backups landen alle 6 h in `/DATA/AppData/mc-dashboard/backups`
  (14 Tage Retention, `latest.tgz` = neuester Snapshot); Restore-Befehle:
  `SERVER-SETUP.md` §5.
