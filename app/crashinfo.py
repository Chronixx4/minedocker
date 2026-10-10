"""Crash-Diagnose: erklärt in Klartext, warum ein Server abgestürzt ist.

Quellen sind das Ende des Container-Logs und der neueste Bericht aus
crash-reports/. Bekannte Muster (fehlende Mod-Abhängigkeit, falsche
Java-Version, zu wenig RAM, Client-Mod auf dem Server, belegter Port, …)
werden erkannt und mit einem konkreten Hinweis versehen. Nichts erkannt →
keine Befunde, die Oberfläche zeigt dann nur die letzten Logzeilen.
"""
import re
import time
from pathlib import Path

# Crash-Reports älter als das sind nicht vom aktuellen Absturz
_REPORT_MAX_AGE = 15 * 60
_REPORT_MAX_BYTES = 256 * 1024
_MAX_EVIDENCE = 6
_TAIL_LINES = 25

# Klassendatei-Version → Java-Version (Java N = 44 + N)
_CLASS_VERSION_RE = re.compile(r"class file version (\d+)(?:\.\d+)?")
_RUNTIME_VERSION_RE = re.compile(
    r"recognizes class file versions up to (\d+)(?:\.\d+)?")


def _java_from_class(version: str) -> int | None:
    try:
        number = int(version) - 44
    except ValueError:
        return None
    return number if 1 <= number <= 40 else None


def _lines_matching(text: str, pattern: re.Pattern) -> list[str]:
    out = []
    for line in text.splitlines():
        if pattern.search(line):
            clean = line.strip()
            if clean and clean not in out:
                out.append(clean[:300])
        if len(out) >= _MAX_EVIDENCE:
            break
    return out


def _block_after(text: str, header: re.Pattern, max_lines: int = 8) -> list[str]:
    """Aufzählungszeilen direkt nach einer Überschrift (Fabric/Forge listen
    fehlende oder unverträgliche Mods so auf)."""
    lines = text.splitlines()
    for idx, line in enumerate(lines):
        if header.search(line):
            block: list[str] = []
            for follow in lines[idx + 1: idx + 1 + max_lines * 2]:
                stripped = follow.strip()
                if not stripped:
                    if block:
                        break
                    continue
                if re.match(r"^(-|\*|Mod ID|\||\[)", stripped) or "requires" in stripped \
                        or "Requested by" in stripped:
                    block.append(stripped[:300])
                elif block:
                    break
                if len(block) >= max_lines:
                    break
            return block
    return []


# (Schlüssel, Titel, Hinweis, Muster für Beleg-Zeilen)
_RULES: list[tuple[str, str, str, re.Pattern]] = [
    ("eula", "EULA nicht akzeptiert",
     "Die Minecraft-EULA muss akzeptiert sein (eula.txt mit eula=true).",
     re.compile(r"agree to the EULA", re.I)),
    ("port", "Port ist schon belegt",
     "Ein anderer Server oder Dienst nutzt denselben Port. Port in den "
     "Einstellungen ändern oder den anderen Server stoppen.",
     re.compile(r"FAILED TO BIND TO PORT|Address already in use", re.I)),
    ("memory", "Zu wenig Arbeitsspeicher",
     "Dem Server ging der RAM aus. In den Einstellungen unter „JVM & RAM“ "
     "mehr RAM geben (große Modpacks brauchen oft 6 bis 10 GB).",
     re.compile(r"java\.lang\.OutOfMemoryError|There is insufficient memory|"
                r"Cannot allocate memory", re.I)),
    ("client_mod", "Client-Mod auf dem Server",
     "Eine Mod ist nur für das Spiel auf dem PC gedacht und läuft nicht auf "
     "einem Server. Die betroffene Mod (siehe unten) aus dem Mod-Ordner "
     "entfernen.",
     re.compile(r"for invalid dist DEDICATED_SERVER|"
                r"Attempted to load class net/minecraft/client|"
                r"NoClassDefFoundError: net/minecraft/client|"
                r"ClassNotFoundException: net\.minecraft\.client|"
                r"Environment type SERVER is not supported|"
                r"cannot be loaded on a dedicated server", re.I)),
    ("duplicate", "Mod doppelt installiert",
     "Dieselbe Mod liegt mehrfach im Mod-Ordner (meist zwei Versionen). "
     "Die ältere Datei löschen.",
     re.compile(r"Found duplicate mods|Duplicate mods found|duplicate mod id|"
                r"DuplicateModsFoundException|Mod ID .* is provided by", re.I)),
    ("mixin", "Mod-Konflikt (Mixin)",
     "Eine Mod verändert Spielcode auf eine Weise, die mit dieser Version "
     "oder einer anderen Mod nicht zusammenpasst. Die genannte Mod "
     "aktualisieren oder vorübergehend deaktivieren.",
     re.compile(r"Mixin apply (?:for mod \S+ )?failed|MixinApplyError|"
                r"InvalidMixinException|MixinTransformerError", re.I)),
    ("world", "Welt-Daten beschädigt",
     "Beim Laden der Welt ist ein Fehler aufgetreten. Ein Backup "
     "zurückspielen oder die genannte Region-Datei prüfen.",
     re.compile(r"Exception reading .*\.mca|Failed to load level|"
                r"Corrupted chunk|ReportedException: Loading NBT data", re.I)),
]

_MISSING_DEP_HEADER = re.compile(
    r"Missing or unsupported mandatory dependencies|"
    r"Incompatible mods? found|Unmet dependency listing|"
    r"Mod resolution failed|Some of your mods are incompatible", re.I)
_MISSING_DEP_LINE = re.compile(
    r"requires (?:any version|version [^ ]+|.{0,40}?) of (?:mod )?'?[\w\- ]+'?|"
    r"which is missing|Mod ID: '[^']+', Requested by|"
    r"depends on .* which is (?:missing|not installed)", re.I)
