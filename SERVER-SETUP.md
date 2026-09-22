# Minecraft-Server einrichten – Komplette Übersicht (v2)

Gültig für **manuelle Setups** (VPS/eigener Host/NAS) und für dieses Projekt
(**mc-dashboard** mit Docker/ZimaOS). Unterschiede sind markiert, der
Dashboard-Schnellstart steht am Ende.

```
Entscheidungspfad:
  Nur Vanilla/Paper, wenig Tuning?        → Weg A (klassisch) oder Dashboard
  Modpack spielen?                        → Abschnitt 2 (Server-Pack) + Abschnitt 3 (Java!)
  Mehrere Server, NAS (ZimaOS), lazys?    → Dashboard (Abschnitt 8) + Abschnitt 4
```

---

## 1. Hardware- & Ressourcen-Planung

**Grundprinzip:** Minecraft ist **single-thread-lastig** — der Einzelkern-Takt
(≥ 3,5 GHz, Basisfrequenz!) entscheidet über TPS, nicht die Kernanzahl. Mehr
RAM hilft Modpacks, ersetzt aber keine CPU. Werte gelten **pro
Server-Instanz**; das Host-OS braucht zusätzlich 1–2 GB.

| Server-Typ | Typische Packs | 5 Spieler | 10 Spieler | 20+ Spieler | SSD |
|---|---|---|---|---|---|
| Vanilla | — | 3 GB | 5 GB | 8 GB | 10–20 GB |
| Paper (leer/Plug-ins) | — | 3–4 GB | 5 GB | 8 GB | 15–30 GB |
| Leichtes Modpack | Simply Optimized, Fabulously Optimized, Adrenaline | 4 GB | 6 GB | 10 GB | 20–50 GB |
| Mittleres Modpack | FTB Academy, Prominence II, Better MC | 6–8 GB | 10 GB | 12–16 GB | 50–80 GB |
| Schweres Modpack | All the Mods 9/10, Enigmatica 6, GT: New Horizons | 10 GB | 12–16 GB | 16–24 GB | 80–150 GB |
| Modded-Dauerwelten (Tech, 1.12.2) | GregTech: NH, Divinci | 10–12 GB | 16 GB | 20–32 GB | 100 GB+ |

CPU-Kerne: 2 (bis ~10 Spieler, leicht) · 4 (10–20 Spieler/mittlere Packs) ·
6+ (20+ Spieler oder mehrere Instanzen).

**Erfahrungswerte:**
- **RAM-Puffer:** `-Xmx` nie = Host-RAM; 10–20 % (mind. 1 GB) für OS + Backups.
- **Welten wachsen:** ~0,5–2 GB/Monat bei 10 aktiven Spielern; Backups brauchen
  das 1–2-Fache der Welten-Größe zusätzlich.
