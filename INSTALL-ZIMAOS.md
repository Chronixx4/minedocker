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
- **CF_API_KEY:** CurseForge-API-Key (console.curseforge.com), optional —
  ohne Key funktionieren Modrinth-Suche/-Installation und der
  Modpack-Upload (CF-.zip) weiterhin (Details: SERVER-SETUP.md §2).
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
      CF_API_KEY: ""            # optional — ohne Key: Modrinth + Modpack-Upload ok
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

## Optional: NVIDIA-GPU für einzelne Minecraft-Instanzen

Das Dashboard kann einem einzelnen Minecraft-Container eine NVIDIA-GPU
freigeben (Einstellungen der Instanz → „NVIDIA-GPU"). Gedacht ist das z. B.
für das OpenCL-Zusatzmodul von C2ME. Die Freigabe gilt **pro Instanz**: das
Dashboard startet den Container mit `runtime="nvidia"`,
`NVIDIA_VISIBLE_DEVICES=<GPU-ID>` und `NVIDIA_DRIVER_CAPABILITIES=compute,utility`.
Es wird weder die Docker-Standard-Runtime geändert noch `privileged`
verwendet. Eine GPU-Freigabe am Dashboard-Service in der Compose-Datei ist
**nicht** nötig und hilft den Instanzen auch nicht.

### 1. Voraussetzungen auf dem Host (einmalig, per SSH)

```bash
nvidia-smi                                  # Treiber läuft, GPU sichtbar
sudo docker info | grep -i runtimes         # Liste muss "nvidia" enthalten
```

Fehlt `nvidia` in der Runtime-Liste, ist auf ZimaOS der NVIDIA-Treiber bzw.
das Container-Toolkit nicht eingerichtet (in ZimaOS über den
NVIDIA-Treiber-Eintrag im App Store / die ZimaOS-Hardware-Einstellungen).
Ohne die Runtime startet eine Instanz mit aktivierter GPU **nicht** und das
Dashboard meldet das (es wird nie stillschweigend ohne GPU gestartet).

Test mit dem Minecraft-Image, unabhängig vom Dashboard:

```bash
sudo docker run --rm --runtime=nvidia \
  -e NVIDIA_VISIBLE_DEVICES=0 \
  -e NVIDIA_DRIVER_CAPABILITIES=compute,utility \
  --entrypoint nvidia-smi itzg/minecraft-server:java21
```

### 2. Im Dashboard aktivieren

1. Instanz öffnen → Reiter „Einstellungen" → Kasten „NVIDIA-GPU".
2. „NVIDIA-GPU aktivieren" anhaken, GPU-ID eintragen (Standard `0`; Index,
   Liste wie `0,1` oder UUID `GPU-…`; `nvidia-smi -L` zeigt sie), speichern.
3. Die Änderung wirkt erst nach **Stoppen und erneutem Starten** der Instanz.
   Laufende Server werden nicht automatisch neu gestartet.

Standard ist „aus"; bestehende Instanzen ohne gespeicherte GPU-Einstellung
starten unverändert ohne GPU. Beim Klonen einer Instanz wird die Einstellung
übernommen.

### 3. Prüfung im echten Minecraft-Container

Nach dem Start der Instanz (Container-Name: `docker ps --filter label=mc-dashboard.managed=true`):

```bash
sudo docker exec <container> nvidia-smi
sudo docker inspect <container> --format '{{.HostConfig.Runtime}}'   # → nvidia
sudo docker exec <container> printenv NVIDIA_VISIBLE_DEVICES NVIDIA_DRIVER_CAPABILITIES
```

`nvidia-smi` im Container muss die GTX 1060 anzeigen. Ohne aktivierte Option
zeigt `HostConfig.Runtime` den Docker-Standard und die `NVIDIA_*`-Variablen
fehlen.

### 4. C2ME / OpenCL

`nvidia-smi` beweist nur, dass der Treiber (`utility`) durchgereicht wird.
Für das OpenCL-Zusatzmodul von C2ME gilt zusätzlich:

| Voraussetzung | Stand |
|---|---|
| Java 25 (das Modul ist mit class file version 69 kompiliert; Java 21 meldet `UnsupportedClassVersionError`) | pro Instanz unter Einstellungen → „JVM & RAM" → Java = 25 einstellen |
| `libnvidia-opencl.so.1` + `/etc/OpenCL/vendors/nvidia.icd` vom Host-Treiber | liefert die NVIDIA-Runtime (Capability `compute`, wird vom Dashboard gesetzt) |
| OpenCL-ICD-Loader `libOpenCL.so.1` / `libOpenCL.so` (LWJGL) | fehlt im itzg-Image (Ubuntu 24.04, geprüft: kein OpenCL-Loader). Das Dashboard baut dafür bei der ersten GPU-Instanz automatisch ein abgeleitetes Image |

**GPU-Image:** Bei aktivierter GPU-Option startet der Container nicht mit
`itzg/minecraft-server:<java-tag>`, sondern mit `mc-dashboard-minecraft-opencl:<java-tag>`.
Das Dashboard baut es beim ersten Start einmalig über den Docker-Socket
(`FROM itzg/minecraft-server:<java-tag>` + `apt-get install ocl-icd-libopencl1 clinfo`,
Symlink `libOpenCL.so` → `libOpenCL.so.1`, `ldconfig`, Fallback-Eintrag
`/etc/OpenCL/vendors/nvidia.icd`). Der **Docker-Host braucht dafür beim ersten
Start Internetzugang**; der Bau dauert etwa eine Minute. Das Image liegt
danach im Image-Speicher des Hosts und bleibt über Stoppen, Starten und
Neuerstellen der Instanz erhalten; Welten und Instanzdaten liegen im
Volume und werden nicht angefasst. Ohne GPU-Option ändert sich nichts (Standard-Image).
Schlägt der Bau fehl, startet die Instanz nicht und das Dashboard meldet den Grund.

Prüfung im laufenden Minecraft-Container:

```bash
sudo docker exec <container> ldconfig -p | grep -i opencl     # libOpenCL.so + libOpenCL.so.1
sudo docker exec <container> ls /etc/OpenCL/vendors/            # nvidia.icd
sudo docker exec <container> clinfo -l                          # Platform: NVIDIA CUDA / Device: NVIDIA GeForce GTX 1060 6GB
```

Zeigt `clinfo -l` „Number of platforms 0", kommt `libnvidia-opencl.so.1` vom
Host nicht im Container an: Capability (`compute`), Treiber und
Container-Toolkit prüfen (`docker inspect <container> --format '{{.Config.Env}}'`).

**Was geprüft ist und was nicht:** Geprüft sind der Image-Bau, dass
`libOpenCL.so.1`/`libOpenCL.so` im abgeleiteten Image liegen und im
ldconfig-Cache stehen und dass `clinfo` den Loader laden kann. Dass die
GTX 1060 als OpenCL-Gerät erscheint und C2ME-OpenCL damit funktioniert, ist
**nicht** in der Entwicklungsumgebung (keine GPU) geprüft, sondern nur auf
deinem Host mit den Befehlen oben prüfbar.