_SUSPECT_RE = re.compile(
    r"Suspected Mods?:\s*(.+)|-- Mod loading issue for: (\S+) --|"
    r"Mixin apply (?:for mod (\S+) )?failed|from mod (\S+)\]?", re.I)
_OLD_FORGE_JAVA_RE = re.compile(
    r"ClassLoaders\$AppClassLoader cannot be cast to (?:class )?java\.net\.URLClassLoader|"
    r"Unsupported Java detected|UnsupportedClassVersionError|"
    r"Minecraft \d+(?:\.\d+)* requires running the server with Java \d+", re.I)


def _java_finding(text: str) -> dict | None:
    if not _OLD_FORGE_JAVA_RE.search(text):
        return None
    evidence = _lines_matching(text, _OLD_FORGE_JAVA_RE)
    needed = None
    match = _CLASS_VERSION_RE.search(text)
    if match:
        needed = _java_from_class(match.group(1))
    have = None
    match = _RUNTIME_VERSION_RE.search(text)
    if match:
        have = _java_from_class(match.group(1))
    if needed and have:
        hint = (f"Der Server oder eine Mod braucht Java {needed}, der Container hat "
                f"Java {have}. In den Einstellungen unter „JVM & RAM“ Java {needed} wählen.")
    elif needed:
        hint = (f"Der Server oder eine Mod braucht Java {needed}. In den Einstellungen "
                f"unter „JVM & RAM“ Java {needed} wählen.")
    elif "URLClassLoader" in text:
        hint = ("Alte Forge-Versionen (bis 1.16) laufen nur mit Java 8. In den "
                "Einstellungen unter „JVM & RAM“ Java 8 wählen.")
    else:
        hint = ("Die Java-Version passt nicht zu Server oder Mods. In den "
                "Einstellungen unter „JVM & RAM“ eine andere Java-Version wählen "
                "oder „Automatisch“ einstellen.")
    return {"key": "java", "title": "Falsche Java-Version", "hint": hint,
            "evidence": evidence}


def _dependency_finding(text: str) -> dict | None:
    evidence = _block_after(text, _MISSING_DEP_HEADER)
    if not evidence:
        evidence = _lines_matching(text, _MISSING_DEP_LINE)
    if not evidence:
        return None
    return {"key": "dependency", "title": "Fehlende oder unpassende Mod-Abhängigkeit",
            "hint": ("Eine Mod braucht eine andere Mod (oder eine andere Version "
                     "davon). Die genannte Mod über den Modbrowser installieren "
                     "bzw. auf die verlangte Version bringen."),
            "evidence": evidence[:_MAX_EVIDENCE]}


def suspected_mods(text: str) -> list[str]:
    """Mod-Namen, die Forge/Fabric/Mixin im Absturz als Verdächtige nennen."""
    found: list[str] = []
    for match in _SUSPECT_RE.finditer(text):
        name = next((g for g in match.groups() if g), "").strip().rstrip("]")
        if name and name.upper() != "NONE" and name not in found:
            found.append(name[:120])
        if len(found) >= 5:
            break
    return found


def analyze(text: str) -> list[dict]:
    """Befunde aus Log-/Crash-Text, wichtigste zuerst."""
    findings: list[dict] = []
    java = _java_finding(text)
    if java:
        findings.append(java)
    dep = _dependency_finding(text)
    if dep:
        findings.append(dep)
    for key, title, hint, pattern in _RULES:
        evidence = _lines_matching(text, pattern)
        if evidence:
            findings.append({"key": key, "title": title, "hint": hint,
                             "evidence": evidence})
    return findings


def latest_crash_report(directory: Path, since: float | None = None) -> Path | None:
    """Neuester Bericht aus crash-reports/, falls er zum Absturz passt."""
    reports = directory / "crash-reports"
    try:
        candidates = [p for p in reports.iterdir()
                      if p.is_file() and p.suffix == ".txt"]
    except OSError:
        return None
    if not candidates:
        return None
    newest = max(candidates, key=lambda p: p.stat().st_mtime)
    limit = since if since is not None else time.time() - _REPORT_MAX_AGE
    return newest if newest.stat().st_mtime >= limit else None


def _read_head(path: Path) -> str:
    try:
        with path.open("rb") as fh:
            return fh.read(_REPORT_MAX_BYTES).decode("utf-8", errors="replace")
    except OSError:
        return ""


def diagnose(directory: Path, log_lines: list[str], exit_code: int | None = None,
             since: float | None = None) -> dict:
    """Diagnose eines Absturzes; wirft nie."""
    report = latest_crash_report(directory, since)
    report_text = _read_head(report) if report else ""
    log_text = "\n".join(log_lines or [])
    text = report_text + "\n" + log_text
    findings = analyze(text)
    # Exit 137 ohne Java-Meldung: der Container hat sein Speicherlimit
    # gesprengt und wurde vom Kernel beendet (OOM-Kill)
    if exit_code == 137 and not any(f["key"] == "memory" for f in findings):
        findings.insert(0, {
            "key": "memory", "title": "Zu wenig Arbeitsspeicher",
            "hint": ("Der Server wurde wegen Speichermangel beendet (Exit-Code 137). "
                     "In den Einstellungen unter „JVM & RAM“ mehr RAM geben."),
            "evidence": []})
    return {
        "at": int(time.time()),
        "exit_code": exit_code,
        "summary": findings[0]["title"] if findings else "Ursache nicht erkannt",
        "findings": findings,
        "suspects": suspected_mods(text),
        "crash_report": report.name if report else None,
        "log_tail": [line[:300] for line in (log_lines or [])[-_TAIL_LINES:]],
    }
