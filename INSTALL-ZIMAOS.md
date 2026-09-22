# ZimaOS-Installation (App-Import) — mc-dashboard

Kurzweg: ZimaOS-UI → Apps → benutzerdefinierte App installieren →
Docker-Compose-Import → die YAML unten einfügen. Der SSH-/Build-Weg ist in
`SERVER-SETUP.md` §11 (Weg 1) beschrieben; dieser Import hier nutzt das
fertige Image `ghcr.io/chronixx4/minedocker:latest`, das GitHub Actions bei
jedem Push auf `main` baut (Workflow `docker-publish.yml`).

**Wichtig (einmalig):** Nach dem ersten Push ist das GHCR-Package privat —
auf GitHub unter „Packages" → `minedocker` → Package settings →
Visibility **Public** stellen, sonst kann ZimaOS ohne Login nicht ziehen.

## Warum diese YAML anders ist als docker-compose.yml

Der ZimaOS-App-Import hat zwei Eigenheiten (empirisch bestätigt):
1. Er legt Services mit `depends_on: service_completed_successfully` nicht
   sauber an — der init-Ownership-Service fehlt dann, und der Dashboard-
   Container bleibt ewig auf Status „Created" stehen. Deshalb enthält diese
   YAML **keinen** init-Service; die Ownership wird einmalig per SSH gesetzt
   (siehe unten).
2. Er verschluckt `$$`-Escapes in Compose-Commands (Backups hießen dann
   `mcdata-.tgz` mit leerem Zeitstempel). Deshalb kommt diese YAML komplett
   **ohne `$`-Zeichen** aus: Das Backup rotiert per Umbenennungskette
   (14 Generationen à 24 h = 14 Tage Retention, `latest.tgz` = neuester
   Snapshot) statt per `$(date ...)`.

## Einmalig per SSH vorbereiten

```bash
ssh admin@<zima-ip>
sudo mkdir -p /DATA/AppData/mc-dashboard/data /DATA/AppData/mc-dashboard/backups
sudo chown -R 1000:1000 /DATA/AppData/mc-dashboard/data /DATA/AppData/mc-dashboard/backups
sudo stat -c '%g' /var/run/docker.sock   # Ausgabe notieren
```

