# Roadmap: Richtung SpawnBox (UI + Features)

Analyse der SpawnBox-Screenshots gegen den Stand von minedocker.
**Grundsatz:** Alle bestehenden Funktionen bleiben erhalten; der Umbau ist
Additiv. Der SpawnBox-Look ist ein Dark-Theme mit grünem Akzent,
Instanz-Workspace statt Übersicht+Dialog.

---

## 1. Was SpawnBox zeigt (Feature-Inventar aus den Screenshots)

1. **Server-Tabs** oben (jede Instanz ein Tab, „+ Add Server", Seitenzähler)
2. **Server-Info-Panel**: MC-Version, Server-Typ (Loader), Difficulty,
   Game Mode, Performance-Bars (TPS / CPU / RAM), Storage, Adresse für
   lokale Spieler und für Internet-Spieler („easy address"), Modpack-Chip
3. **Server-Controls**: Welt-Auswahl (Multi-World!), Uptime-Timer,
   Chat-Eingabe (sendet `say` ins Spiel), Start/Restart/Stop,
   Toggles: „Internet-Spieler erlauben" (UPnP + Easy-Address),
   „Öffentliches Web-Dashboard", „Start bei PC-Neustart"
4. **Backup-Panel**: „Protected"-Badge, letztes Backup, Anzahl + Gesamt-
   größe, „Backup Now", Backup-Timeline mit Zeitpunkten
5. **Connected-Players-Tabelle**: Kopf-Avatar, Name, Spielzeit, Position
   (x/z + y), Health, Air, XP/Level
6. **Workspace-Tabs**: Activity & Chat, Scoreboard, Player Engagement
   (Advancement-Fortschritt mit Balken/Rarity/Rang), Player Relationships,
   Access Control, Investigate, **Map** (Live-Weltkarte mit Heat-Maps:
   Visits, Deaths, Bases, Portals, Spawns; Overworld/Nether/End),
   Modrinth-Mods, Modrinth-Datapacks, World Settings, **Hibernation**
7. **Landingpage** (Marketing — für uns irrelevant)

## 2. Abgleich mit minedocker

| SpawnBox-Feature | minedocker heute | Machbarkeit |
|---|---|---|
| Server-Tabs-Workspace | Kartenliste + Detail-Dialog | ✅ A — Frontend-Umbau |
| Dark/Green Design | eigenes Dark-Theme vorhanden | ✅ A — CSS-Retheme |
| Info-Panel (Version/Loader/Difficulty/GameMode) | über properties-Editor vorhanden, nicht sichtbar | ✅ A — lesend aus config/instance.json |
| CPU/RAM-Bars | vorhanden (Sparklines) | ✅ A |
| Storage-Breakdown | `disk_usage()` vorhanden | ✅ A |
| TPS-Anzeige | **fehlt** | ⚠️ B — loader-abhängig (s. u.) |
| Chat-Eingabe (say) | RCON-Konsole vorhanden | ✅ A — Shortcut in Controls |
| Start/Restart/Stop | vorhanden | ✅ |
| Auto-Start-Toggle | scheduler `auto_start` vorhanden | ✅ A — Toggle ins UI |
| „Easy Address" für Internet | fehlt (SpawnBox = gehosteter Dienst) | ⚠️ B — nur eigener Tunnel/UPnP, kein Subdomain-Dienst |
| Öffentliches Read-only-Dashboard | fehlt | ⚠️ B — separater `/public`-Read-only-View |
| Backup-Panel mit Timeline | Backups vorhanden (Liste/Restore), keine Timeline | ✅ A/B — UI + Größen-Summen |
| Players-Tabelle (Playtime) | Playtime-Leaderboard vorhanden | ✅ A |
| Position/Health/Air/XP live | **fehlt** | ❌ C — Vanilla RCON liefert das nicht; nur mit Server-Mod |
| Activity & Chat-Feed | Log-SSE-Stream vorhanden | ✅ A — Logs clientseitig parsen (join/leave/chat/advancement) |
| Scoreboard | RCON-Scoreboard-Kommandos möglich | ⚠️ B — Umsetzung über freie Konsole |
| Player Engagement (Advancements-Statistik) | **fehlt** | ❌ C — nur mit Mod/Plugin; Auswertung advancement-Meldungen aus Logs als „lite" machbar |
| Player Relationships / Investigate | fehlt | ❌ D — weggelassen (Spezial-Features von SpawnBox) |
| Access Control | Whitelist/OP/Ban vorhanden | ✅ A — in Tab einsortieren |
| **Live-Map** | fehlt | ✅/⚠️ B — BlueMap/squaremap als Mod/Plugin integrieren + Web-Viewer einbetten; Heat-Overlays (Visits/Deaths) = ❌ C |
| Modrinth-Mods/-Datapacks-Tabs | vorhanden | ✅ A — nur Umsortierung |
| World Settings | Gamerules + properties-Editor vorhanden | ✅ A — Umsortierung |
| Multi-World-Auswahl + „Create/Import World" | nur Welt ersetzen/upload | ⚠️ B — Welten-Ordner-Verwaltung: mehrere Welten speichern, per level-name umschalten (Stop → Patch → Start) |
| **Hibernation** | fehlt | ⚠️ B — Wake-Proxy + Auto-Stopp machbar (s. u.) |
| UPnP-Portfreigabe | fehlt | ⚠️ B — miniupnpc, optional + Opt-in |
| Landingpage | — | ❌ D — unnötig |

**Legende:** A = reine UI-/kleine Backend-Arbeit · B = machbar, mittlerer
Aufwand · C = nur mit Server-Mod/Plugin (Companion) · D = nicht geplant

## 3. Wichtige Machbarkeits-Details

- **TPS:** Vanilla-RCON hat kein TPS-Kommando. Paper/Bukkit: RCON `tps`
  direkt. Fabric/NeoForge/Forge: nur mit Mod (z. B. spark) — daher
  loader-abhängig anzeigen, sonst ausblenden. Fallback: MSPT-Schätzung
  weglassen statt fake-Werte.
- **Position/Health/XP je Spieler:** geht NUR mit serverseitigem Mod/Plugin
  ( scoreboard oder Companion-Mod mit HTTP-Endpoint ). Das ist der
  größte Riskio-Posten → eigene Phase, optional pro Loader.
- **Activity & Chat:** Container-Logs enthalten join/leave, Chat-Zeilen
  (`<Name> text`), Advancement-Meldungen. Der bestehende SSE-Log-Stream
  reicht aus — Parse im Frontend, kein Backend-Umbau.
- **Map:** BlueMap gibt es als Fabric/Forge/NeoForge-Mod UND
  Paper/Spigot-Plugin und bringt einen Web-Viewer mit (eigener Port oder
  Reverse-Pfad). Umsetzung: „Map aktivieren"-Toggle pro Instanz →
  Mod/Plugin in den Instanz-Ordner legen, Container-Env ergänzen,
  Viewer-Port publizieren, im Workspace als Tab per iframe einbetten.
  SpawnBox-Heat-Overlays (Visits/Deaths/Bases) sind proprietär —
  Ersatz: BlueMap-Marker; Deaths/Visits-Heat nur mit eigenem Companion → C.
- **Hibernation:** zwei Bausteine: (1) Auto-Stopp nach X Min ohne Spieler
  (Sampler-Spielerzahl vorhanden — trivial). (2) Aufwecken: Dashboard
  bindet den Spiel-Port solange der Instanz-Container gestoppt ist
  (Ports werden beim Container-Stopp freigegeben) als Mini-TCP-Proxy und
  startet bei erstem Verbindungsversuch den Container + reicht Bytes
  durch. Compose braucht dafür die Port-Range am Dashboard-Container.
  Aufwand mittel, sehr lohnendes Feature.
- **Multi-World:** Welten in `{instance}/worlds/{name}` verwalten,
  Umschalten = Stop → level-name patchen (Props-API vorhanden) → Start.
  „Create World" = leerer Ordner; Import = bestehender Welt-Upload-Pfad.
- **UPnP:** miniupnpc als subprocess/pip-Paket; Opt-in-Checkbox,
  automatisch Lease erneuern. Nur LAN-Features, klar dokumentieren.

## 4. Phasenplan

### Phase 1 — Design-System & Workspace-Umbau (nur Frontend, kein Backend-Risiko)
- Dark-Theme mit grünem Akzent vereinheitlichen (CSS-Variablen, Karten,
  Badges, Buttons im SpawnBox-Look, beibehaltene Responsive-Regeln)
- Instanz-Workspace: Tabs oben (eine Instanz = ein Tab + „+ Add Server"),
  Ersetzen des Detail-Dialogs durch Vollansicht mit Spalten-Grid:
  Info-Panel (Version/Loader/Difficulty/GameMode/Storage/Adressen),
  Controls (Start/Restart/Stop, Chat-Kurzfeld, Auto-Start-Toggle,
  Uptime), Backup-Kompaktpanel
- Vorhandene Boxen (Mods, Modpacks, Datapacks, Dateien, Whitelist,
  Gamerules, Konsole, Zeitplan) als Workspace-Tabs einsortieren
- Übersichtsseite bleibt als „Alle Server"-Startseite bestehen
- **Aufwand:** groß, aber risikoarm; Features ändern sich nicht

### Phase 2 — Quick Wins Backend+UI
- Backup-Panel: Timeline + Anzahl/Gesamtgröße + „Jetzt backuppen"-Button
  (API: Liste existiert, sizes ergänzen)
- Activity & Chat-Tab: Log-SSE parsen (Chat/join/leave/advancement) +
  Chat-Sendefeld (RCON `say`) mit Verlauf
- Uptime-Timer + „Aktualisiert vor X"-Feinschliff im Header
- Adress-Chips: lokale Adresse (host:port) + Port-Klick-Kopie (vorhanden)
  ins Info-Panel
- **Aufwand:** klein–mittel

### Phase 3 — Hibernation
- Auto-Stopp bei 0 Spielern (Timeout konfigurierbar, Status-Badge
  „Schläft")
- Wake-Proxy am Spiel-Port (Dashboard bindet Port des gestoppten
  Containers, startet bei Connect und bridged)
- Compose: Port-Range am Dashboard ergänzen; Doku/Tests
- **Aufwand:** mittel — eines der wertvollsten neuen Features

### Phase 4 — Live-Map (BlueMap/squaremap)
- Toggle pro Instanz „Weltkarte aktivieren" (loader-abhängige Auswahl:
  BlueMap für Fabric/NeoForge/Forge/Paper)
- Mod/Plugin-Installation in den Instanz-Ordner, Viewer-Port/Route,
  Einbettung als Workspace-Tab (iframe) + Statusanzeige (Render-Fortschritt)
- **Aufwand:** mittel

### Phase 5 — Multi-World
- Welten-Verwaltung (Liste/Anlegen/Import/Umschalten/Löschen) je Instanz,
  Umschalten nur gestoppt, Sicherheitskopie vor dem Wechsel
- **Aufwand:** mittel

### Phase 6 (optional) — Companion-Mod & Advanced-Stats
- Position/Health/Air/XP-Tabelle, Advancement-Fortschritt, Scoreboard-Tab:
  eigener kleiner Server-Mod (Fabric/NeoForge) bzw. Paper-Plugin, das
  Player-Daten per HTTP/RCON-Scoreboard bereitstellt; Dashboard liest aus
- Nur wenn Phase 1–5 steht; hoher Aufwand, loader-spezifische Pflege
- **Aufwand:** groß — bewusst zuletzt

### Optional-Schublade
- UPnP-Portfreigabe-Toggle, Read-only-Öffentlich-Dashboard (`/public`),
  Scoreboard-Tab über RCON

## 5. Empfehlung

1. Mit **Phase 1** starten (Look & Feel ~80 % des SpawnBox-Eindrucks,
   ohne eine einzige Backend-Änderung).
2. Dann **Phase 2 + 3** (sichtbarer Funktionsgewinn, moderater Aufwand).
3. **Phase 4 (Map)** als Killer-Feature, **Phase 5** danach.
4. **Phase 6** nur bei Bedarf — sie bindet langfristig Pflege pro
   MC-Version/Loader und ist der einzige Bereich, der „nicht sauber" in
   einer Docker-/itzg-Architektur abbildbar ist.

Nicht umsetzbar bzw. verworfen: SpawnBox-„Easy Address"-Subdomains
(gehosteter zentraler Dienst), Heat-Maps für Visits/Deaths ohne
Companion-Mod, Player-Relationships/Investigate-Forensik.