- **Modpack-Anforderungen** stehen in der Pack-Beschreibung („Server RAM").
  Große Packs unter 6 GB → GC-Stotterer; mit 20 Spielern unter 10 GB kritisch.
- **View-Distance 8** (statt 10/12) ist der größte einzelne Hebel bei vielen
  Spielern; bei Tech-Packs zusätzlich **Pregen der Welt** (Chunky/Cubby).

---

## 2. Modpack-Workflow (Server-Pack — ohne Vanilla-Vorab-Setup)

Moderne Modpacks liefern **fertige Server-Packs** mit Loader, Libraries und
Installationsskript. **Kein Vanilla-Server vorher aufsetzen.**

### Weg A: Manuell (VPS/Host)

1. **Server-Pack laden:**
   - **CurseForge:** Pack-Seite → *Files* → zur MC/Loader-Version passende
     Datei → *Additional Files* → **Server Pack** (.zip) herunterladen.
   - **FTB:** ftb.nz bzw. FTB-App → Pack → „Server download".
   - **Modrinth:** Pack-Seite → *Versions* → Eintrag → **Server Pack**
     (`.mrpack`-Container oder ZIP) herunterladen.
2. **Eigenen Ordner** anlegen und entpacken (`/opt/mc/<pack>`,
   Windows `D:\mc\<pack>`) — nie in ein bestehendes Server-Verzeichnis
   mischen; Server- und Client-Mods unterscheiden sich bewusst
   (Client-only-Mods fehlen serverseitig).
3. **Installationsskript ausführen** (einmalig, Internet nötig):
   - Linux: `./install.sh` bzw. `./start.sh --installServer`
   - Windows: `install.bat`
   - Forge/NeoForge alt: `java -jar forge-installer.jar --installServer`
4. **EULA akzeptieren:** `eula.txt` → `eula=true`
   (Zustimmung zur [Minecraft-EULA](https://aka.ms/MinecraftEULA)).
5. **Startdatei/Startbefehl** mit korrektem RAM starten
   (Flags → Abschnitt 6): `start.bat`/`start.sh` oder eigener `java …`-Aufruf.
6. **Erster Boot** generiert Welt/Configs; danach `server.properties`
   anpassen (Abschnitt 7) und neu starten.

**Häufige Fehler:** falsches Java (→ Abschnitt 3), RAM in `-Xmx` vergessen
(Standard 1 GB → Packs stürzen ab), Server-Pack-`mods/` mit Client-Mods
überladen, `eula.txt` erst nach dem ersten Start anlegen (Boot bricht ab).

### Weg B: Dashboard (dieses Projekt — alles automatisiert)

- **Tab „Modpacks"** → Ziel-Instanz + Quelle (Modrinth/CurseForge) → Pack
  suchen → **Installieren**. Kompatibilität (Loader + MC-Version) wird
  vorab geprüft;parallele Mod-Downloads mit SHA1-Prüfung, Fortschrittsbalken.
- **Eigener Server-Pack-Upload:** `.mrpack` (Modrinth) oder `.zip` mit
  `manifest.json` (CurseForge) direkt im Tab.
- **Keine Vanilla-Vorbereitung, keine Java-/Loader-Handarbeit:** Die Instanz
  startet direkt mit gewähltem Loader; Java wählt das itzg-Image passend.
  Loader nur als **Beta** verfügbar → Dialog wählt automatisch die neueste
  (Beta-Label), damit „auto" nicht scheitert.
- **EULA:** beim Instanz-Start automatisch gesetzt.

---

## 3. Java-Version ↔ Minecraft-Version

| Minecraft | Java | Bemerkung |
|---|---|---|
| ≤ 1.16.5 | 8 (oder 11) | alte Modpacks (GT:NH & Co.) |
| 1.17 – 1.20.4 | 17 | |
| 1.20.5 – 1.21.x | 21 | Pflicht ab 1.20.5 |
| 26.x (aktuell) | 21+ | aktuelle Images mitbringen Java 25 |

- **Manuell:** JDK-Pfad im Startskript setzen
  (`"/usr/lib/jvm/java-21-openjdk/bin/java" … -jar server.jar nogui`);
  mehrere Server mit verschiedenen MC-Versionen → pro Ordner ein JDK.
- **Docker/itzg (auch im Dashboard):** Java wird anhand `VERSION` automatisch
  gewählt; `JAVA_VERSION` erzwingt eine Variante.
- **Merksatz:** „Server startet nicht / Class-Version-Fehler" = fast immer
  falsches Java.

---

## 4. Netzwerk & Ports

| Port | Protokoll | Zweck | öffentlich? |
|---|---|---|---|
| 25565 | TCP | Minecraft Java (Standard) | ja (Spiel) |
| 25570, 25571, … | TCP | Dashboard-Instanzen (`INSTANCES_PORT_BASE`) | je ja |
| 19132 | UDP | Bedrock/Geyser (optional) | ja |
| 25575 | TCP | RCON (optional, **nicht** öffnen) | nein |
| 8080 | TCP | Dashboard | **nein** — nur LAN/VPN |

- **Heimnetz:** feste interne IP für den Server-Host (DHCP-Reservierung) →
  Router-Portweiterleitung 25565/tcp auf diese IP → Host-Firewall:
  `ufw allow 25565/tcp` (Linux) bzw. Windows-Firewall-Eingangsregel; bei VPS
  zusätzlich Security-Group.
- **DynDNS** bei wechselnder Heimanbindung (z. B. DuckDNS); **SRV-Record**
  für hübsche Adressen:
  `_minecraft._tcp.play` SRV 0 5 25565 server.meinedomain.de →
  Spieler verbinden mit `play.meinedomain.de`.
- **ZimaOS/NAS:** Dashboard-Port über `.env` (`DASHBOARD_HTTP_PORT`, Default
  8080; siehe Abschnitt 11); Instanz-Ports werden vom Dashboard direkt auf dem
  Host veröffentlicht. Port-Weiterleitung am NAS-Router wie oben.
- **Dashboard niemals direkt ins Internet** — nur VPN (WireGuard/Tailscale)
  oder Reverse-Proxy mit Auth; optional `DASHBOARD_API_KEY` setzen.

---

## 5. Backups (automatisch)

Kern: `world*/`, `configs/`, `mods/`, `server.properties`, `eula.txt`.
3-2-1-Regel: 3 Kopien, 2 Medien, 1 extern.

**Docker/Volume (dieses Projekt):**
```bash
# Backup des gesamten mcdata-Volumes (Instanzen inklusive). /data-Quelle:
#   Named Volume: <projekt>_mcdata (z. B. minedocker_mcdata, `docker volume ls`)
#   ZimaOS/Bind:  MCDATA_DIR aus .env (z. B. /DATA/AppData/mc-dashboard/data)
docker run --rm -v minedocker_mcdata:/data -v /pfad/zum/backup:/backup alpine \
  tar czf /backup/mcdata-$(date +%F-%H%M).tar.gz -C /data .
```

**Dauerhaft — bereits in der docker-compose.yml enthalten:**
Der Service `backup` (Alpine-Tar-Loop) sichert alle 6 h das gesamte
`mcdata`-Volume (alle Instanzen + Mods) nach `./backups`, löscht Archive
älter als 14 Tage und pflegt `latest.tgz` als Symlink auf das Neueste.
Anpassbar: `sleep 6h` (Intervall), `-mtime +14` (Retention), Backup-Pfad
(.env: `BACKUPS_DIR`, bei ZimaOS unter `/DATA/`). Für 100 % konsistente
Weltschnapp-
schüsse die Instanz vorher im Dashboard stoppen.
```bash
# Restore (alle Instanzen) — /data-Quelle analog zum Backup-Kommentar oben:
#   Named Volume: <projekt>_mcdata, sonst MCDATA_DIR-Pfad aus .env
#   (ZimaOS: -v /DATA/AppData/mc-dashboard/data:/data -v .../backups:/backup)
docker run --rm -v minedocker_mcdata:/data -v ./backups:/backup alpine \
  sh -c "rm -rf /data/* && tar xzf /backup/latest.tgz -C /data"
```

**Manueller Server:**
```bash
tar czf backup-$(date +%F-%H%M).tar.gz world/ configs/ mods/ server.properties
# cron: 0 */6 * * *  + rclone/rsync auf zweiten Standort
```

- Während des Betriebs: vorher `save-off`, danach `save-on` (Konsole) — oder
  `itzg/mc-backup`, das das automatisch macht.
- In-Game: **FTB Backups** (in vielen Packs enthalten), Paper-Backup-Plugins.
- **Restore testen!** Ungeprüftes Backup = keins.

---

## 6. Start-Parameter / JVM-Flags

**Aikar-Flags (bewährt für Modpacks & viele Spieler):**
```bash
java -Xms6G -Xmx6G \
  -XX:+UseG1GC -XX:+ParallelRefProcEnabled -XX:MaxGCPauseMillis=200 \
  -XX:+UnlockExperimentalVMOptions -XX:+DisableExplicitGC \
  -XX:+AlwaysPreTouch -XX:G1NewSizePercent=30 -XX:G1MaxNewSizePercent=40 \
  -XX:G1HeapRegionSize=8M -XX:G1ReservePercent=20 -XX:G1HeapWastePercent=5 \
  -XX:G1MixedGCCountTarget=4 -XX:InitiatingHeapOccupancyPercent=15 \
  -XX:G1MixedGCLiveThresholdPercent=90 -XX:G1RSetUpdatingPauseTimePercent=5 \
  -XX:SurvivorRatio=32 -XX:+PerfDisableSharedMem -XX:MaxTenuringThreshold=1 \
  -jar fabric-server-launch.jar nogui
```

- **`-Xms = -Xmx`** (vermeidet GC-Druck durch Laufzeit-Vergrößerung).
- `-Xmx` = geplanter RAM aus Abschnitt 1 minus ~1 GB Reserve.
- Vanilla/Paper mit ≤ 10 Spielern: G1-Default reicht; Paper hat eigene
  Empfehlungen (docs.papermc.io).
- Immer `nogui`; Betrieb via `screen`/`tmux`, systemd-Unit oder Docker
  (`restart: unless-stopped`).

---

## 7. `server.properties` – Essentials

```properties
motd=Mein Server                 # will match SLP-Anzeige im Dashboard
max-players=10
view-distance=8                  # 8–10; größter Hebel
simulation-distance=6            # 4–8 bei Modpacks
white-list=true                  # privater Server
online-mode=true                 # false nur mit Absicherung (LAN-Tools)
level-type=minecraft\:normal     # bei manchen Packs vorgegeben
enable-rcon=false                # nur bei Bedarf, nie öffentlich
```
`difficulty`, `spawn-protection`, `max-tick-time=-1` (bei großen Modpacks
hilfreich gegen Watchdog-Kicks) nach Bedarf.

---

## 8. Betrieb & Monitoring (dieses Dashboard)

- **Übersicht:** laufende Instanzen + CPU/RAM aller MC-Container (Docker-
  Stats, 5-s-Refresh).
- **Instanz-Verwaltung:** Start/Stop/**Neustart**, Logs (Live-Stream per
  Server-Sent Events, Fallback: 5-s-Polling), je-Instanz-Mods mit
  Aktivieren/Deaktivieren-Toggle.
- **Verlauf (Statistik-Tab):** CPU/RAM aller Container und Spielerzahlen
  werden persistent in SQLite gesammelt (`/data/history.db`, Intervall
  `HISTORY_INTERVAL`, Retention `HISTORY_RETENTION_DAYS`, Default 30 Tage) —
  Auslastung über Tage statt nur der Sitzungs-Sparkline.
- **Ressourcen-Interpretation:** `ram_limit` = Host-RAM (Container ohne
  eigenes Limit); Java-Baselineram nach Boot ~1–2 GB, Modpacks steuern
  Richtung `-Xmx`. Paper pausiert leere Server („Server empty … pausing") —
  CPU ~0 % ist dann normal.
- **Logs live:** Verwalten → Logs; Fehler wie „UnsupportedClassVersion"
  (Java), „Unable to find suitable version" (Loader nur als Beta → Dialog
  wählt automatisch) und Maven-502 (Netzwerk-Hickser, Retry genügt) sind die
  häufigsten.
- **Crash-Watchdog:** Das Dashboard beobachtet die Docker-Exit-Events aller
  Instanz-Container. Stirbt ein Server unerwartet (Exit-Code ≠ 0), wird der
  Instanz-Status automatisch auf **error** gesetzt — auch wenn das Dashboard
  selbst gerade neu startete. Saubere Stops (Dashboard-Stop/Neustart) werden
  nicht als Crash gewertet; Docker-Restarts der Restart-Policy setzen den
  Status wieder auf „läuft".
- **Alerts (optional):** Bei Crash/Start/Stop kann ein Webhook benachrichtigt
  werden — Discord-kompatibel (`ALERT_WEBHOOK_URL`, POST `{"content": …}`)
  und/oder Telegram (`TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`). Mit
  `ALERT_EVENTS` wählt man die Ereignisse (`crash,start,stop`, Default: alle
  drei; z. B. nur `crash`). Ohne Env-Variablen bleibt alles still.
- **Sicherheits-Snapshots:** Vor jeder Modpack-Installation in eine Instanz
  mit bestehenden Mods (und vor jedem Backup-Restore) legt das Dashboard
  automatisch einen Snapshot an (`pre-install-*.tar.gz` bzw.
  `pre-restore-*.tar.gz` in der Instanz-Backup-Liste) — eine fehlgeschlagene
  Installation ist damit per Restore rückholbar, `force`-Überschreiben ist
  risikofrei. Pro Instanz rotieren max. 3 Sicherheits-Snapshots.
- **Job-Persistenz:** Laufende Downloads/Installationen/Mod-Updates werden
  alle 5 s auf Platte gespiegelt (`/data/jobs/`). Nach einem Dashboard-
  Neustart erscheinen abgebrochene Vorgänge als „Abgebrochen (Neustart)"
  statt zu verschwinden; fertige Jobs bleiben 7 Tage abrufbar.
- **Härtung:** Dashboard läuft als UID/GID 1000 mit Socket-Zugriff über die
  Docker-Gruppe — in der `docker-compose.yml` `DOCKER_GID` auf die Host-GID
  von `/var/run/docker.sock` setzen (ermitteln mit
  `stat -c '%g' /var/run/docker.sock`). Bestands-`mcdata`-Volume (früher
  root) migriert der `init`-Service beim nächsten `docker compose up`
  automatisch; manuell als Fallback:
  `docker compose run --rm --user 0 dashboard chown -R 1000:1000 /data`

---

## 9. Checkliste (komplett)

- [ ] Host: 64-bit OS, SSD, ≥ 3,5 GHz Einzelkern, RAM laut Matrix + 1–2 GB OS
- [ ] Passendes Java (Abschnitt 3) oder Docker/Compose
- [ ] Pro Server eigener Ordner; Server-Pack entpackt (Abschnitt 2)
- [ ] `install.sh`/`install.bat` bzw. Loader-Installer ausgeführt
- [ ] `eula.txt` → `eula=true`
- [ ] Startskript mit `-Xms = -Xmx` + Flags (Abschnitt 6), `nogui`
- [ ] `server.properties`: MOTD, max-players, view-/simulation-distance,
      white-list, online-mode (Abschnitt 7)
- [ ] Ports: 25565/tcp (Java) bzw. 19132/udp (Bedrock) weitergeleitet +
      Firewall; Instanzen 25570+ (Abschnitt 4)
- [ ] DynDNS/SRV eingerichtet (Heimnetz) bzw. DNS auf VPS
- [ ] Dashboard gesichert: `DASHBOARD_API_KEY`, 8080 nur LAN/VPN
- [ ] Backups automatisiert (Abschnitt 5) + extern + Restore getestet
- [ ] Erster Join über `<ip>:<port>`; „Done (…s)!" in Logs; Ressourcen beobachtet

---

## 10. Schnellstart mit diesem Dashboard

```bash
docker compose up -d --build        # startet NUR das Dashboard (Port 8080)
# → http://localhost:8080
#   Tab "Server"    → Instanz erstellen (Version, Loader, RAM, Port 25570+, EULA)
#   Tab "Modpacks"  → Server-Pack suchen (Modrinth/CurseForge) oder hochladen
#   Tab "Übersicht" → Instanzen + CPU/RAM aller Server (Docker-Stats)
#   Start/Stop/Neustart, Logs, je-Instanz-Mods in der Instanz-Verwaltung
```

Automatisch erledigt: Java-Auswahl, Loader-Installation (inkl. Beta-Handling),
EULA, Server-Pack-Installation, Speicherorte (`INSTANCES_DIR`), Start/Stop.
Nur **Portweiterleitung** (Abschnitt 4) und **Backups** (Abschnitt 5) bleiben
Host-Aufgabe.

---

## 11. ZimaOS-Deployment (NAS)

ZimaOS verwaltet App-Daten unter `/DATA` (im Dateimanager/Samba sichtbar);
die Systemplatte ist klein — Daten und Backups gehören auf das Storage-Array.

**Weg 1 — SSH (empfohlen, hier funktioniert `build:`):**

1. Per SSH auf dem ZimaOS-Gerät anmelden, Projekt nach
   `/DATA/AppData/mc-dashboard` kopieren (git clone oder Ordner-Upload).
2. Vorlage kopieren und anpassen:
   ```bash
   cp .env.example .env
   stat -c '%g' /var/run/docker.sock   # GID notieren (typisch 999)
   ```
   `.env` für ZimaOS:
   ```ini
   DOCKER_GID=999
   MCDATA_DIR=/DATA/AppData/mc-dashboard/data
   BACKUPS_DIR=/DATA/AppData/mc-dashboard/backups
   #DASHBOARD_HTTP_PORT=8080   # nur setzen, wenn 8080 belegt ist
   ```
3. Starten: `docker compose up -d --build`
4. Öffnen: `http://<zima-ip>:8080` → Tab „Server" → Instanz erstellen.

Der `init`-Service setzt beim ersten Start die Ownership (1000:1000) auf
dem Datenordner — keine manuelle `chown`-Aktion nötig.

**Weg 2 — App-Import in der ZimaOS-UI (mit fertigen Images):** Der
Import-Dialog baut **keine** Images (`build:` wird dort nicht ausgeführt,
nur Image-Pull). GitHub Actions baut das Dashboard-Image bei jedem Push auf
`main` und pusht es nach `ghcr.io/chronixx4/minedocker:latest` (Workflow
`docker-publish.yml`). Nach dem ersten Push das GHCR-Package einmalig auf
GitHub unter „Packages" auf **Public** stellen, sonst kann ZimaOS ohne
Login nicht ziehen. Zwei empirische Import-Fallen (deshalb die
Spezial-YAML in `INSTALL-ZIMAOS.md` nutzen):
1. Der Import erstellt Services mit `depends_on:
   service_completed_successfully` nicht sauber — ein init-Ownership-Service
   bleibt unangelegt und das Dashboard hängt ewig auf Status „Created".
   Der Import-Weg setzt die Ownership daher einmalig per SSH
   (`sudo chown -R 1000:1000 /DATA/AppData/mc-dashboard/data ...`).
2. Der Import verschluckt `$$`-Escapes in Commands (Backup-Archive hießen
   `mcdata-.tgz`) — die Import-YAML kommt deshalb ohne `$`-Zeichen aus
   (Backup-Rotation per Umbenennungskette, 14 Generationen à 24 h).
Die „Compose Toolbox"-App (App Store) hilft beim Validieren/Loggen eigener
Stacks.

**ZimaOS-Besonderheiten:**

- **Docker-Socket-Gruppe:** `DOCKER_GID` wie oben setzen; ohne passende GID
  meldet das Dashboard „Permission denied" beim Socket-Zugriff (Start/Stop
  und Ressourcen-Karte bleiben leer).
- **`MCDATA_DIR` exklusiv:** Der Pfad muss ein dediziertes Unterverzeichnis
  sein — nie `/DATA` selbst oder ein Ordner mit fremden Daten. Der
  `init`-Service bricht das `chown` sonst mit FATAL ab (Diagnose:
  `docker compose logs init`); Notfall-Start ohne init:
  `docker compose up -d --no-deps dashboard`.
- **Projektordner umziehen:** Der Name des Daten-Volumes hängt am Compose-
  Projektnamen (`<projekt>_mcdata`). Bei einem Ordnerwechsel das Volume
  umbenennen (`docker volume rename alt_mcdata neu_mcdata`) oder in `.env`
  `MCDATA_DIR` setzen und die Daten dorthin umziehen — sonst startet das
  Dashboard gegen ein leeres Volume (siehe `.env.example`).
- **init-Service hängt:** `docker compose logs init` zeigt Pull-/chown-
  Fehler; `restart: on-failure` retryt automatisch, bis der Ownership-Fix
  klappt, und das Dashboard startet erst danach.
- **Scheduler-Zeiten** (täglicher Neustart, geplante Backups) laufen in
  Container-Lokalzeit → in der dashboard-Umgebung `TZ: "Europe/Berlin"`
  setzen (in docker-compose.yml vorgemerkt), sonst gilt UTC.
- **Instanz-Container & Compose-Netz:** `docker compose down` entfernt das
  Netzwerk; laufende Instanzen verlieren den Anschluss und werden beim
  nächsten „Starten" im Dashboard automatisch neu erstellt (kein Daten-
  verlust — die Instanzdaten liegen unter `MCDATA_DIR`).
- **Storage umziehen:** ZimaOS-Einstellungen → Apps → „App data location"
  steuert nur App-Store-Apps; bei diesem Stack `MCDATA_DIR`/`BACKUPS_DIR`
  in `.env` ändern und die Ordner verschieben (Instanzen vorher stoppen).
- **Ports:** Dashboard-Port nur LAN/VPN (nicht ins Internet), Minecraft-
  Ports 25570+ je nach Bedarf weiterleiten (siehe Abschnitt 4).
- **Docker-Images:** liegen auf der Systemplatte (`/var/lib/docker`); bei
  Platzmangel den ZimaOS-„Data Migration"-Mechanismus nutzen (Docker-
  Daten zwischen Platten verschieben) — Instanzdaten sind davon unabhängig.
