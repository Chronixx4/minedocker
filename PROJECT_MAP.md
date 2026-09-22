# PROJECT_MAP – mc-dashboard

Web-Dashboard zur Verwaltung beliebig vieler **Minecraft-Server-Instanzen**
(FastAPI-Backend + Vanilla-JS-Frontend, Docker/ZimaOS-ready).

**Hauptserver entfernt:** Es gibt keinen festen "Hauptserver" mehr — alle
Server sind Instanzen, die ausschließlich über das Dashboard manuell
erstellt/gestartet/gestoppt werden. Compose startet nur das Dashboard.

**Doku:** `SERVER-SETUP.md` — ausführlicher Einrichtungs-Guide (Hardware-
Planung, Modpack-Server-Packs, Java-Matrix, Netzwerk, Backups, Flags,
Checkliste, Dashboard-Schnellstart).

## Struktur
- app/ – FastAPI-Backend
  - main.py – App, CORS, API-Routen (/api/*), Static-Mount, Fehlerhandler,
    Lifespan (startet den Verlauf-Sampler, den Job-Spiegel-Flush alle 5 s,
    den Crash-Watchdog-Thread und den Scheduler-Thread; lädt beim Start
    gespiegelte Jobs)
  - config.py – Settings aus Env + MC-Version-Autoerkennung; Multi-Server:
    INSTANCES_DIR, INSTANCES_PORT_BASE (25570), INSTANCES_MEMORY, INSTANCES_HOST_DIR
  - instances.py – Instanz-Verwaltung: create/list/update/delete, getrennte Ordner
    unter {INSTANCES_DIR}/{id}/ (instance.json, eula.txt, server.properties, mods/),
    EULA-Pflicht, Name-Port-Kollisionsschutz (Thread-Lock; RCON-Port = port+1000
    wird mitreserviert und in instance.json gespeichert), Status-Maschine,
    read/write_server_properties (validiert: Key-Regex, keine Zeilenumbrüche,
    bekannte Keys per Schema: bool/enum/int inkl. Bereichs-Prüfung + Normalisierung;
    verwaltete Keys (server-port, level-name, enable-rcon, rcon.*, query.port)
    werden außer bei internen Aufrufern (managed_ok=True, z. B. Welt-Import/
    Restore via worlds._patch_properties) auf den Dateistand gezwungen bzw.
    nicht neu angelegt; _PROPS_SCHEMA/properties_schema() liefert das Schema
    für den Formular-Editor),
     update_settings() (PATCH: Name/RAM/jvm_opts/use_aikar/tags/port; RAM
     jederzeit änderbar — auch bei laufender Instanz, wirksam beim nächsten
     (Neu-)Start; RAM-Regex [1-9]\d{0,3}[GM] lehnt 0G/08G ab; JVM-Flags
     einzeilig, max 2000 Zeichen, keine Steuerzeichen; Tags: 1-32 Zeichen
     ^[A-Za-z0-9][A-Za-z0-9 _-]*$, max 8, Dedupe case-insensitive; Port-Wechsel:
     nur bei gestoppter Instanz, fail-closed über runtime.running_state
     (None = Docker nicht prüfbar → 503), Kollisionsprüfung ohne die eigene
     Instanz UND Persistenz atomar unter _LOCK (TOCTOU-sicher), RCON-Port
     wird mitverschoben; Route läuft via asyncio.to_thread (kein Event-Loop-
     Blocking)), update_schedule() (PATCH: Zeitplan
      mit Validierung — auto_start, restart {enabled, time HH:MM, warn_minutes
      0-30}, backup {enabled, interval_hours 1-168, keep 1-20}, update_check
      {enabled, interval_hours}; Merge: nur übergebene Felder), clone_instance()
      (Klon als Vorlage: kompletter Ordner ohne packs/instance.json, neuer
      ID/Port, 409 bei laufender Quelle, Zeitplan UND Tags werden übernommen),
     find_world_dir()/world_dir() (Welt-Erkennung über level-name aus
     server.properties → 'world' → erster Unterordner mit level.dat),
     disk_usage() (total/mods/world/packs/rest + world_dir)
  - rcon.py – minimaler RCON-Client (Source-RCON-Protokoll, stdlib-only):
    command(host, port, password, cmd) + parse_list_output() für 'list'
  - runtime.py – Docker-Runtime pro Instanz (docker SDK): Container itzg/minecraft-server
    (mc-inst-{id}), Env je Loader (FABRIC/FORGE/NEOFORGE/QUILT/PAPER/BUKKIT) +
    RCON aktiv (ENABLE_RCON=TRUE, RCON_PASSWORD = deterministisch aus Salt-Datei
    {INSTANCES_DIR}/.rcon_salt + Instanz-ID, RCON_PORT=25575, Host-Mapping
    port+1000), optionale pro Instanz gesetzte JVM_OPTS + USE_AIKAR_FLAGS,
    Bind-Mount Instanz-Ordner -> /data, Host-Pfad via /proc/self/mountinfo
  - catalog.py – Katalog: Mojang-Versionen (Releases/Snapshots) + Loader-Versionen
    (Fabric/Quilt-Meta, Forge-Promotions, NeoForge-Maven, Paper Fill-API v3,
    Bukkit/Spigot-BuildTools-Hinweis); 10-min-Cache, nie werfend je Loader
  - packs.py – Modpack-Installer: Modrinth-Suche, mrpack-Download+Entpacken,
    Upload-Installation (.mrpack + CurseForge-.zip mit manifest.json),
    Index-Normalisierung (mrpack/CF -> internes Schema: title, game_version,
    loader, files[]), Kompatibilitaetspruefung, parallele Mod-Downloads
    (SHA1, .part+atomic, CF-Redirect-Dateiname), overrides/-Extraktion
    (Pfad-Whitelist: mods, config, kubejs, openloader, ...), Speicherplatz-Check,
    install_pack_cf(): Direktinstallation CurseForge-Packs via CF-API
    (Slug/File-ID -> Pack-Zip -> gleiche Upload-Pipeline, source=curseforge),
     env-Filter: mrpack-Dateien mit env.server=unsupported (nur Client)
     werden fuer Server-Instanzen uebersprungen,
     _safety_snapshot(): vor jeder Installation in eine Instanz mit
     bestehenden Mods (alle drei Install-Pfade) wird ein 'pre-install'-
     Sicherheits-Snapshot angelegt (fail-closed: fehlgeschlagener Snapshot
     bricht die Installation ab, nichts wird angetastet),
     Server direkt aus Modpack erstellen (ohne bestehende Instanz):
    create_server_from_pack() (Modrinth: Loader/MC-Version aus der Version-
    Metadaten; CurseForge: aus gameVersions-Tags, Server-Pack-Datei
    (serverPackFileId) wird bevorzugt),      create_server_from_upload()
    (Loader/MC-Version aus dem Archiv-Index), Namens-Sanitizing/-Suffixe,
    Cleanup der neuen Instanz bei fehlgeschlagenem Install-Start,
    Upload in bestehende Instanz: install_upload(auto_version=True) erkennt
    die benoetigte MC-Version/Loader-Kombination im Archiv und stellt die
    Instanz automatisch um statt mit 'Inkompatibel' abzubrechen (nur bei
    gestoppter Instanz, sonst Job-Fehler; 400 bei ununterstuetztem Loader;
    auto_version=false = streng wie bisher; Suche/Direktinstallation bleibt
    streng),
     Modpack-Updates in bestehende Instanz: pack_update_check() prüft je
     Quelle, ob eine neuere Pack-Version existiert (Modrinth: neueste
     kompatible Version via gemeinsamer Helfer _first_compatible_modrinth_
     version/_primary_modrinth_file — Install-Pfad und Check können nicht
     auseinanderlaufen; CurseForge: neueste stabile Datei + Kompatibilitaet
     aus den gameVersions-Tags; fehlende installierte Versions-ID (ältere
     CF-Installs) = einmal aktualisieren empfohlen, um den Stand zu pinnen;
     source=upload/ohne project_id → checkable=false); resolve_pack()
     liefert jetzt die gewählte Datei-ID mit (6-Tuple), install_pack_cf
     pinnt sie im Job/Meta (kein Dauer-"Update verfügbar" mehr);
     pack_update() installiert die neueste (oder per version_id/file_id
     gewaehlte) Version erneut in die Instanz (force=true, gleiche Pipeline
     inkl. Sicherheits-Snapshot),
- curseforge.py – CurseForge-API v1-Client (api.curseforge.com, x-api-key):
    Mod-/Modpack-Suche (gameId=432, classId 6/4471, modLoaderType-Mapping
    forge=1/fabric=4/quilt=5/neoforge=6), Slug-oder-ID-Auflösung,
    Dateiauswahl (neueste stabile .jar, downloadUrl oder Website-Redirect-Fallback),
    resolve_download_full() (liefert zusätzlich required_dep_ids() aus dem
    Relations-Feld der Datei — relationType 3=required, best effort, CF
    liefert das Feld nicht bei allen Dateien), dependency_files() (Pflicht-
    Deps zu Download-Einträgen auflösen, Tiefe 2, max 20, installierte
    übersprungen — gleiches Schema wie Modrinth),
    Key-Guard (503 ohne CF_API_KEY), resolve_pack_file() mit Server-Pack-
    Erkennung (serverPackFileId) + pack_meta_from_file() (Loader/MC-Version
    aus gameVersions-Tags), globale Modpack-Suche ohne Instanzbezug
    (Loader/Versionen aus latestFilesIndexes)
  - modrinth.py – Modrinth-API v2 + Download-Jobs (kind mod/pack/update,
    phase): In-Memory plus Job-Persistenz — jeder Job-Zustand wird als
    JSON-Datei nach /data/jobs gespiegelt (create, Endzustände, Flush alle
    5 s via Lifespan-Task), restore_jobs() lädt sie beim App-Start zurück
    und markiert dabei aktive Jobs als 'Abgebrochen (Neustart)'; MAX_JOBS-
    Verwerfen löscht die Spiegeldatei, Spiegel älter als 7 Tage werden beim
    Snapshot aufgeräumt; /api/jobs/{id} liefert auch nach Neustarts Status
  - worlds.py – Welt-Verwaltung + Server-Import: world_info/create_world_zip
    (Welt-Ordner als .zip in _staging, level.dat-Erkennung), restore_world_upload
    (.zip/.tar.gz ersetzt Welt; Layouts 'level.dat am Anfang' → Inhalt in den
    bestehenden Welt-Ordner bzw. 'Welt als Unterordner' → Ordner-Name wird
    übernommen, alter Welt-Ordner entfernt, level-name gesetzt; 409 bei laufender
    Instanz), import_server() (bestehenden Server-Ordner .zip/.tar.gz als neue
    Instanz einbinden: Extraktion mit Namens-/Größen-Deckelung, Traversal-Schutz
    (Zip: manuelle Pfadvalidierung; tar.gz: PEP-706-Filter 'data'), Welt am
    Wurzelverzeichnis wird nach world/ umgezogen, server.properties auf Container
    angepasst (server-port=25565, enable-rcon, rcon.port=25575, level-name),
    eula.txt erzwungen, Cleanup der Instanz bei Fehlern)
  - minecraft.py – Server List Ping (Status/MOTD/Spieler); Ressourcen-Statistik
    jetzt in runtime.py docker_resources() über die Docker-Stats-API
    (Hauptserver + Instanzen, kein PID-Namespace-Sharing mehr nötig;
    CPU-% normalisiert auf die Gesamtkapazität des Hosts, 0-100 % wie der
    Task-Manager — statt Docker-Rohwerten > 100 % pro Kern)
  - whitelist.py – Whitelist-Verwaltung ohne laufenden Server: whitelist.json
    lesen/schreiben (atomar, tolerant bei Defekt), Online-Mode: UUID per
    Mojang-API (best effort, MOJANG_API überschreibbar), Offline-Mode
    (online-mode=false): UUID deterministisch wie Java
    nameUUIDFromBytes('OfflinePlayer:'+Name), dedupe case-insensitive,
    Namens-Regex ^[A-Za-z0-9_]{1,16}$, max 200 Einträge; reload_on_server()
    (RCON 'whitelist reload', nur bei laufender Instanz)
  - updates.py – Mod-Update-Prüfung per Hash, ohne Registry: jede .jar/.jar.disabled
    wird einmal eingelesen → SHA1 + CurseForge-Murmur2 (Java-Referenz, seed=1,
    signed int32); Modrinth /version_file/{sha1}?multiple=true identifiziert
    installierte Version (Fallback: erste), /project/{id}/version liefert
    neueste kompatible; CurseForge /mods/fingerprints batchweise (50er-Chunks,
    Paarung über exactFingerprints) + /mods/{id}/files; Status je Mod:
    update_available / up_to_date / newer_than_latest (Downgrade-Schutz via
    date_published bzw. fileDate) / not_found; Update-Job (kind='update'):
    neueste Datei wird zum Update-Zeitpunkt frisch aufgelöst, atomar geladen
    (.part→rename, SHA1-Prüfung, CF-CDN-Host-Check), alte Datei entfernt,
    deaktiviert-Zustand (.disabled) bleibt erhalten; Fehler je Mod sammeln
    sich in job.summary, brechen den Job nicht ab
  - security.py – Dateinamen-/Pfad-Validierung (Anti-Path-Traversal), API-Key-Guard
  - history.py – Persistenter Statistik-Verlauf (SQLite/WAL, Default
    /data/history.db): Sampler-Lauf (HISTORY_INTERVAL, Default 30 s) via
    Lifespan-Task sammelt docker_resources() (CPU/RAM je Container) und
    Spieler-Zahlen (SLP je laufender Instanz, parallel, 2 s Timeout);
    Retention-Prune täglich + bei Start (HISTORY_RETENTION_DAYS, Default 30);
    query_series() buckettet serverseitig (~300 Punkte) auf: Summen CPU/RAM/
    Spieler + Spieler je Instanz; wirft nie — Sampling-Fehler nur geloggt
  - backups.py – Instanz-Backups (tar.gz-Snapshots je Instanz in
    /data/backups/{id}); safety_backup() Sicherheits-Snapshots: 'pre-install'
    vor Modpack-Installationen (ohne Welt/packs) und 'pre-restore' vor
    Backup-Restores (ohne packs); Rotation auf max. 3 pro Instanz; erscheinen
    in der normalen Backup-Liste und sind über die Restore-Route zurückholbar;
    scheduled_backup() zeitgesteuerte Backups des Schedulers ('scheduled-*',
    eigene Rotation auf keep, 1-20)
  - watchdog.py – Crash-Watchdog (Daemon-Thread aus dem Lifespan): liest die
    Docker-Events 'die'/'start' aller mc-inst-Container; 'die' mit Exit-Code
    ≠ 0 (ohne expect_stop-Markierung) → Instanz-Status 'error' (Crash),
    Exit-Code 0 oder gemeldeter Stop (Stop-/Neustart-/Löschen-Routen rufen
    expect_stop() auf) → 'stopped', 'start' → 'running' (deckt Docker-Restarts
    der Restart-Policy ab); optionale Alerts: ALERT_WEBHOOK_URL (Discord-
    kompatibel) und/oder Telegram (TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID),
    Ereignisauswahl via ALERT_EVENTS (Default crash,start,stop); wirft nie,
    reconnectet bei Docker-Fehlern endlos, stoppt sauber beim Shutdown
- scheduler.py – Geplante Aufgaben je Instanz (Daemon-Thread aus dem
    Lifespan, Tick alle 30 s, Zustand in /data/scheduler_state.json mit
    Atomic-Write; wirft nie, räumt verwaiste Einträge gelöschter Instanzen
    weg): Auto-Start (einmal pro Dashboard-Start — Instanzen mit
    schedule.auto_start werden beim Boot gestartet, manuell Gestopptes
    bleibt gestoppt, Docker-Boot-Wettlauf: 5 Versuche), geplanter Neustart
    (täglich zur schedule.restart.time in Container-Lokalzeit, RCON-'say'-
    Vorwarnungen warn_minutes davor + 1 Min vorher; verpasste Termine nur
    innerhalb 30 Min nachgeholt, sonst übersprungen; gleicher Ablauf wie die
    Restart-Route inkl. expect_stop, bei Fehlern Status 'error' + Crash-Alert),
    zeitgesteuerte Backups (alle interval_hours ein 'scheduled-*'-Snapshot
    via backups.scheduled_backup mit eigener Rotation auf keep; erstes Tick
    setzt nur die Fälligkeit), geplanter Mod-Update-Check (alle
    interval_hours, asyncio.run im Thread; bei Updates Alert mit Ereignis
    'update' — opt-in über ALERT_EVENTS, z. B. ALERT_EVENTS=crash,update;
    Anbieter-Fehler nur geloggt)
- static/ – Frontend ohne Framework: Übersicht mit KPI-Kacheln (Instanzen mit
  Lauf/Start/Fehler-Verteilungsbalken, Spieler online via SLP inkl. Namen,
  CPU/RAM mit Sparkline-Verlauf), zweispaltigem Hauptlayout (Server-Karten
  links, Ressourcen-Karte rechts sticky), Toolbar mit Namenssuche, Sortierung
  (Name/Status/Spieler/CPU/RAM/neueste), Statusfiltern (inkl. „Startet“),
  Gruppen-Aktionen (Tag-Dropdown + 'Gruppe starten/stoppen' — wirkt auf alle
  Instanzen mit dem gewählten Tag) und Sammel-Aktionen (Alle starten/stoppen);
  Server-Karten mit Info-Chips
  (Loader, MC-Version, RAM, Modpack, #-Tag-Chips, klickbarer Port = Adresse
  kopieren, Uptime via Container-StartedAt), Spieler/MOTD live, CPU/RAM je
  Container,
  Quick-Actions Start/Stop/Neustart/Logs/Verwalten; „Aktualisiert vor X s“-
  Anzeige + Manuell-Aktualisieren; Docker-Ressourcen-Karte mit Gesamtwerten +
  Verlaufskurven und Aufschlüsselung je Container (Status, Uptime, CPU/RAM,
   Sparklines); Polling: Status/Instanzen alle 5 s, Live-Pings alle 15 s
   (nur wenn Instanzen laufen), Detail-Logs per SSE (…/logs/stream, Fallback
   5-s-Polling bei Fehlern),
  Server-Tab (Instanz-Liste mit Tag-Chips, Erstell-Dialog mit Versions-/
  Loader-/Tag-Auswahl, Import-Dialog für bestehende Server-Ordner
  (.zip/.tar.gz: Name, Archiv, Versionen/Loader, Port, RAM, EULA),
  Start/Stop/Neustart/Logs/Klonen,
  je-Instanz-Mods mit Aktivieren/Deaktivieren-Toggle, Modpack-Suche/Upload im
  Detail-Dialog), Detail-Dialog mit Status-Badge, erweiterten Infos (Docker-
  Status, Uptime, Spieler, Server-Version, Modpack, Speicher je mods/world/
  packs), Tags-&-Port-Box (Tags kommagetrennt bearbeiten → PATCH tags; Port
  ändern → PATCH port, nur gestoppt, RCON = Port+1000),
  Modpack-Update-Box ('Update prüfen' → Statuszeile installiert/neueste
  inkl. Kompatibilität; inkompatible neueste Version wird als Warnung
  angezeigt statt 'neueste Version installiert'; 'Pack aktualisieren'
  startet den Update-Job; Check läuft automatisch beim Öffnen des
  Detail-Dialogs),
    Welt-Box (Info, Download als .zip, Upload .zip/.tar.gz nur bei
    gestoppter Instanz), JVM- & RAM-Box (RAM-Limit nachträglich änderbar —
    auch bei laufender Instanz, wirksam beim nächsten (Neu-)Start;
    Aikar-Checkbox + JVM_OPTS-Textarea, PATCH),
   Zeitplan-Box (Auto-Start-Checkbox, täglicher Neustart mit Zeit + Vorwarn-
    Minuten, Backup-Intervall + Behalte-Anzahl, Update-Check-Intervall,
    PATCH schedule; Hinweise zu Container-Lokalzeit und 30-min-Nachhol-Fenster),
    kompakter Mod-Liste (Zusammenfassung aktiv/deaktiviert/Größe, Namensfilter,
    ein-/ausklappbar, eigene Scrollfläche, kurze An/Aus-Buttons,
    Update-Check: 'Updates prüfen' → je Mod 'Update'-Button + Versions-Chip,
    'Alle aktualisieren' mit Fortschrittsbalken über den update-Job),
    Konsole (freie RCON-Befehle mit Verlauf per Pfeiltasten, zeigt Modus
    aus /api/settings), Whitelist-Editor (Liste mit Entfernen, Hinzufügen,
    Speichern → whitelist.json inkl. UUID-Auflösung, 'Auf Server laden'
    = RCON-Reload bei laufender Instanz, offline-mode-Erkennung),
    Einstellungs-Editor (server.properties) mit Formular-/Raw-Umschalter:
    Formular-Modus rendert bekannte Keys per Schema (bool → Auswahl true/false,
    enum → Auswahlliste, int → Zahlenfeld mit min/max, str → Textfeld;
    gespeicherte Werte außerhalb der Auswahl werden als '(aktueller Wert)'-
    Option bewahrt statt stillschweigend auf den Standard umgeschrieben),
    verwaltete Keys (server-port/level-name/enable-rcon/rcon.port/
    rcon.password/query.port) gesperrt mit Hinweis, unbekannte Keys
    unter 'Weitere Eigenschaften' raw editierbar; Raw-Modus = freie Liste;
    Speichern validiert serverseitig (400 mit Key-Namen),
    Such-Tab für Einzel-Mods
   (Modrinth/CurseForge, Ziel-Instanz per Dropdown, kein Hauptserver mehr;
    Treffer der Ziel-Instanz sind per Datei-Hash als 'Installiert' markiert —
    grün hervorgehobene Karte, Tag neben dem Titel, gesperrter Button — und
    lassen sich nicht doppelt installieren; nach Erfolg wird lokal markiert;
    Pflicht-Abhängigkeiten werden automatisch mitinstalliert (Modrinth aus
    den Versions-Metadaten, CurseForge best effort aus dem Relations-Feld)
    und werden während des Downloads als 'Mit installiert: …' angezeigt),
  Modpacks-Tab (globale Modpack-Suche + Installation + Upload .mrpack/.zip,
  Ziel-Instanz wählbar; ohne Ziel: Treffer/Upload direkt als neuer Server
  erstellbar über den „Neuer Server“-Button bzw. den Erstellen-Dialog mit
  Name/Versionswahl/RAM/EULA; Filter nach MC-Version und Loader;
   Suchergebnisse laden automatisch beim Tab-Öffnen),
   Statistik-Tab (persistenter Verlauf: CPU/RAM-Gesamt-Charts + Spieler-
   Linien je Instanz mit Legende, Zeitraum 6 h/24 h/7 Tage/30 Tage,
   serverseitig gebuckett über /api/history; SVG-Charts ohne Framework),
   Instanz-Badge in der Topbar; responsiv: fluides Layout (clamp-Breiten,
   max 1320 px), Breakpoints 1020/760/640/420 px — ≤1020 px einspaltige
   Übersicht, ≤760 px einspaltige Formulare/Detail-Grid + 2 KPI-Spalten,
   ≤640 px Mobile-Version mit fester Bottom-Navigation (Safe-Area-Insets,
   Toasts darüber), 2-spaltigen Schnellaktionen und 44 px Tap-Flächen,
   ≤420 px Feintuning; hover-Lifts auf Touch-Geräten deaktiviert;
   Detail öffnet auf Mobil automatisch gescrollt (x aktiv)
- tests/ – pytest-Suite (API, Instanzen, Runtime mit Fake-Docker, Katalog-Mocks,
  Modpack-Mocks inkl. mrpack/CF-ZIP, Upload-End-to-End, CF-Download/Redirects,
  Overrides-Pfadschutz, SLP, Modrinth, CurseForge-API inkl. Key-Guard/
  Suche/Pack-Installation, Server-aus-Modpack-Abläufe, Klonen, Import, Welt-
  Download/Upload, JVM-Flags, Disk-Usage, Scheduler)
- Dockerfile – Python 3.12-slim, UID 1000, docker-Paket, tzdata (für TZ im
  Scheduler), Healthcheck (Startphase 30 s); /app nur lesend (root-eigentümlich)
- docker-compose.yml – init (Ownership-Fix 1000:1000 auf /data, idempotent,
  Fremdordner-Guard: kein chown bei Daten ohne instances/, restart
  on-failure; Dashboard startet erst nach Erfolg) + Dashboard (Docker-Socket-
  Mount!, gehärtet: user 1000:1000 + group_add ${DOCKER_GID:-999} für die
  Socket-Gruppe des Hosts) + Backup-Daemon (Fehler landen sichtbar im Log);
  Compose-Interpolation per .env (Vorlage .env.example): DOCKER_GID,
  MCDATA_DIR (leer = Named Volume mcdata, sonst Bind-Mount — ZimaOS:
  /DATA/AppData/mc-dashboard/data), BACKUPS_DIR (Default ./backups),
  DASHBOARD_HTTP_PORT (Default 8080) sowie Passthrough mit Defaults für
  DASHBOARD_API_KEY/CF_API_KEY/CORS_ORIGINS/TZ/ALERT_*/INSTANCES_*;
  Instanz-Ports 25570+ direkt auf dem
  Host; kein minecraft-Service mehr; ZimaOS-Weg: SERVER-SETUP.md §11

## Wichtige Endpunkte
- GET /api/health, GET /api/settings (Legacy-Felder für Bestands-APIs)
- GET /api/status – Docker-Ressourcen (CPU/RAM) aller laufenden MC-Container
- GET /api/mods, DELETE /api/mods/{filename}  (Legacy: zentraler mods-Ordner, ohne UI)
- GET /api/modrinth/search, POST /api/modrinth/download, GET /api/modrinth/jobs/{id}
  (Download: Pflicht-Abhängigkeiten werden best effort mitinstalliert —
  Modrinth per Versions-Metadaten, CurseForge per Relations-Feld der Datei;
  nur bei Instanz-Ziel, with_dependencies abschaltbar)
- GET /api/curseforge/search, POST /api/curseforge/download  (benötigt CF_API_KEY)
- GET /api/modpacks/search?q=&source=modrinth|curseforge&loader=&game_version= –
  globale Modpack-Suche (ohne Instanzbezug, für 'Server aus Modpack erstellen',
  optional gefiltert nach Loader und MC-Version)
- GET /api/modpacks/versions?project_id=&source= – Versionen eines Modpacks
  (Modrinth-Versionen bzw. CurseForge-Pack-Dateien) für die Versionswahl
  beim Server-Erstellen
- POST /api/instances/from-pack {project_id, source?, version_id?/file_id?, name?,
  memory?, port?, accept_eula, prefer_server_pack=true} – neuen Server direkt
  aus einem Modpack erstellen (MC-Version/Loader aus dem Pack; Antwort:
  instance + job)
- POST /api/instances/from-pack-upload (multipart: file, name?, memory?,
  port?, accept_eula) – neuen Server aus hochgeladenem .mrpack/.zip erstellen
- GET /api/catalog/mc-versions, GET /api/catalog/loaders?game_version=
- GET/POST /api/instances, GET/PATCH-Update/DELETE /api/instances/{id}
  (PATCH: name/memory/jvm_opts/use_aikar/tags/port/schedule — tags ist die
  Tag-Liste (max 8, validiert), port der Spiel-Port (nur bei gestoppter
  Instanz, RCON-Port = port+1000 wird mitreserviert); schedule ist der
  Zeitplan {auto_start, restart{enabled,time,warn_minutes}, backup{enabled,
  interval_hours,keep}, update_check{enabled,interval_hours}}, merge nur
  übergebener Felder; Container-Status enthält
  started_at = letzter Container-Start, für die Uptime-Anzeige in der
  Übersicht; Detail liefert disk = Speicher-Aufschlüsselung),
  POST /api/instances/import (multipart: file=.zip/.tar.gz, name, loader,
  game_version, loader_version?, port?, memory?, accept_eula) – bestehenden
  Server-Ordner als neue Instanz einbinden,
  POST /api/instances/{id}/clone {name?} – Instanz als Vorlage klonen
  (409 bei laufender Quelle; Name ohne Angabe → '{Name} 2'),
  GET /api/instances/{id}/world (world_dir/exists/size_bytes),
  GET /api/instances/{id}/world/download (Welt als .zip),
  POST /api/instances/{id}/world/upload (multipart file=.zip/.tar.gz,
  ersetzt die Welt; nur bei gestoppter Instanz, sonst 409)
- GET /api/instances/live – SLP-Ping aller laufenden Instanzen (parallel,
  Spieler/MOTD/Version für die Übersicht; gestoppte fehlen)
- GET .../logs?tail=N (Snapshot), GET .../logs/stream?tail=N – Live-Logs als
  Server-Sent Events (tail-Rückstand + Echtzeit; Keepalive alle 15 s; endet
  beim Container-Stopp; Frontend liest per fetch mit X-API-Key und fällt
  auf 5-s-Polling zurück)
- GET /api/history?hours=1..720 – gebuckette Zeitreihen (CPU/RAM gesamt,
  Spieler gesamt + je Instanz + Namen) aus dem SQLite-Verlauf
- POST /api/instances/{id}/start|stop|restart, GET .../logs, GET/DELETE .../mods[/{filename}]
- GET/POST .../config (server.properties lesen/schreiben; GET liefert zusätzlich
  schema = Schema bekannter Keys für den Formular-Editor; POST validiert
  bekannte Keys (bool/enum/int mit Bereichen) und liefert
  restart_required = Server läuft), POST .../rcon (Whitelist-Aktionen
  op/deop/kick/ban/pardon/list/banlist, nur bei laufender Instanz, sonst 409),
  GET .../players (RCON 'list' geparst: online/max/names)
- POST .../console {command} – freies RCON-Kommando (nur laufend, sonst 409);
  RCON_CONSOLE_MODE=whitelist (Standard): nur freigegebene Befehle
  (op/deop/kick/ban/pardon/banlist/list/whitelist/say/me/msg/tell/w/seed/
  gamerule/difficulty/weather/time + RCON_CONSOLE_WHITELIST-Zusätze, sonst
  403 mit Erlaubt-Liste), RCON_CONSOLE_MODE=free: beliebige Befehle;
  führende Slashes und Steuerzeichen werden entfernt
- GET/POST .../whitelist, POST .../whitelist/reload – whitelist.json direkt
  bearbeiten (auch bei gestoppter Instanz): GET liefert Einträge + online_mode
  + running + corrupt-Flag; POST {entries:[Namen]} ersetzt die Datei (UUIDs
  per Mojang-API/offline berechnet, unresolved im Antwort), bei laufendem
  Server best-effort 'whitelist reload'; .../reload → RCON-Reload
  (409 gestoppt, 503 RCON nicht erreichbar)
- POST .../mods/update-check – Update-Prüfung aller Mods (Modrinth-SHA1,
  CurseForge-Fingerprint mit CF_API_KEY; sonst curseforge_enabled=false);
  liefert je Mod status/installed/latest (max 300 Dateien)
- POST .../mods/update {filenames?} – 'Alles aktualisieren' als Job
  (kind='update'; ohne Liste = alle mit update_available; 409 wenn nichts
  offen ist), Fortschritt über /api/jobs/{id}
- GET/POST .../backups, GET/DELETE .../backups/{name}, POST .../backups/{name}/restore,
  GET .../backups/{name}/download  (tar.gz-Snapshots in /data/backups/{id})
- Runtime: Host-Pfad für Instanz-Binds wird autoritativ vom Docker-Daemon
  erfragt (Eigene-Container-Mount-Source); mountinfo nur als Fallback —
  Docker-Desktop-Volume-Roots sind als Bind-Quelle unbrauchbar
- Runtime: Instanz-Netzwerk = Dashboard-Netzwerk (_network_of_dashboard);
  hängt das Dashboard nur am Default-Bridge (ZimaOS-Import ohne Compose-
  Netzwerk, dort löst Docker keine Namen auf → RCON Errno -2), wird
  automatisch 'mc-dashboard-net' angelegt/verbunden und Instanzen landen
  darin (RCON via Container-Name: mc-inst-<id>, SLP ebenso)
- GET .../modpacks/search?q=&source=modrinth|curseforge,
  POST .../modpacks/install {project_id, version_id?, force?, source?, file_id?}
  (source=curseforge: project_id = CF-Mod-ID oder Slug, file_id = numerische Pack-Datei),
  GET .../modpacks/update-check – 'Update verfügbar' für das installierte
  Modpack (installed/checkable/source/project_id/installed_pack/latest/
  compatible/update_available; Modrinth: neueste kompatible Version,
  CurseForge: neueste stabile Datei + Kompatibilität aus gameVersions-Tags;
  checkable=false bei Uploads ohne Projekt-Quelle),
  POST .../modpacks/update {version_id?/file_id?} – neuere Pack-Version
  erneut in die Instanz installieren (force, Job über /api/jobs/{id})
- GET /api/jobs/{job_id} – Fortschritt (phase, done/total, downloaded, error)

## Konfiguration (Env-Variablen)
DASHBOARD_API_KEY, CORS_ORIGINS, INSTANCES_DIR (/data/instances),
INSTANCES_PORT_BASE (25570), INSTANCES_MEMORY (2G), INSTANCES_HOST_DIR
(leer = Auto-Erkennung), CF_API_KEY (CurseForge-API-Key, kostenlos:
console.curseforge.com; ohne Key antworten die CF-Endpunkte mit 503),
CURSEFORGE_API (Override für die API-Basis-URL), MOJANG_API (Override,
Default https://api.mojang.com — UUID-Auflösung für die Whitelist),
RCON_CONSOLE_MODE (whitelist|free, Default whitelist — freie RCON-Konsole),
RCON_CONSOLE_WHITELIST (Komma-separierte Zusatzbefehle für den
Whitelist-Modus), HISTORY_DB (SQLite-Verlauf, Default /data/history.db),
HISTORY_INTERVAL (Sampling-Sekunden, Default 30, min 10),
HISTORY_RETENTION_DAYS (Default 30),
Crash-Watchdog/Alerts (optional): ALERT_WEBHOOK_URL (Discord-kompatibler
Webhook, POST {"content": …}), TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID
(Telegram sendMessage), ALERT_EVENTS (Komma-separiert crash,start,stop,
update — 'update' = geplanter Mod-Update-Check, Default ohne 'update').
Scheduler (Zeitplan je Instanz, keine Env nötig): Zustand liegt in
scheduler_state.json neben INSTANCES_DIR; Uhrzeit = Container-Lokalzeit
(für lokale Zeit im Dashboard-Container TZ setzen, z. B. TZ=Europe/Berlin).
Legacy (ohne UI, für Bestands-APIs): MODS_DIR, MC_HOST, MC_PORT, MC_VERSION, MOD_LOADER.

## Lokal starten
pip install -r requirements.txt
WICHTIG lokal: INSTANCES_DIR setzen (Default ist ein Docker-Pfad), z.B.:
$env:INSTANCES_DIR="$PWD\data\instances"
python -m uvicorn app.main:app --port 8080
Frontend: http://localhost:8080
Hinweis: Instanz-Start/Stop benoetigt einen Docker-Daemon (ohne -> 503).

## Docker/ZimaOS starten
docker compose up -d --build   # Dashboard 8080; Instanzen 25570+ (manuell, keine Auto-Starts)
Compose-Parameter (DOCKER_GID, MCDATA_DIR, BACKUPS_DIR, DASHBOARD_HTTP_PORT)
kommen aus .env — Vorlage .env.example; ZimaOS-Einrichtung: SERVER-SETUP.md §11.

## Tests
python -m pytest tests/   # 499 Tests (Windows: 1 Skip — Symlink-Test, CI/Linux: alle grün)
CI: .github/workflows/ci.yml — ruff check app tests, mypy (app/, Regeln in
pyproject.toml inkl. dokumentierter Ausnahmen), pytest; Dev-Abhängigkeiten
in requirements-dev.txt. Docker-Image: .github/workflows/docker-publish.yml
— bei Push auf main/tags nach ghcr.io/chronixx4/minedocker:latest
(Grundlage für die ZimaOS-UI-Installation, Vorlage INSTALL-ZIMAOS.md).
Abgedeckt: API, Instanz-CRUD/Ports/EULA, Runtime-Env/Start/Stop (Fake-Docker),
Katalog (alle Loader + Fehlerfaelle), Modpack-Install (Erfolg/Fehler/Speicher/Pfade),
mrpack-Kompatibilitaet (altes + neues Format), Upload-End-to-End (mrpack +
CurseForge inkl. Overrides/Redirects), Job-Lifecycle, 409-Overwrite,
Mod-Toggle je Instanz, API-Key, CORS, Pfad-Traversal, SLP, Modrinth,
CurseForge (Key-Guard 503, Suchparameter/Key-Header, Dateiauswahl,
Einzel-Mod-Download inkl. Relations-Dep-Auflösung/-Mitinstallation,
Pack-Direktinstallation inkl. Fehlern),
Server-aus-Modpack (Modrinth/CurseForge inkl. Server-Pack-Datei,
Namens-Sanitizing/Suffixe, EULA/Loader/MC-Validierung, Cleanup,
Upload-Variante, env-Filter fuer nur-Client-Dateien, globale Suche),
Klonen (Kopie ohne packs, neue ID/Ports, 409 laufend, API),
PATCH-Einstellungen (Name/RAM/JVM-Flags inkl. Validierung/Env-Auswirkung),
Welt (Erkennung, Disk-Usage, Download-Zip, Upload zip/tar.gz inkl. Layouts,
409 laufend, Traversal-Schutz), Server-Import (Welt-Unterordner/Root-Layout,
Props-Anpassung, EULA-Zwang, Kollisionen, Cleanup, Traversal),
Konsole (Whitelist-/Free-Modus, 403/409/400, Steuerzeichen), Whitelist
(Datei lesen/schreiben inkl. Defekt-Toleranz, Mojang-/Offline-UUID,
Dedupe/Limits, RCON-Reload), Mod-Updates (Murmur2-Vektor + Java-Referenz,
Modrinth-SHA1-Check inkl. newer_than_latest/not_found, CF-Fingerprint
inkl. Batch, Update-Job End-to-End inkl. deaktiviert-Zustand/Fehler-Summary),
Verlauf (Record/Prune/Bucketing, sample_tick mit gemockten Docker-/SLP-
Quellen inkl. Ping-Fehler, /api/history), Live-Logs (SSE-Stream inkl.
JSON-Zeilensicherheit, 404, gestoppter Container, follow_logs-Decode/
Fehler-Toleranz), Sicherheits-Snapshots (pre-install/pre-restore inkl.
Ausschlüsse/Rotation/Restore und Install-/Restore-Hooks), Job-Persistenz
(Spiegeln/Restore/Abbruch-Markierung/Eviction/Verwaiste-Prune), Watchdog (Event-Handling inkl. erwarteter Stops, Alerts Discord/Telegram
inkl. Ereignisfilter/Fehler-Toleranz, Event-Schleife mit Fake-Docker),
Scheduler (Zeitplan-Validierung/Merge/Klon-Übernahme, PATCH schedule,
Auto-Start mit Fake-Docker, geplanter Neustart mit Vorwarnungen/Nachhol-
Fenster/gestoppter Instanz/fehlerhafter RCON, zeitgesteuerte Backups mit
Rotation, Update-Check-Alerts inkl. Fehler-Toleranz, Zustandsdatei
inkl. Defekt-Toleranz/Verwaisten-Prune, Loop-Stop),
Formular-Editor (Schema-Antwort, bool/enum/int-Validierung inkl.
Normalisierung/Bereiche, verwaltete Keys auf Dateistand gezwungen/interne
Aufrufer via managed_ok, unbekannte Keys frei), Tags (Erstellen/PATCH inkl.
Normalisierung/Dedupe/Limits, Klon-Übernahme, List-Antwort), Port-Wechsel
(gestoppt OK inkl. RCON-Port-Verschiebung + Start-Verwendung, Kollisionen
409, laufend 409, Docker unprüfbar 503 fail-closed, atomare Validierung+
Persistenz unter Lock inkl. Race-Test), Modpack-Update-Check (ohne Pack/
upload nicht prüfbar, Modrinth inkl. Loader-Filter über gemeinsamen Helfer
+ aktuell-Fall, CurseForge inkl. Key-Guard/Kompatibilität/fehlende
Versions-ID → einmal pinnen), Pack-Update (400 ohne Pack, force-Rufe
Modrinth/CF inkl. version_id/file_id, CF pinnt gewählte Datei-ID via
resolve_pack 6-Tuple, End-to-End-Job mit Meta-Aktualisierung) in
tests/test_new_features.py.

## Bekannte Eigenheiten
- mrpack-Format: aktuelle Packs tragen die MC-Version in dependencies.minecraft
  (gameVersion leer); beide Formate werden gelesen.
- CurseForge: Suche filtert nach gameId/classId/gameVersion/modLoaderType;
   Loader-Kompatibilitaet eines Packs steht erst im manifest.json (UI zeigt
   'wird bei Installation geprüft'); Dateien ohne downloadUrl laufen über den
   Website-Redirect (Dateiname aus der CDN-URL). Key-Guard = 503 ohne CF_API_KEY.
   Pflicht-Abhängigkeiten einzelner Mods stehen im Relations-Feld der Datei
   (relationType 3) — das Feld fehlt bei vielen Dateien, die Auflösung ist
   daher best effort (ohne Feld werden keine Deps mitinstalliert).
  Sicherheits-Checks für CF-Downloads: Download-URLs werden auf
  CurseForge-CDN-Hosts beschränkt (www.curseforge.com/*.forgecdn.net, inkl.
  finale Redirect-URL) und SHA1 aus der CF-API wird geprüft (bis 64 MiB).
- TestClient (starlette 1.6) bricht Hintergrund-Tasks nach dem Response ab
  (Portal cancel_remaining=True) — Produktionsbetrieb (uvicorn) laeuft die
  Tasks separat; Route-Tests pruefen daher nur den Job-Start, End-to-End
  ueber Direktaufrufe.
- PaperMC: Fill-API v3 (fill.papermc.io), v2 ist abgeschaltet (410).
- Bukkit/Spigot: kein Auto-Download (BuildTools), Loader-Version leer lassen.
