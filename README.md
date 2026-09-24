# mc-dashboard

[![CI](https://github.com/Chronixx4/minedocker/actions/workflows/ci.yml/badge.svg)](https://github.com/Chronixx4/minedocker/actions/workflows/ci.yml)
[![Docker Image](https://img.shields.io/badge/ghcr.io-chronixx4%2Fminedocker-blue)](https://github.com/Chronixx4/minedocker/pkgs/container/minedocker)

Web-Dashboard zur Verwaltung beliebig vieler **Minecraft-Server-Instanzen** auf einem
Docker-Host (NAS, Home-Server, VPS) — FastAPI-Backend + Vanilla-JS-Frontend, ohne
Framework-Abhängigkeiten im Frontend.

Es gibt **keinen festen „Hauptserver"**: Jeder Server ist eine Instanz mit eigenem
Ordner, Port, RAM-Limit und eigenem `itzg/minecraft-server`-Container — erstellt,
gestartet und verwaltet über das Dashboard.

## Features

- **Beliebig viele Instanzen** — Vanilla, Forge, NeoForge, Fabric, Quilt, Paper,
  Bukkit/Spigot; eigene Ports ab 25570 (Spiel) und Port+1000 (RCON), RAM-Limit
  je Instanz (**jederzeit nachträglich änderbar**, wirksam beim nächsten
  Start/Neustart), Log-Rotation je Instanz
- **Übersicht mit Live-Daten** — CPU/RAM (Sparklines), Spieler online (Server-List-
  Ping), Uptime, MOTD, Statusfilter, Suche, Sortierung, Gruppen- und Sammel-Aktionen
- **Mods & Modpacks** — Modrinth- und CurseForge-Suche je Instanz (Pflicht-
  Abhängigkeiten werden mitinstalliert), Mod an/aus-Toggle, Update-Check mit
  Versions-Chips, Modpacks als `.mrpack`/CurseForge-`.zip` hochladen oder direkt
  aus der Suche installieren — auch als **neuer Server aus einem Modpack**
  (Server-Pack bevorzugt, MC-Version/Loader automatisch aus dem Pack)
- **Sicherheit zuerst** — vor jeder Modpack-Installation/-Restore wird automatisch
  ein Wiederherstellungs-Snapshot angelegt (fail-closed)
- **Welt-Verwaltung** — Welt als `.zip` herunterladen/hochladen, bestehende
  Server-Ordner importieren (`.zip`/`.tar.gz`), Instanzen klonen als Vorlage
- **RCON-Konsole & Spielerverwaltung** — freie Befehle (Whitelist- oder Free-Modus),
  Whitelist-Editor mit UUID-Auflösung, op/kick/ban direkt aus der Übersicht
- **Gamerule-Quick-Editor** — kuratierte Vanilla-1.21.x-Gamerules mit Toggles und
  Zahlenfeldern, sofort per RCON gesetzt (nur bei laufender Instanz)
- **Datapacks** — hochladen (nur gestoppt), aktivieren/deaktivieren (bei laufendem
  Server per RCON), löschen; deaktivierte Packs liegen in `disabled_datapacks/`
- **Datei-Browser** — Instanz-Ordner durchstöbern, Textdateien ≤ 1 MiB bearbeiten,
  Dateien hochladen/umbenennen/löschen; verwaltete Dateien (`server.properties`,
  `instance.json`, `packs/`) sind geschützt
- **Spielzeit je Spieler** — Leaderboard 24 h/7 Tage/30 Tage/Gesamt aus Session-
  Tracking des Verlauf-Samplers (SLP-Spielerliste, RCON-Fallback)
- **Login & Rollen** — Benutzer mit Admin/Viewer-Rolle (`users.json`, scrypt-Hash),
  Session-Cookie (HttpOnly, SameSite=Strict), Setup-Dialog für den ersten Admin,
  Lockout nach 10 Fehlversuchen; `DASHBOARD_API_KEY` bleibt als Admin-Bypass
- **Zeitplan je Instanz** — Auto-Start, täglicher Neustart mit Vorwarnung,
  geplante Backups mit Rotation, geplanter Mod-Update-Check (Container-Lokalzeit)
- **Crash-Watchdog & Alerts** — erkennt abgestürzte Instanzen und meldet per
  Discord-Webhook oder Telegram (`crash`, `start`, `stop`, optional `update`)
- **Statistik-Verlauf** — persistente CPU/RAM-/Spieler-Charts über 6 h bis 30 Tage
- **Live-Logs per SSE**, Einstellungs-Editor für `server.properties` (Formular- oder
  Raw-Modus, verwaltete Keys geschützt)
- **Mobil-fähig** — responsives Layout mit Bottom-Navigation, 44 px Tap-Flächen

## Ports

| Port | Zweck |
|---|---|
| 8080 | Dashboard (LAN/VPN — **nie** direkt ins Internet!) |
| 25570+ | Minecraft-Spielports der Instanzen (auf dem Host veröffentlicht) |
| port+1000 | RCON-Endpunkt der Instanz (vom Dashboard genutzt) |

## Schnellstart (Docker Compose — Server/NAS)

Voraussetzung: Docker + Docker-Compose v2 auf einem Linux-Host.

```bash
git clone https://github.com/Chronixx4/minedocker.git
cd minedocker
cp .env.example .env
nano .env          # DOCKER_GID setzen (siehe unten)
docker compose up -d --build
```

Dashboard: `http://<host-ip>:8080` → Tab „Server" → Instanz erstellen.
Die Instanz-Container werden vom Dashboard selbst erzeugt (Benötigt den
Docker-Socket-Mount, der in der Compose-Datei enthalten ist).

`DOCKER_GID` ist die Gruppen-GID des Docker-Sockets:

```bash
stat -c '%g' /var/run/docker.sock    # typisch 999 (Linux), 0 (Docker Desktop/WSL2)
```

Alle Pfade/Ports sind über `.env` anpassbar — siehe Kommentare in
[`.env.example`](.env.example): `MCDATA_DIR` (Named Volume oder Bind-Mount,
z. B. `/DATA/AppData/mc-dashboard/data`), `BACKUPS_DIR`, `DASHBOARD_HTTP_PORT`.

## ZimaOS / NAS-App-Import

Das GitHub-Action `docker-publish.yml` baut bei jedem Push auf `main` das Image und
pusht es nach `ghcr.io/chronixx4/minedocker:latest`. Für die Installation über die
ZimaOS-UI gibt es eine angepasste Import-YAML ohne Build-Schritt und ohne
`$`-Escapes (beides Eigenheiten des ZimaOS-Import-Dialogs):

**→ [INSTALL-ZIMAOS.md](INSTALL-ZIMAOS.md)** — inkl. einmaliger SSH-Vorbereitung
(Ownership), Port-Mapping-Hinweisen und Troubleshooting.

## Konfiguration (App-Env)

| Variable | Default | Zweck |
|---|---|---|
| `DASHBOARD_API_KEY` | *(leer)* | Admin-Bypass für alle API-Routen außer `/api/health` (Skripte/curl) — **setzen, wenn fremde Nutzer im Netz!** (z. B. `openssl rand -hex 32`) |
| `FILEBROWSER_MAX_UPLOAD_MB` | `300` | Upload-Limit je Datei im Datei-Browser |
| `CORS_ORIGINS` | `*` | Bei Reverse-Proxy auf die echte Origin einschränken |
| `CF_API_KEY` | *(leer)* | CurseForge-API-Key für Mod-/Modpack-Suche ([console.curseforge.com](https://console.curseforge.com)). **Optional:** Ohne Key sind CF-Suche und -Direktinstallation deaktiviert (503) — Modrinth-Suche/-Installation und der Modpack-Upload (CF-.zip) funktionieren weiterhin ohne Key (→ SERVER-SETUP.md §2). |
| `CF_CLIENT_ONLY_MODS` | *(leer)* | Zusätzliche Muster (Komma-Liste) für Client-only-Mods, die beim CF-Pack-Upload übersprungen werden — ergänzt die eingebaute Liste (u. a. Sodium, Iris, Embeddium, Rubidium, Oculus, Magnesium, Nvidium, Dark Mode Everywhere, Toast Control, Mouse Tweaks, Controlling; diese sind auf Servern nutzlos bzw. crashen beim Start) |
| `INSTANCES_DIR` | `/data/instances` | Instanz-Ordner im Dashboard-Container |
| `INSTANCES_PORT_BASE` | `25570` | Erster Spielport für neue Instanzen |
| `INSTANCES_MEMORY` | `2G` | Default-Heap pro Instanz |
| `INSTANCES_HOST_DIR` | *(auto)* | Host-Pfad der Instanzen (leer = automatische Erkennung) |
| `ALERT_WEBHOOK_URL` / `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` / `ALERT_EVENTS` | *(leer)* | Crash-Watchdog-Alerts; leer = aus |
| `TZ` | UTC | Container-Lokalzeit für Scheduler-Zeiten (z. B. `Europe/Berlin`) |

## Login & Rollen

Beim ersten Aufruf öffnet das Dashboard den **Setup-Dialog** — erster Benutzer
wird Admin. Danach schützt das Login alle `/api`-Routen:

- **Admin**: alles (Start/Stop, Konsole, Dateien, Datapacks, Benutzer verwalten)
- **Viewer**: strikt nur lesen (Verlauf, Logs, Listen) — schreibende Aufrufe
  werden zusätzlich serverseitig mit 403 abgewiesen
- **Benutzer verwalten** über das Topbar-Menü (Rolle wechseln, Passwort
  zurücksetzen, löschen; letzter Admin bleibt geschützt); eigenes Passwort
  über „Passwort ändern"
- **Sitzung**: HMAC-signiertes Cookie (HttpOnly, SameSite=Strict, 30 Tage,
  Secret in `/data/.auth_secret`, 0600); Logout löscht den Cookie
- **Lockout**: 10 Fehlversuche pro Benutzername → 5 Minuten Sperre (in-memory,
  Neustart resettet); Login-Fehler mit konstantem Delay
- **Ohne Setup** (kein Benutzer, kein `DASHBOARD_API_KEY`): API offen wie zuvor
  — Homelab-Bestandsverhalten
- **`DASHBOARD_API_KEY`** bleibt als Alternative voll gültig (implizit Admin),
  auch parallel zum Login; das Frontend merkt sich ihn im Browser (localStorage)

## Sicherheit

- Das Dashboard hat vollen Zugriff auf die Docker-Engine (Socket-Mount) und
  verwaltet Server-Container — **nur im LAN/VPN betreiben** (WireGuard/Tailscale)
  oder hinter einen authentifizierten Reverse-Proxy hängen.
- Login & Rollen nutzen (Setup-Dialog beim ersten Aufruf) und/oder
  `DASHBOARD_API_KEY` setzen, sobald das Netzwerk nicht vollständig vertrauens-
  würdig ist. Bei Reverse-Proxy: HTTPS-Terminierung am Proxy (Cookie ist
  HttpOnly + SameSite=Strict; `secure`-Flag ist bewusst nicht gesetzt, damit
  HTTP-LAN-Zugänge funktionieren).
- Das Dashboard läuft als unprivilegierter User (UID/GID 1000), Docker-Socket-
  Zugriff nur über die Socket-Gruppe (`DOCKER_GID`).

## Backups

- **Compose-Daemon** (`backup`-Service): tar't das gesamte Datenverzeichnis im
  6-h-Takt, 14 Tage Retention, `latest.tgz`-Symlink auf den neuesten Stand.
  Ziel: `BACKUPS_DIR` (Default `./backups`).
- **Zeitplan je Instanz:** zusätzliche Snapshots pro Instanz mit eigenem
  Intervall/Behalte-Anzahl (Dashboard → Zeitplan-Box).
- Restore-Befehle: [SERVER-SETUP.md §5](SERVER-SETUP.md).

## Entwicklung

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt

python -m ruff check app tests   # Lint
python -m mypy                   # Typen (app/)
python -m pytest tests/          # Test-Suite (~500 Tests, Fake-Docker, keine echten Container nötig)
```

CI ([ci.yml](.github/workflows/ci.yml)) läuft bei jedem Push/PR mit ruff, mypy und
pytest; das Docker-Image baut [docker-publish.yml](.github/workflows/docker-publish.yml).

Für die App-Verwaltung inkl. lokalem Betrieb mit Docker siehe `PROJECT_MAP.md`
(**die** interne Landkarte: Struktur, Endpunkte, Verhalten, bekannte Eigenheiten).

## Dokumentation

| Datei | Inhalt |
|---|---|
| [SERVER-SETUP.md](SERVER-SETUP.md) | Ausführlicher Setup-Guide: Hardware-Planung, Modpack-Server-Packs, Java-Matrix, Netzwerk/Portforwarding, Backups, JVM-Flags, ZimaOS (§11) |
| [INSTALL-ZIMAOS.md](INSTALL-ZIMAOS.md) | ZimaOS-App-Import mit fertigen Images |
| [PROJECT_MAP.md](PROJECT_MAP.md) | Interne Projektlandkarte (Struktur, alle Endpunkte, Verhalten) |

## Tech-Stack

FastAPI + Docker-SDK (Backend), Vanilla-JS + SVG-Charts (Frontend),
`itzg/minecraft-server` als Instanz-Image, Docker Compose, pytest.