- Die Ausgabe des `stat`-Befehls ist die Docker-Socket-GID. Ist sie **nicht
  999**, trage sie unten in `group_add` ein (Falscher Wert = Dashboard meldet
  „Permission denied", Start/Stop und Ressourcen-Karte bleiben leer).
- Der `chown` ist idempotent und richtet auch bestehende Instanzdaten ein;
  bestehende `mc-inst-*`-Container und die Daten unter
  `/DATA/AppData/mc-dashboard/data` bleiben erhalten und werden vom
  Dashboard wiederverwendet.

## Vor dem Import anpassen

- **DASHBOARD_API_KEY:** setzen (z. B. `openssl rand -hex 32`) — schützt
  alle API-Routen außer `/api/health`. Leer = alle Routen offen (nur LAN ok).
- **CF_API_KEY:** CurseForge-API-Key (console.curseforge.com), optional.
- **TZ:** für Scheduler-Zeiten (täglicher Neustart, geplante Backups)
  entkommentieren, sonst gilt UTC.
- **Port 8080 belegt?** Host-Seite des `ports`-Eintrags ändern.
- Instanz-Ports 25570+ veröffentlicht das Dashboard selbst auf dem Host.

## docker-compose.yml (ZimaOS-Import)

```yaml
services:
  dashboard:
    image: ghcr.io/chronixx4/minedocker:latest
    container_name: mc-dashboard
    restart: unless-stopped
    # Updates: Ein reines Neustarten zieht KEIN neues Image (Container sind an
    # ihre Image-ID gebunden). pull_policy: always sorgt dafür, dass
    # `docker compose up -d` vorher das neue latest-Image zieht und den
    # Container neu anlegt. Zuverlässig per SSH:
    #   sudo docker compose pull dashboard && sudo docker compose up -d dashboard
    pull_policy: always
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
      - "999"                   # Docker-Socket-GID (siehe stat-Befehl oben)
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
               mv /backups/13.tgz /backups/14.tgz 2>/dev/null;
               mv /backups/12.tgz /backups/13.tgz 2>/dev/null;
               mv /backups/11.tgz /backups/12.tgz 2>/dev/null;
               mv /backups/10.tgz /backups/11.tgz 2>/dev/null;
               mv /backups/9.tgz /backups/10.tgz 2>/dev/null;
               mv /backups/8.tgz /backups/9.tgz 2>/dev/null;
               mv /backups/7.tgz /backups/8.tgz 2>/dev/null;
               mv /backups/6.tgz /backups/7.tgz 2>/dev/null;
               mv /backups/5.tgz /backups/6.tgz 2>/dev/null;
               mv /backups/4.tgz /backups/5.tgz 2>/dev/null;
               mv /backups/3.tgz /backups/4.tgz 2>/dev/null;
               mv /backups/2.tgz /backups/3.tgz 2>/dev/null;
               mv /backups/1.tgz /backups/2.tgz 2>/dev/null;
               tar -czf /backups/1.tgz -C /data .;
               ln -sf 1.tgz /backups/latest.tgz;
               echo backup 1.tgz ok;
               sleep 24h;
             done"
    mem_limit: 128m
    cpus: 0.25
    logging:
      driver: json-file
      options:
        max-size: "1m"
        max-file: "3"
```

## App ersetzen und starten

1. Alte App in ZimaOS entfernen — dabei **Daten behalten** (die gemappten
   Ordner unter `/DATA/AppData/mc-dashboard` überleben das laut
   ZimaOS-Doku; falls der Löschdialog fragt: Daten nicht löschen).
2. Neue App mit der YAML oben importieren und starten.
3. Falls der Import das Port-Mapping (8080→8080) nicht übernimmt: in den
   App-Einstellungen ergänzen.

## Updates einspielen

Ein reines Neustarten (auch ZimaOS „App neustarten") **zieht kein neues
Image** — Container sind an die Image-ID ihrer Erstellung gebunden, und
`docker compose up -d` nutzt lokal vorhandene Images ohne Pull. Der Weg, der
immer funktioniert (der App-Ordner unter `/DATA/AppData/...` enthält nur die
Daten-Binds; die Compose-Definition verwaltet ZimaOS intern):

```bash
sudo docker pull ghcr.io/chronixx4/minedocker:latest
sudo docker stop mc-dashboard
sudo docker rm mc-dashboard
```

Danach in ZimaOS die App **starten** — ZimaOS legt den Container aus seiner
gespeicherten App-Definition neu an und nimmt dabei das frisch gezogene
Image (das Tag `latest` zeigt nach dem Pull auf das neue Image).

Kontrolle: `sudo docker ps --filter name=mc-dashboard` (frischer „Up vor …") —
im Dashboard erscheint die neue Funktionalität (z. B. die Box „JVM & RAM").
Wer einen eigenen Compose-Speicherort nutzt, kann stattdessen im jeweiligen
Ordner `sudo docker compose pull && sudo docker compose up -d` fahren.

Die Release-Notes nennen immer das passende Image-Tag
(`ghcr.io/chronixx4/minedocker:<version>`); `latest` folgt `main`.

## Nach der Installation

- App öffnen → `http://<zima-ip>:8080` → Tab „Server" → Instanz erstellen
  (bzw. bestehende Instanzen starten).
- Startet das Dashboard nicht: `docker logs mc-dashboard --tail 50`
  (per SSH, ggf. mit `sudo`).
- **RCON-Fehler „[Errno -2] Name or service not known":** Das Dashboard
  hängt nur am Default-Bridge (ZimaOS-Import ohne Compose-Netzwerk) — dort
  löst Docker keine Container-Namen auf. Ab Image-Stand mit Auto-Heilung
  legt das Dashboard selbst `mc-dashboard-net` an und startet Instanzen
  darin (einmal Instanz stoppen/starten genügt). Manuell per SSH:
  ```bash
  sudo docker network create mc-dashboard-net
  sudo docker network connect mc-dashboard-net mc-dashboard
  ```
  und die Instanz im Dashboard neu starten (nicht per `docker start` —
  sonst bleibt der Container auf der falschen Bridge).
- Restore-Befehle: `SERVER-SETUP.md` §5 (hier `1.tgz`/`latest.tgz` aus
  `/DATA/AppData/mc-dashboard/backups` verwenden).
