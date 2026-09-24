/* Minecraft Dashboard – Vanilla JS, kein Framework, asynchrone API-Aufrufe. */
(() => {
  "use strict";

  const $ = (sel) => document.querySelector(sel);
  const state = {
    apiKey: localStorage.getItem("dash_api_key") || "",
    tab: "overview",
    offset: 0,
    total: 0,
    statusTimer: null,
    liveTimer: null,
    searchTimer: null,
    searchSource: "modrinth",
    packSource: "modrinth",
    // Auth (Login & Rollen): null = noch nicht geprüft
    user: null,
    role: null,
    me: null,
    authOverlay: false,
  };

  /* ---------- API-Client ---------- */
  async function api(path, opts = {}, retried = false) {
    const headers = Object.assign({}, opts.headers || {});
    if (state.apiKey) headers["X-API-Key"] = state.apiKey;
    let res;
    try {
      res = await fetch(path, Object.assign({}, opts, { headers }));
    } catch (e) {
      throw Object.assign(new Error("Server nicht erreichbar"), { status: 0 });
    }
    if (res.status === 401) {
      // Nicht angemeldet → Login-/Setup-Overlay zeigen (idempotent statt prompt)
      showAuthOverlay();
    }
    let data = {};
    try { data = await res.json(); } catch (e) { /* leere/HTML-Antwort */ }
    if (!res.ok) {
      throw Object.assign(
        new Error(data.detail || `HTTP-Fehler ${res.status}`),
        { status: res.status, filename: data.filename }
      );
    }
    return data;
  }

  // Upload mit Fortschrittsanzeige (fetch kennt keinen Upload-Progress → XHR).
  // Fehlverhalten wie api(): Error-Objekt mit .status und .message (Detail).
  function apiUpload(path, form, onProgress) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open("POST", path);
      if (state.apiKey) xhr.setRequestHeader("X-API-Key", state.apiKey);
      xhr.upload.onprogress = (ev) => {
        if (ev.lengthComputable && onProgress) {
          onProgress(Math.round((ev.loaded / ev.total) * 100));
        }
      };
      xhr.onload = () => {
        let data = {};
        try { data = JSON.parse(xhr.responseText); } catch (e) { /* leer/HTML */ }
        if (xhr.status >= 200 && xhr.status < 300) return resolve(data);
        if (xhr.status === 401) showAuthOverlay();
        reject(Object.assign(
          new Error(data.detail || `HTTP-Fehler ${xhr.status}`),
          { status: xhr.status }));
      };
      xhr.onerror = () => reject(
        Object.assign(new Error("Server nicht erreichbar"), { status: 0 }));
      xhr.send(form);
    });
  }

  // Vorprüfung eines Modpack-Archivs vor dem Upload: fängt abgebrochene
  // Downloads (leer/kein ZIP) ab, bevor riesige Dateien hochgeschickt werden.
  async function packFileProblem(file) {
    if (!/\.(zip|mrpack)$/i.test(file.name)) {
      return "Nur .zip (CurseForge-Pack) oder .mrpack (Modrinth) werden unterstützt.";
    }
    if (file.size <= 0) {
      return "Die Datei ist leer — bitte das Pack erneut herunterladen.";
    }
    try {
      const head = new Uint8Array(await file.slice(0, 4).arrayBuffer());
      if (head[0] !== 0x50 || head[1] !== 0x4b) { // „PK" = ZIP-Magic
        return "Die Datei ist kein gültiges ZIP-Archiv — vermutlich ein " +
          "abgebrochener Download. Bitte das Pack erneut herunterladen.";
      }
    } catch (e) { /* nicht prüfbar → Upload trotzdem versuchen */ }
    return null;
  }

  /* ---------- UI-Helfer ---------- */
  function toast(message, type = "info") {
    const el = document.createElement("div");
    el.className = `toast ${type}`;
    el.textContent = message; // textContent schützt vor XSS
    $("#toasts").appendChild(el);
    setTimeout(() => el.remove(), 4500);
  }
  function fmtBytes(n) {
    if (typeof n !== "number") return "–";
    if (n >= 1073741824) return (n / 1073741824).toFixed(2) + " GB";
    if (n >= 1048576) return (n / 1048576).toFixed(1) + " MB";
    if (n >= 1024) return (n / 1024).toFixed(1) + " KB";
    return n + " B";
  }
  function fmtNumber(n) {
    if (typeof n !== "number") return "–";
    if (n >= 1e6) return (n / 1e6).toFixed(1).replace(".0", "") + " Mio.";
    if (n >= 1e3) return (n / 1e3).toFixed(1).replace(".0", "") + " Tsd.";
    return String(n);
  }
  function fmtMb(mb) {
    if (typeof mb !== "number" || !isFinite(mb)) return "–";
    if (mb >= 1024) return (mb / 1024).toFixed(1) + " GB";
    return Math.round(mb) + " MB";
  }
  function fmtDate(ts) { return new Date(ts * 1000).toLocaleString("de-DE"); }
  const show = (el, on = true) => el.classList.toggle("hidden", !on);
  const setText = (el, text) => { el.textContent = text; };

  const motionOK = () =>
    !window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  /* Weiches Ein-/Ausblenden für große Flächen (Auth-Overlay, Detail-Panel).
     Kleine Elemente bleiben beim klassischen show() ohne Verzögerung. */
  const SOFT_MS = 190;
  function showSoft(el, on = true) {
    if (el._softTimer) { clearTimeout(el._softTimer); el._softTimer = null; }
    el.classList.remove("soft-hide");
    if (on) {
      el.classList.remove("hidden");
      if (!motionOK()) return;
      el.classList.remove("soft-show");
      void el.offsetWidth; // Reflow: Einstiegs-Animation von vorn starten
      el.classList.add("soft-show");
      el.addEventListener("animationend",
        () => el.classList.remove("soft-show"), { once: true });
    } else {
      if (el.classList.contains("hidden")) return;
      if (!motionOK()) { el.classList.add("hidden"); return; }
      el.classList.add("soft-hide");
      const finish = () => {
        el._softTimer = null;
        el.classList.add("hidden");
        el.classList.remove("soft-hide");
      };
      el._softTimer = setTimeout(finish, SOFT_MS + 80); // Fallback ohne animationend
      el.addEventListener("animationend", () => {
        if (!el._softTimer) return;
        clearTimeout(el._softTimer);
        finish();
      }, { once: true });
    }
  }

  /* Native Dialoge animiert schließen: kurz 'closing', dann echtes close(). */
  function closeDialog(dlg) {
    if (!dlg || !dlg.open || dlg.classList.contains("closing")) return;
    if (!motionOK()) { dlg.close(); return; }
    dlg.classList.add("closing");
    let done = false;
    const finish = () => {
      if (done) return;
      done = true;
      dlg.classList.remove("closing");
      dlg.close();
    };
    const t = setTimeout(finish, 280); // Fallback, falls animationend fehlt
    dlg.addEventListener("animationend", () => { clearTimeout(t); finish(); },
      { once: true });
  }

  // ESC schließt die drei Modal-Dialoge ebenfalls animiert
  ["users-dialog", "password-dialog", "fb-editor"].forEach((id) => {
    const dlg = document.getElementById(id);
    dlg.addEventListener("cancel", (e) => { e.preventDefault(); closeDialog(dlg); });
  });

  /* ---------- Auth: Login, Setup, Rollen, Benutzerverwaltung ---------- */
  function applyRoleUi() {
    const body = document.body;
    body.classList.toggle("role-viewer", state.role === "viewer");
    body.classList.toggle("role-admin", state.role === "admin");
    setText($("#user-name"), state.user || "Anmelden");
    const info = state.user
      ? `${state.user} · ${state.role === "admin" ? "Admin" : "Viewer (nur lesen)"}`
      : "Nicht angemeldet";
    setText($("#user-menu-info"), info);
    show($("#menu-change-password"), !!state.user);
    show($("#menu-users"), state.role === "admin");
    show($("#menu-setup"),
      !state.user && !!state.me?.setup_available && !state.me?.api_key_required);
    show($("#menu-logout"), !!state.user);
  }

  function showAuthOverlay(me = null) {
    // Idempotent: Polls liefern laufend 401 — Overlay nur einmal aufbauen
    if (state.authOverlay) return;
    if (me) state.me = me;
    const m = state.me;
    if (!m) return; // noch nicht geprüft → init() regelt es
    // Nicht anzeigen, wenn gar nichts geschützt ist (Bestandsverhalten):
    // kein API-Key, keine Benutzer, GET/POST offen.
    if (!m.api_key_required && !m.login_active && !m.setup_available) return;
    state.authOverlay = true;
    stopPolling();
    const login = $("#auth-login-form");
    const setup = $("#auth-setup-form");
    const keyForm = $("#auth-apikey-form");
    const wantLogin = !!m.login_active;
    const wantSetup = !!m.setup_available && !wantLogin;
    const wantKey = !!m.api_key_required && !wantLogin && !wantSetup;
    show(login, wantLogin);
    show(setup, wantSetup);
    show(keyForm, wantKey);
    // Setup + zugleich API-Key geschützt: Key-Feld im Setup-Formular Pflicht
    show($("#setup-key-field"), wantSetup && m.api_key_required);
    $("#setup-key").required = !!m.api_key_required;
    showSoft($("#auth-overlay"), true);
    if (wantLogin) $("#auth-user").focus();
    else if (wantSetup) $("#setup-user").focus();
    else $("#auth-apikey").focus();
  }

  function hideAuthOverlay() {
    state.authOverlay = false;
    showSoft($("#auth-overlay"), false);
  }

  function authError(sel, message) {
    const box = $(sel);
    if (message) {
      setText(box, message);
      show(box, true);
    } else {
      show(box, false);
    }
  }

  function reloadAfterAuth() {
    location.reload();
  }

  async function initAuth() {
    try {
      const headers = state.apiKey ? { "X-API-Key": state.apiKey } : {};
      const res = await fetch("/api/auth/me", { headers });
      if (res.ok) {
        const me = await res.json();
        state.me = me;
        if (me.authenticated) {
          state.user = me.username;
          state.role = me.role;
          applyRoleUi();
          return true;
        }
      }
    } catch (e) {
      // Server nicht erreichbar → wie bisher weiterlaufen, Polls zeigen Fehler
      applyRoleUi();
      return true;
    }
    applyRoleUi();
    // Geschützt, aber nicht angemeldet → Overlay statt Polls
    if (state.me.api_key_required || state.me.login_active) {
      showAuthOverlay();
      return false;
    }
    // Nichts konfiguriert: wie heute offen — Setup optional über das Menü
    return true;
  }

  $("#auth-login-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    authError("#auth-error", "");
    const btn = $("#auth-login-btn");
    btn.disabled = true;
    btn.textContent = "Anmelden…";
    try {
      const resp = await fetch("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          username: $("#auth-user").value.trim(),
          password: $("#auth-pass").value,
        }),
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        authError("#auth-error", data.detail || `Anmeldung fehlgeschlagen (${resp.status})`);
        return;
      }
      reloadAfterAuth();
    } finally {
      btn.disabled = false;
      btn.textContent = "Anmelden";
    }
  });

  $("#auth-setup-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    authError("#setup-error", "");
    if ($("#setup-pass").value !== $("#setup-pass2").value) {
      authError("#setup-error", "Passwörter stimmen nicht überein");
      return;
    }
    const btn = $("#setup-btn");
    btn.disabled = true;
    btn.textContent = "Lege an…";
    try {
      const key = $("#setup-key").value.trim();
      const resp = await fetch("/api/auth/setup", {
        method: "POST",
        headers: Object.assign(
          { "Content-Type": "application/json" },
          key ? { "X-API-Key": key } : (state.apiKey ? { "X-API-Key": state.apiKey } : {})),
        body: JSON.stringify({
          username: $("#setup-user").value.trim(),
          password: $("#setup-pass").value,
        }),
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        authError("#setup-error", data.detail || `Setup fehlgeschlagen (${resp.status})`);
        return;
      }
      reloadAfterAuth();
    } finally {
      btn.disabled = false;
      btn.textContent = "Admin anlegen";
    }
  });

  $("#auth-apikey-form").addEventListener("submit", (e) => {
    e.preventDefault();
    authError("#apikey-error", "");
    const key = $("#auth-apikey").value.trim();
    if (!key) return;
    // Key merken und verifizieren — bei Fehlschlag Overlay erneut zeigen
    state.apiKey = key;
    localStorage.setItem("dash_api_key", state.apiKey);
    state.authOverlay = false;
    initAuth().then((ok) => {
      if (ok) reloadAfterAuth();
      else state.authOverlay = false;
    });
  });

  /* ---------- Topbar-Benutzermenü ---------- */
  $("#user-menu-btn").addEventListener("click", (e) => {
    e.stopPropagation();
    show($("#user-menu-drop"), $("#user-menu-drop").classList.contains("hidden"));
  });
  document.addEventListener("click", (e) => {
    if (!e.target.closest?.("#user-menu")) show($("#user-menu-drop"), false);
  });
  $("#menu-logout").addEventListener("click", async () => {
    try {
      await fetch("/api/auth/logout", { method: "POST" });
    } finally {
      reloadAfterAuth();
    }
  });
  $("#menu-setup").addEventListener("click", () => {
    show($("#user-menu-drop"), false);
    state.me = Object.assign({}, state.me, { setup_available: true });
    showAuthOverlay(state.me);
  });
  $("#menu-users").addEventListener("click", async () => {
    show($("#user-menu-drop"), false);
    openUsersDialog();
  });
  $("#menu-change-password").addEventListener("click", () => {
    show($("#user-menu-drop"), false);
    openPasswordDialog();
  });

  /* ---------- Benutzerverwalten-Dialog (Admin) ---------- */
  function usersError(msg) { authError("#users-error", msg); }

  async function openUsersDialog() {
    $("#users-dialog").showModal();
    usersError("");
    await renderUsersList();
  }

  async function renderUsersList() {
    const list = $("#users-list");
    list.textContent = "";
    try {
      const data = await api("/api/auth/users");
      const users = data.users || [];
      show($("#users-empty"), users.length === 0);
      for (const user of users) {
        const li = document.createElement("li");
        li.className = "mod-row";
        const info = document.createElement("div");
        info.className = "mod-info";
        const name = document.createElement("span");
        name.className = "mod-name";
        name.textContent = user.username;
        const meta = document.createElement("span");
        meta.className = "mod-meta muted small";
        meta.textContent =
          `${user.role === "admin" ? "Admin" : "Viewer"} · seit ${fmtDate(user.created || 0)}`;
        info.append(name, meta);
        const actions = document.createElement("div");
        actions.className = "row";
        const roleBtn = document.createElement("button");
        roleBtn.className = "btn small-btn";
        roleBtn.textContent = user.role === "admin" ? "→ Viewer" : "→ Admin";
        roleBtn.title = "Rolle wechseln";
        roleBtn.addEventListener("click", async () => {
          try {
            await api(`/api/auth/users/${encodeURIComponent(user.username)}`, {
              method: "PATCH",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ role: user.role === "admin" ? "viewer" : "admin" }),
            });
            renderUsersList();
          } catch (e2) { usersError(e2.message); }
        });
        const passBtn = document.createElement("button");
        passBtn.className = "btn small-btn";
        passBtn.textContent = "Passwort";
        passBtn.title = "Passwort zurücksetzen";
        passBtn.addEventListener("click", async () => {
          const pw = window.prompt(`Neues Passwort für ${user.username} (min. 8 Zeichen):`);
          if (!pw) return;
          try {
            await api(`/api/auth/users/${encodeURIComponent(user.username)}`, {
              method: "PATCH",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ password: pw }),
            });
            toast(`Passwort für ${user.username} gesetzt.`, "success");
          } catch (e2) { usersError(e2.message); }
        });
        const delBtn = document.createElement("button");
        delBtn.className = "btn danger small-btn";
        delBtn.textContent = "Löschen";
        delBtn.addEventListener("click", async () => {
          if (!window.confirm(`Benutzer ${user.username} wirklich löschen?`)) return;
          try {
            await api(`/api/auth/users/${encodeURIComponent(user.username)}`,
              { method: "DELETE" });
            renderUsersList();
          } catch (e2) { usersError(e2.message); }
        });
        actions.append(roleBtn, passBtn, delBtn);
        li.append(info, actions);
        list.appendChild(li);
      }
    } catch (e) {
      usersError(`Benutzer nicht ladbar: ${e.message}`);
      show($("#users-empty"), true);
    }
  }

  $("#nu-create").addEventListener("click", async () => {
    usersError("");
    const btn = $("#nu-create");
    btn.disabled = true;
    try {
      await api("/api/auth/users", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          username: $("#nu-name").value.trim(),
          password: $("#nu-pass").value,
          role: $("#nu-role").value,
        }),
      });
      $("#nu-name").value = "";
      $("#nu-pass").value = "";
      toast("Benutzer angelegt.", "success");
      renderUsersList();
    } catch (e) {
      usersError(e.message);
    } finally {
      btn.disabled = false;
    }
  });
  $("#users-close").addEventListener("click", () => closeDialog($("#users-dialog")));

  /* ---------- Eigenes Passwort ändern ---------- */
  function pwError(msg) { authError("#pw-error", msg); }

  function openPasswordDialog() {
    $("#pw-form").reset();
    pwError("");
    $("#password-dialog").showModal();
  }

  $("#pw-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    pwError("");
    if ($("#pw-new").value !== $("#pw-new2").value) {
      pwError("Neue Passwörter stimmen nicht überein");
      return;
    }
    const btn = $("#pw-save");
    btn.disabled = true;
    try {
      await api("/api/auth/change-password", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          current: $("#pw-current").value,
          password: $("#pw-new").value,
        }),
      });
      closeDialog($("#password-dialog"));
      toast("Passwort geändert.", "success");
    } catch (e2) {
      pwError(e2.message);
    } finally {
      btn.disabled = false;
    }
  });
  $("#pw-close").addEventListener("click", () => closeDialog($("#password-dialog")));

  /* ---------- Tab-Umschaltung ---------- */
  document.querySelectorAll(".tab").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.tab = btn.dataset.tab;
      const applyTabUi = () => {
        document.querySelectorAll(".tab").forEach((b) => b.classList.toggle("active", b === btn));
        document.querySelectorAll(".panel").forEach((p) =>
          p.classList.toggle("active", p.id === `tab-${state.tab}`));
      };
      // Weicher Tab-Wechsel per View Transitions (Fallback: panel-in-Einstieg)
      if (document.startViewTransition && motionOK()) {
        // vt-tab bleibt dauerhaft gesetzt: panel-in würde sonst nach der
        // Transition (oder bei übersprungenen Übergängen) neu starten.
        document.body.classList.add("vt-tab");
        // Topbar/Tab-Leiste nur während der Transition benennen — dauerhafte
        // view-transition-names würden deren backdrop-filter (Glass) aushebeln.
        document.body.classList.add("vt-naming");
        const vt = document.startViewTransition(applyTabUi);
        const cleanup = () => document.body.classList.remove("vt-naming");
        vt.finished.then(cleanup, cleanup); // auch bei übersprungener Transition
      } else {
        applyTabUi();
      }
      if (state.tab === "servers") loadInstances();
      if (state.tab === "search") {
        // Erst Ziele (und Filter) laden, dann direkt suchen — sonst läuft
        // die Suche ins Leere, bevor die Instanz-Ziele da sind.
        loadInstanceTargets().then(() => {
          if (!$("#search-results").childElementCount) doSearch(0); // direkt laden
        });
        loadSearchFilters(); // Filter-Optionen (MC-Versionen) einmalig laden
      }
      if (state.tab === "modpacks") {
        loadInstanceTargets();
        loadMpFilters(); // Filter-Optionen (MC-Versionen) einmalig laden
        if (!$("#mp-results").childElementCount) mpSearch(0); // direkt laden
      }
      if (state.tab === "upload") loadUploadTargets();
      if (state.tab === "stats") {
        loadHistory();
        loadPlaytime();
      }
    });
  });

  /* ---------- Übersicht: KPIs, Server-Karten, Ressourcen ---------- */
  function activateTab(name) {
    const btn = document.querySelector(`.tab[data-tab="${name}"]`);
    if (btn) btn.click();
  }

  state.ovFilter = "all";     // all | running | starting | stopped | error
  state.ovSort = "name";      // name | status | players | cpu | ram | created
  state.ovSearch = "";        // Namensfilter für die Server-Karten
  state.ovInstances = null;   // letzte /api/instances-Antwort (null = Fehler)
  state.ovStatus = null;      // letzte /api/status-Antwort
  state.ovLive = {};          // id -> {id, name, port, ping} aus /api/instances/live
  state.lastLiveAt = 0;       // Zeitpunkt des letzten Live-Pings (ms)
  state.ovUpdatedAt = 0;      // Zeitpunkt der letzten Übersicht-Aktualisierung (ms)
  state.ovHistory = { cpu: [], ram: [], cont: {} }; // Verlauf für Sparklines (max. 40 Messpunkte)

  function renderBadge(runningCount) {
    const badge = $("#inst-badge");
    badge.classList.toggle("on", runningCount > 0);
    badge.classList.toggle("off", runningCount === 0);
    setText($("#inst-badge-text"),
      runningCount > 0 ? `${runningCount} aktiv` : "keine aktiv");
  }

  function instStateKey(inst) {
    if (inst.status === "running" || inst.container?.running) return "running";
    if (inst.status === "starting") return "starting";
    if (inst.status === "error") return "error";
    return "stopped";
  }

  const OV_STATE_LABELS = {
    running: "läuft", starting: "startet…", error: "Fehler", stopped: "gestoppt",
  };

  function containerStatsById() {
    const map = {};
    for (const c of state.ovStatus?.resources?.containers || []) map[c.name] = c;
    return map;
  }

  function liveEntry(inst) {
    return state.ovLive[inst.id] || null;
  }

  /* ---------- Übersicht: Helfer ---------- */
  const SVG_NS = "http://www.w3.org/2000/svg";

  function fmtUptime(startedAt) {
    const t = new Date(startedAt).getTime();
    if (!Number.isFinite(t)) return null;
    let s = Math.max(0, Math.floor((Date.now() - t) / 1000));
    const d = Math.floor(s / 86400); s -= d * 86400;
    const h = Math.floor(s / 3600); s -= h * 3600;
    const m = Math.floor(s / 60);
    if (d > 0) return `${d} d ${h} h`;
    if (h > 0) return `${h} h ${m} min`;
    if (m > 0) return `${m} min`;
    return "< 1 min";
  }

  function hostFor(inst) {
    return `${location.hostname || "localhost"}:${inst.port}`;
  }

  async function copyAddress(inst) {
    const addr = hostFor(inst);
    try {
      await navigator.clipboard.writeText(addr);
      toast(`Adresse kopiert: ${addr}`, "success");
    } catch (e) {
      toast(`Kopieren nicht möglich — Adresse: ${addr}`, "error");
    }
  }

  // Mini-Verlaufskurve (SVG-Polyline), fixedMax z. B. 100 für CPU-Prozent
  function sparkline(values, cls = "", w = 120, h = 26, fixedMax = null) {
    const svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
    svg.setAttribute("width", String(w));
    svg.setAttribute("height", String(h));
    svg.setAttribute("preserveAspectRatio", "none");
    if (cls) svg.classList.add(...cls.split(/\s+/));
    const pts = Array.isArray(values) ? values : [];
    if (pts.length >= 2) {
      const max = fixedMax || Math.max(...pts, 1e-6);
      const stepX = w / (pts.length - 1);
      let d = "";
      pts.forEach((v, i) => {
        const x = i * stepX;
        const y = h - 2 - (Math.max(0, v) / max) * (h - 4);
        d += `${i === 0 ? "M" : "L"}${x.toFixed(1)},${y.toFixed(1)}`;
      });
      const area = document.createElementNS(SVG_NS, "path");
      area.setAttribute("d", `${d}L${w},${h}L0,${h}Z`);
      area.classList.add("spark-area");
      const line = document.createElementNS(SVG_NS, "path");
      line.setAttribute("d", d);
      line.classList.add("spark-line");
      svg.append(area, line);
    } else {
      const line = document.createElementNS(SVG_NS, "line");
      line.setAttribute("x1", "0");
      line.setAttribute("x2", String(w));
      line.setAttribute("y1", String(h - 1));
      line.setAttribute("y2", String(h - 1));
      line.classList.add("spark-line");
      svg.appendChild(line);
    }
    return svg;
  }

  function setSpark(sel, values, cls = "", fixedMax = null) {
    const box = $(sel);
    if (!box) return;
    box.textContent = "";
    box.appendChild(sparkline(values, cls, 120, box.classList.contains("sm") ? 16 : 26, fixedMax));
  }

  function pushOverviewHistory() {
    const r = state.ovStatus?.resources || {};
    const push = (arr, v) => {
      arr.push(typeof v === "number" && isFinite(v) ? v : 0);
      if (arr.length > 40) arr.shift();
    };
    push(state.ovHistory.cpu, r.found ? r.cpu_percent : 0);
    push(state.ovHistory.ram, r.found ? r.ram_mb : 0);
    const seen = new Set();
    for (const c of r.containers || []) {
      seen.add(c.name);
      const ch = (state.ovHistory.cont[c.name] ||= { cpu: [], ram: [] });
      push(ch.cpu, c.cpu_percent);
      push(ch.ram, c.ram_mb);
    }
    for (const gone of Object.keys(state.ovHistory.cont)) {
      if (!seen.has(gone)) delete state.ovHistory.cont[gone];
    }
  }

  function updateUpdatedHint() {
    const el = $("#ov-updated");
    if (!el || !state.ovUpdatedAt) return;
    const s = Math.floor((Date.now() - state.ovUpdatedAt) / 1000);
    setText(el, s < 3 ? "gerade aktualisiert" : `aktualisiert vor ${s} s`);
  }

  function pingLine(inst) {
    if (!inst.container?.running) return null;
    const ping = liveEntry(inst)?.ping;
    if (!ping) return "Status-Abfrage läuft…";
    if (!ping.online) return "Server antwortet noch nicht (Start kann dauern)";
    const p = ping.players || {};
    const motd = (ping.motd || "").replace(/\s+/g, " ").trim();
    const parts = [`${p.online}/${p.max} Spieler online`];
    if (ping.version) parts.push(`Version ${ping.version}`);
    if (motd) parts.push(motd);
    return parts.join(" · ");
  }

  function renderOverviewStats(list) {
    const counts = { running: 0, stopped: 0, starting: 0, error: 0 };
    for (const i of list) counts[instStateKey(i)]++;

    if (state.ovInstances === null) {
      setText($("#ov-inst-total"), "–");
      setText($("#ov-inst-sub"), "Instanzen nicht abrufbar");
    } else {
      setText($("#ov-inst-total"), String(list.length));
      setText($("#ov-inst-sub"), list.length
        ? `${counts.running} laufen`
          + (counts.starting ? ` · ${counts.starting} startet` : "")
          + ` · ${counts.stopped} gestoppt`
          + (counts.error ? ` · ${counts.error} Fehler` : "")
        : "noch keine angelegt");
    }

    // Verteilungsbalken in der Instanzen-Kachel
    const dist = $("#ov-inst-dist");
    if (list.length) {
      show(dist, true);
      const total = list.length;
      for (const [cls, n] of [
        ["running", counts.running], ["starting", counts.starting],
        ["stopped", counts.stopped], ["error", counts.error],
      ]) {
        const fill = dist.querySelector(`.dist-fill.${cls}`);
        fill.style.width = `${(n / total) * 100}%`;
        fill.style.display = n ? "" : "none";
      }
    } else {
      show(dist, false);
    }

    let players = 0, playersMax = 0, reachable = 0;
    const names = [];
    for (const i of list) {
      if (!i.container?.running) continue;
      const ping = liveEntry(i)?.ping;
      if (ping?.online) {
        players += ping.players?.online || 0;
        playersMax += ping.players?.max || 0;
        reachable++;
        for (const p of ping.players?.sample || []) {
          if (p.name) names.push(p.name);
        }
      }
    }
    const runningCount = counts.running;
    setText($("#ov-players"), String(players));
    setText($("#ov-players-sub"),
      runningCount === 0 ? "kein Server läuft"
        : `${reachable} von ${runningCount} Server(n) erreichbar`
          + (playersMax ? ` · Slots ${playersMax}` : ""));
    // Spielernamen (bis 6 in der Kachel, alle im Tooltip)
    const namesEl = $("#ov-players-names");
    if (names.length) {
      show(namesEl, true);
      setText(namesEl, names.slice(0, 6).join(", ")
        + (names.length > 6 ? ` +${names.length - 6}` : ""));
      namesEl.title = names.join(", ");
    } else {
      show(namesEl, false);
    }

    const r = state.ovStatus?.resources || {};
    if (r.found) {
      setText($("#ov-cpu"), `${r.cpu_percent} %`);
      setText($("#ov-cpu-sub"), `${r.processes} Container`);
      setText($("#ov-ram"), fmtMb(r.ram_mb));
      setText($("#ov-ram-sub"), r.ram_limit_mb
        ? `von ${fmtMb(r.ram_limit_mb)} Limit` : "kein Limit erkannt");
      show($("#ov-cpu-spark"), true);
      show($("#ov-ram-spark"), true);
      setSpark("#ov-cpu-spark", state.ovHistory.cpu, "", 100);
      setSpark("#ov-ram-spark", state.ovHistory.ram, "ram");
    } else {
      setText($("#ov-cpu"), "–");
      setText($("#ov-ram"), "–");
      setText($("#ov-cpu-sub"), r.reason ? "Docker nicht abrufbar" : "–");
      setText($("#ov-ram-sub"), r.reason ? "" : "–");
      show($("#ov-cpu-spark"), false);
      show($("#ov-ram-spark"), false);
    }
  }

  function sortOvInstances(list) {
    const stateRank = { running: 0, starting: 1, error: 2, stopped: 3 };
    const stats = containerStatsById();
    const stat = (i) => stats[`mc-inst-${i.id}`] || {};
    const players = (i) => liveEntry(i)?.ping?.players?.online || 0;
    const byName = (a, b) => a.name.localeCompare(b.name, "de");
    switch (state.ovSort) {
      case "status":
        return list.sort((a, b) => stateRank[instStateKey(a)] - stateRank[instStateKey(b)] || byName(a, b));
      case "players":
        return list.sort((a, b) => players(b) - players(a) || byName(a, b));
      case "cpu":
        return list.sort((a, b) => (stat(b).cpu_percent || 0) - (stat(a).cpu_percent || 0) || byName(a, b));
      case "ram":
        return list.sort((a, b) => (stat(b).ram_mb || 0) - (stat(a).ram_mb || 0) || byName(a, b));
      case "created":
        return list.sort((a, b) => (b.created_at || 0) - (a.created_at || 0) || byName(a, b));
      default:
        return list.sort(byName);
    }
  }

  function renderOverviewCards() {
    const list = (state.ovInstances?.instances || []).slice();
    const grid = $("#ov-grid");
    grid.textContent = "";

    // Filter-Buttons mit Trefferzahlen + aktiven Zustand
    const counts = { running: 0, stopped: 0, starting: 0, error: 0 };
    for (const i of list) counts[instStateKey(i)]++;
    const labels = { all: "Alle", running: "Läuft", starting: "Startet", stopped: "Gestoppt", error: "Fehler" };
    document.querySelectorAll("#ov-filter .seg-btn").forEach((btn) => {
      const key = btn.dataset.ovFilter;
      const n = key === "all" ? list.length : counts[key];
      setText(btn, `${labels[key]} (${n})`);
      btn.classList.toggle("active", state.ovFilter === key);
      if (key === "starting") show(btn, counts.starting > 0);
    });
    if (state.ovFilter === "starting" && counts.starting === 0) state.ovFilter = "all";

    // Namensfilter + Statusfilter + Gruppenfilter kombinieren
    const q = state.ovSearch.trim().toLowerCase();
    const visible = sortOvInstances(list.filter((i) => {
      if (q && !i.name.toLowerCase().includes(q)) return false;
      if (state.ovGroup
          && !(i.tags || []).some((t) => String(t).toLowerCase() === state.ovGroup)) {
        return false;
      }
      if (state.ovFilter === "all") return true;
      return instStateKey(i) === state.ovFilter;
    }));

    const emptyBox = $("#ov-empty");
    if (state.ovInstances === null) {
      setText(emptyBox, "Instanzen konnten nicht geladen werden.");
      show(emptyBox, true);
    } else if (list.length === 0) {
      setText(emptyBox, "Noch keine Server-Instanzen vorhanden — lege oben rechts einen an.");
      show(emptyBox, true);
    } else if (visible.length === 0) {
      setText(emptyBox, q
        ? `Keine Server für Suche „${state.ovSearch.trim()}“.`
        : `Keine Server für Filter „${labels[state.ovFilter]}“.`);
      show(emptyBox, true);
    } else {
      show(emptyBox, false);
    }
    setText($("#ov-count"), `(${visible.length}${visible.length !== list.length ? ` von ${list.length}` : ""})`);

    const stats = containerStatsById();
    for (const inst of visible) {
      const node = $("#tpl-ov-card").content.cloneNode(true);
      setText(node.querySelector(".inst-name"), inst.name);
      const badge = node.querySelector(".inst-badge");
      badge.classList.add(stateBadgeClass(inst));
      setText(node.querySelector(".inst-state"), OV_STATE_LABELS[instStateKey(inst)]);

      // Info-Chips: Loader, MC-Version, RAM, Modpack, Port (klickbar = Adresse kopieren), Uptime
      const chips = node.querySelector(".chips");
      const addChip = (text, title) => {
        const c = document.createElement("span");
        c.className = "chip";
        c.textContent = text;
        if (title) c.title = title;
        chips.appendChild(c);
        return c;
      };
      addChip(`${inst.loader}${inst.loader_version ? ` ${inst.loader_version}` : ""}`, "Server-Loader");
      addChip(`MC ${inst.game_version}`, "Minecraft-Version");
      addChip(`${inst.memory || "2G"} RAM`, "Zugewiesener RAM");
      if (inst.modpack?.title) addChip(inst.modpack.title, "Installiertes Modpack");
      for (const tag of inst.tags || []) addChip(`#${tag}`, "Gruppe/Tag").classList.add("tag");
      const portChip = addChip(`Port ${inst.port}`, `Server-Adresse kopieren: ${hostFor(inst)}`);
      portChip.classList.add("chip-btn");
      portChip.addEventListener("click", () => copyAddress(inst));
      if (inst.container?.running && inst.container.started_at) {
        const up = fmtUptime(inst.container.started_at);
        if (up) addChip(`läuft seit ${up}`, "Uptime seit dem letzten Start");
      }
      const cstate = inst.container?.state;
      if (cstate && cstate !== "running" && cstate !== "exited") {
        addChip(`Docker: ${cstate}`, "Docker-Container-Status");
      }

      // Live-Info: Spieler/MOTD (aus /api/instances/live)
      const ping = pingLine(inst);
      const pingBox = node.querySelector(".ov-ping");
      if (ping) {
        setText(pingBox, ping);
        show(pingBox, true);
        pingBox.classList.toggle("has-players", /Spieler online/.test(ping));
      }

      // Spielernamen als Chips (aus dem SLP-Sample)
      const livePing = inst.container?.running ? liveEntry(inst)?.ping : null;
      const sample = livePing?.online ? (livePing.players?.sample || []).map((p) => p.name).filter(Boolean) : [];
      const playersBox = node.querySelector(".ov-players");
      if (sample.length) {
        for (const name of sample.slice(0, 8)) {
          const chip = document.createElement("span");
          chip.className = "player-chip";
          chip.textContent = name;
          playersBox.appendChild(chip);
        }
        if (sample.length > 8) {
          const more = document.createElement("span");
          more.className = "player-chip more";
          more.textContent = `+${sample.length - 8}`;
          more.title = sample.join(", ");
          playersBox.appendChild(more);
        }
        show(playersBox, true);
      } else {
        show(playersBox, false);
      }

      // Docker-Statistiken je Container (mc-inst-{id})
      const res = stats[`mc-inst-${inst.id}`];
      if (res && inst.container?.running) {
        const box = node.querySelector(".ov-res");
        show(box, true);
        setText(node.querySelector(".ov-cpu-val"), `${res.cpu_percent} %`);
        node.querySelector(".ov-cpu-bar").style.width =
          `${Math.max(2, Math.min(100, res.cpu_percent))}%`;
        setText(node.querySelector(".ov-ram-val"), res.ram_limit_mb
          ? `${fmtMb(res.ram_mb)} / ${fmtMb(res.ram_limit_mb)}` : fmtMb(res.ram_mb));
        node.querySelector(".ov-ram-bar").style.width = res.ram_limit_mb
          ? `${Math.max(2, Math.min(100, (res.ram_mb / res.ram_limit_mb) * 100))}%`
          : `${Math.max(2, Math.min(100, res.ram_mb))}%`;
      }

      const errBox = node.querySelector(".inst-error");
      if (inst.status === "error" && inst.error) {
        setText(errBox, inst.error);
        show(errBox, true);
      }

      const stateKey = instStateKey(inst);
      const running = stateKey === "running";
      node.querySelector(".start").disabled = running || stateKey === "starting";
      node.querySelector(".stop").disabled = !running;
      node.querySelector(".restart").disabled = !running;
      node.querySelector(".start").addEventListener("click", () => instAction(inst, "start"));
      node.querySelector(".stop").addEventListener("click", () => instAction(inst, "stop"));
      node.querySelector(".restart").addEventListener("click", () => instAction(inst, "restart"));
      node.querySelector(".logs").addEventListener("click", () => {
        activateTab("servers");
        openDetail(inst.id).then(() =>
          $("#detail-logs").scrollIntoView({ behavior: "smooth", block: "nearest" }));
      });
      node.querySelector(".open").addEventListener("click", () => {
        activateTab("servers");
        openDetail(inst.id);
      });
      grid.appendChild(node);
    }
  }

  function renderResources() {
    const r = state.ovStatus?.resources || {};
    show($("#ov-res-none"), !r.found);
    $("#res-note").hidden = r.found;
    show($("#res-cpu-spark"), !!r.found);
    show($("#res-ram-spark"), !!r.found);
    // Hinweis in BEIDEN Zweigen setzen (sonst bleibt ein alter Text stehen,
    // z. B. „Docker: Kein laufender …“ obwohl längst Container laufen)
    setText($("#res-refresh-hint"), r.reason ? `Docker: ${r.reason}` : "Abfrage alle 5 s");
    if (r.found) {
      setText($("#cpu-val"), `${r.cpu_percent} %`);
      $("#cpu-bar").style.width = `${Math.max(2, Math.min(100, r.cpu_percent))}%`;
      setText($("#ram-val"), r.ram_limit_mb
        ? `${fmtMb(r.ram_mb)} / ${fmtMb(r.ram_limit_mb)}` : fmtMb(r.ram_mb));
      $("#ram-bar").style.width = r.ram_limit_mb
        ? `${Math.max(2, Math.min(100, (r.ram_mb / r.ram_limit_mb) * 100))}%`
        : `${Math.max(2, Math.min(100, r.ram_mb))}%`;
      setSpark("#res-cpu-spark", state.ovHistory.cpu, "", 100);
      setSpark("#res-ram-spark", state.ovHistory.ram, "ram");
    } else {
      setText($("#cpu-val"), "–");
      setText($("#ram-val"), "–");
      $("#cpu-bar").style.width = "0%";
      $("#ram-bar").style.width = "0%";
    }

    // Aufschlüsselung je Container (mit Status, Uptime und Verlaufskurve);
    // flexibel gestapelt, damit nichts aus der schmalen Seitenkarte läuft
    const wrap = $("#res-containers");
    wrap.textContent = "";
    const byName = {};
    for (const i of state.ovInstances?.instances || []) byName[`mc-inst-${i.id}`] = i;
    for (const c of r.containers || []) {
      const inst = byName[c.name];
      const row = document.createElement("div");
      row.className = "res-c-row";

      const head = document.createElement("div");
      head.className = "res-c-head";
      const name = document.createElement("span");
      name.className = "res-c-name";
      name.textContent = inst?.name || c.name;
      if (!inst) name.title = c.name;
      const meta = document.createElement("span");
      meta.className = "res-c-meta muted small";
      const metaParts = [];
      if (inst) {
        metaParts.push(OV_STATE_LABELS[instStateKey(inst)]);
        if (inst.container?.running && inst.container.started_at) {
          const up = fmtUptime(inst.container.started_at);
          if (up) metaParts.push(`seit ${up}`);
        }
      }
      meta.textContent = metaParts.join(" · ");
      head.append(name, meta);
      row.appendChild(head);

      const metrics = document.createElement("div");
      metrics.className = "res-c-metrics";
      for (const kind of ["cpu", "ram"]) {
        const cell = document.createElement("div");
        cell.className = `res-c-metric ${kind}`;
        const val = document.createElement("span");
        val.className = "res-c-val";
        const bar = document.createElement("div");
        bar.className = "bar mini";
        const fill = document.createElement("div");
        fill.className = `bar-fill${kind === "ram" ? " ram" : ""}`;
        bar.appendChild(fill);
        if (kind === "cpu") {
          setText(val, `${c.cpu_percent} %`);
          fill.style.width = `${Math.max(2, Math.min(100, c.cpu_percent))}%`;
        } else {
          setText(val, c.ram_limit_mb
            ? `${fmtMb(c.ram_mb)} / ${fmtMb(c.ram_limit_mb)}` : fmtMb(c.ram_mb));
          fill.style.width = c.ram_limit_mb
            ? `${Math.max(2, Math.min(100, (c.ram_mb / c.ram_limit_mb) * 100))}%`
            : `${Math.max(2, Math.min(100, c.ram_mb))}%`;
        }
        cell.append(val, bar);
        const hist = state.ovHistory.cont[c.name];
        if (hist) {
          // Wrapper nötig: SVGs ohne Größen-CSS würden auf 300×150 px aufblasen
          const sp = document.createElement("div");
          sp.className = `spark sm${kind === "ram" ? " ram" : ""}`;
          sp.appendChild(sparkline(
            kind === "cpu" ? hist.cpu : hist.ram, "", 90, 14,
            kind === "cpu" ? 100 : null));
          cell.appendChild(sp);
        }
        metrics.appendChild(cell);
      }
      row.appendChild(metrics);
      wrap.appendChild(row);
    }
  }

  function renderOverview() {
    const list = state.ovInstances?.instances || [];
    renderBadge(list.filter((i) => i.container?.running).length);
    renderGroupControls(list);
    renderOverviewStats(list);
    renderOverviewCards();
    renderResources();
  }

  async function refreshOverview() {
    const [res, inst] = await Promise.allSettled([
      api("/api/status"),
      api("/api/instances"),
    ]);
    state.ovStatus = res.status === "fulfilled" ? res.value : null;
    state.ovInstances = inst.status === "fulfilled" ? inst.value : null;
    state.ovUpdatedAt = Date.now();
    pushOverviewHistory();
    renderOverview();
    updateUpdatedHint();
    if (res.status === "rejected" && res.reason?.status !== 401) {
      setText($("#ov-error"), `Ressourcen nicht abrufbar: ${res.reason.message}`);
      show($("#ov-error"), true);
    } else if (inst.status === "rejected" && inst.reason?.status !== 401) {
      setText($("#ov-error"), `Instanzen nicht abrufbar: ${inst.reason.message}`);
      show($("#ov-error"), true);
    } else {
      show($("#ov-error"), false);
    }
    // Sobald eine Instanz läuft, Spielerdaten nachziehen (auch kurz nach Start)
    if (state.ovInstances?.instances?.some((i) => i.container?.running)
        && Date.now() - state.lastLiveAt > 5000) {
      refreshLive();
    }
  }

  async function refreshLive() {
    const list = state.ovInstances?.instances || [];
    if (!list.some((i) => i.container?.running)) {
      if (Object.keys(state.ovLive).length) {
        state.ovLive = {};
        renderOverview();
      }
      return;
    }
    state.lastLiveAt = Date.now();
    try {
      const data = await api("/api/instances/live");
      state.ovLive = {};
      for (const entry of data.live || []) state.ovLive[entry.id] = entry;
      renderOverview();
    } catch (e) {
      /* alte Werte behalten; nächster Poll versucht es erneut */
    }
  }

  function startPolling() {
    if (state.statusTimer) return;
    if (!state.pollingAllowed) return; // erst nach erfolgreichem Auth-Check
    refreshOverview();
    state.statusTimer = setInterval(refreshOverview, 5000);
    state.liveTimer = setInterval(refreshLive, 15000);
    state.tickTimer = setInterval(updateUpdatedHint, 1000);
  }
  function stopPolling() {
    clearInterval(state.statusTimer);
    clearInterval(state.liveTimer);
    clearInterval(state.tickTimer);
    state.statusTimer = null;
    state.liveTimer = null;
    state.tickTimer = null;
  }
  document.addEventListener("visibilitychange", () =>
    document.hidden ? stopPolling() : startPolling());
  // Erst Auth prüfen (Login-Overlay?), dann Polling starten — sonst laufen
  // die 5-s-Polls gegen 401, während der Login noch offen ist.
  initAuth().then((authorized) => {
    state.pollingAllowed = authorized;
    if (authorized) startPolling();
  });

  document.querySelectorAll("#ov-filter .seg-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.ovFilter = btn.dataset.ovFilter;
      renderOverviewCards();
    });
  });
  $("#ov-new").addEventListener("click", () => {
    activateTab("servers");
    openCreate();
  });

  // Suche, Sortierung, manuelle Aktualisierung, Sammel-Aktionen
  let ovSearchTimer = null;
  $("#ov-search").addEventListener("input", (e) => {
    clearTimeout(ovSearchTimer);
    ovSearchTimer = setTimeout(() => {
      state.ovSearch = e.target.value;
      renderOverviewCards();
    }, 200);
  });
  $("#ov-sort").addEventListener("change", (e) => {
    state.ovSort = e.target.value;
    renderOverviewCards();
  });
  $("#ov-reload").addEventListener("click", () => {
    refreshOverview();
    refreshLive();
  });

  async function bulkInstanceAction(action, tagFilter = null) {
    const list = state.ovInstances?.instances || [];
    const wanted = tagFilter ? String(tagFilter).toLowerCase() : null;
    const targets = list.filter((i) => {
      if (wanted && !(i.tags || []).some((t) => String(t).toLowerCase() === wanted)) {
        return false;
      }
      return action === "start"
        ? ["stopped", "error"].includes(instStateKey(i))
        : ["running", "starting"].includes(instStateKey(i));
    });
    if (!targets.length) {
      toast(wanted
        ? `Keine passenden Server in der Gruppe "${tagFilter}".`
        : action === "start"
          ? "Keine gestoppten Server vorhanden."
          : "Keine laufenden Server vorhanden.", "info");
      return;
    }
    const label = wanted ? ` der Gruppe "${tagFilter}"` : "";
    if (targets.length > 2 && !window.confirm(
      `${targets.length} Server${label} ${action === "start" ? "starten" : "stoppen"}?`)) return;
    const results = await Promise.allSettled(targets.map((i) =>
      api(`/api/instances/${i.id}/${action}`, { method: "POST" })));
    const ok = results.filter((r) => r.status === "fulfilled").length;
    const failed = results.length - ok;
    toast(`${ok} Server${label} ${action === "start" ? "gestartet" : "gestoppt"}` +
      (failed ? `, ${failed} fehlgeschlagen` : "") + ".",
      failed ? "error" : "success");
    refreshOverview();
    setTimeout(refreshLive, 3000);
  }
  $("#ov-start-all").addEventListener("click", () => bulkInstanceAction("start"));
  $("#ov-stop-all").addEventListener("click", () => bulkInstanceAction("stop"));

  /* ---------- Gruppen (Tags): Filter + Sammelaktionen ---------- */
  state.ovGroup = ""; // ausgewählter Tag

  function allTagOptions(list) {
    const tags = new Map(); // lowercase → angezeigte Schreibweise
    for (const i of list) for (const t of i.tags || []) {
      if (!tags.has(String(t).toLowerCase())) tags.set(String(t).toLowerCase(), String(t));
    }
    return [...tags.entries()].sort((a, b) => a[1].localeCompare(b[1], "de"));
  }

  function renderGroupControls(list) {
    const sel = $("#ov-group");
    const options = allTagOptions(list);
    // Auswahl halten, wenn der Tag noch existiert; sonst zurücksetzen
    if (state.ovGroup && !options.some(([key]) => key === state.ovGroup)) {
      state.ovGroup = "";
    }
    sel.textContent = "";
    const empty = document.createElement("option");
    empty.value = "";
    empty.textContent = `– (${options.length})`;
    sel.appendChild(empty);
    for (const [key, label] of options) {
      const opt = document.createElement("option");
      opt.value = key;
      opt.textContent = label;
      sel.appendChild(opt);
    }
    sel.value = state.ovGroup;
    $("#ov-group-start").disabled = !state.ovGroup;
    $("#ov-group-stop").disabled = !state.ovGroup;
  }

  $("#ov-group").addEventListener("change", (e) => {
    state.ovGroup = e.target.value;
    $("#ov-group-start").disabled = !state.ovGroup;
    $("#ov-group-stop").disabled = !state.ovGroup;
    renderOverviewCards();
  });
  $("#ov-group-start").addEventListener("click", () => {
    if (state.ovGroup) bulkInstanceAction("start", state.ovGroup);
  });
  $("#ov-group-stop").addEventListener("click", () => {
    if (state.ovGroup) bulkInstanceAction("stop", state.ovGroup);
  });

  /* ---------- Installierte Mods (Hauptserver, Legacy-API ohne UI) ---------- */
  // Der Hauptserver wurde entfernt; die /api/mods-Routen bleiben für
  // Bestandsinstallationen bestehen, das Frontend nutzt nur noch Instanzen.

  /* ---------- Installationsziel wählen (nur Instanzen) ---------- */
  // Metadaten der Ziele: Instanz-ID → {loader, game_version}
  state.searchTargets = {};

  async function loadInstanceTargets() {
    const sels = [$("#search-target-select"), $("#mp-target-select")];
    try {
      const data = await api("/api/instances");
      state.searchTargets = {};
      const options = data.instances.map((i) => {
        state.searchTargets[i.id] = { loader: i.loader, game_version: i.game_version };
        return { value: i.id, label: `${i.name} (${i.loader} ${i.game_version})` };
      });
      for (const sel of sels) {
        if (!options.length) {
          fillSelect(sel, [], "Keine Instanz — zuerst im Tab „Server“ erstellen");
        } else {
          fillSelect(sel, options);
          const current = sel.value;
          sel.value = (current && options.some((o) => o.value === current))
            ? current : options[0].value;
        }
      }
    } catch (e) {
      for (const sel of sels) fillSelect(sel, [], "Instanzen nicht abrufbar");
    }
    updateSearchHint();
    updateMpHint();
  }

  function targetFromSelect(sel) {
    const opt = sel.selectedOptions[0];
    const id = opt?.value || "";
    if (!id || !state.searchTargets[id]) return null;
    return {
      instanceId: id,
      label: (opt.textContent || "").split(" (")[0] || "Instanz",
      isInstance: true,
      ...state.searchTargets[id],
    };
  }

  function searchTarget() {
    return targetFromSelect($("#search-target-select"));
  }

  function mpTarget() {
    return targetFromSelect($("#mp-target-select"));
  }

  function updateSearchHint() {
    const target = searchTarget();
    setText($("#search-target"), target
      ? `Installationsziel: ${target.label} · Mods landen im mods-Ordner dieser Instanz`
      : "Keine Ziel-Instanz ausgewählt — Suche deaktiviert.");
  }

  function updateMpHint() {
    const target = mpTarget();
    setText($("#mp-target"), target
      ? `Ziel: ${target.label} · Modpack wird in dieser Instanz installiert`
      : "Keine Ziel-Instanz gewählt — Treffer können direkt als neuer Server erstellt werden (Button „Neuer Server“).");
  }

  $("#search-target-select").addEventListener("change", () => {
    updateSearchHint();
    doSearch(0); // Suche passend zum neuen Ziel neu ausführen
  });

  $("#mp-target-select").addEventListener("change", () => {
    updateMpHint();
    mpSearch(0);
  });

  $("#search-source").addEventListener("change", (e) => {
    state.searchSource = e.target.value;
    updateEnvironmentFilter(); // Server/Client-Filter nur bei Modrinth
    doSearch(0);
  });

  $("#pack-source").addEventListener("change", (e) => {
    state.packSource = e.target.value;
    searchPacks(0);
  });

  $("#mp-source").addEventListener("change", (e) => {
    state.mpSource = e.target.value;
    mpSearch(0);
  });

  /* ---------- Mod-/Pack-Suche (Modrinth & CurseForge) ---------- */
  state.searchFilterSort = "relevance";
  state.searchFilterLoader = "";     // "" = wie Instanz, "any" = alle Loader
  state.searchFilterVersion = "";    // "" = wie Instanz, "any" = alle Versionen
  state.searchFilterEnvironment = ""; // "" = alle Mods (nur Modrinth)
  state.searchFiltersLoaded = false;

  for (const [sel, key] of [
    ["#search-filter-sort", "searchFilterSort"],
    ["#search-filter-loader", "searchFilterLoader"],
    ["#search-filter-version", "searchFilterVersion"],
    ["#search-filter-environment", "searchFilterEnvironment"],
  ]) {
    $(sel).addEventListener("change", (e) => {
      state[key] = e.target.value;
      doSearch(0);
    });
  }

  // Server/Client-Filter gibt es nur bei Modrinth (CurseForge liefert dazu
  // keine Suchdaten) — daher bei CF deaktivieren.
  function updateEnvironmentFilter() {
    const disabled = state.searchSource === "curseforge";
    const sel = $("#search-filter-environment");
    sel.disabled = disabled;
    if (disabled) {
      sel.value = "";
      state.searchFilterEnvironment = "";
    }
  }
  updateEnvironmentFilter();

  async function loadSearchFilters() {
    if (state.searchFiltersLoaded) return;
    state.searchFiltersLoaded = true;
    try {
      const options = await fetchMcVersionOptions();
      fillSelect($("#search-filter-version"), options, "Wie Instanz");
      $("#search-filter-version").value = state.searchFilterVersion;
    } catch (e) {
      fillSelect($("#search-filter-version"), [], "Wie Instanz (Katalog nicht erreichbar)");
    }
  }

  $("#search-input").addEventListener("input", () => {
    clearTimeout(state.searchTimer);
    state.searchTimer = setTimeout(() => doSearch(0), 400); // Debounce
  });
  $("#search-prev").addEventListener("click", () => doSearch(Math.max(0, state.offset - 20)));
  $("#search-next").addEventListener("click", () => doSearch(state.offset + 20));

  // CurseForge ohne API-Key: Backend antwortet mit 503 („CF_API_KEY" im Detail).
  // Statt der generischen Meldung einen Alternativ-Hinweis zeigen (keylos möglich:
  // Modrinth-Quelle oder CF-Pack als .zip hochladen).
  function cfNoKeyHint(e) {
    if (e.status !== 503 && !(e.message || "").includes("CF_API_KEY")) return null;
    return "CurseForge-Suche benötigt einen API-Key (wird nicht mehr vergeben). " +
      "Alternativen ohne Key: Quelle „Modrinth“ wählen oder ein CF-Pack als .zip " +
      "hochladen (Upload-Box im Modpacks-Tab) — beides funktioniert ohne Key.";
  }

  async function doSearch(offset) {
    state.offset = offset;
    const q = $("#search-input").value.trim();
    const target = searchTarget();
    if (!target) {
      setText($("#search-error"),
        "Keine Ziel-Instanz vorhanden — bitte zuerst im Tab „Server“ eine Instanz erstellen.");
      show($("#search-error"), true);
      $("#search-results").textContent = "";
      setText($("#search-page"), "");
      $("#search-prev").disabled = true;
      $("#search-next").disabled = true;
      return;
    }
    show($("#search-loading"), true);
    show($("#search-error"), false);
    try {
      const params = new URLSearchParams({ q, offset: String(offset), sort: state.searchFilterSort });
      // Filter: Standard = Loader/MC-Version des Installationsziels,
      // überschreibbar („Alle …“ = filterfrei), damit man besser findet.
      const loader = state.searchFilterLoader === "any" ? "any"
        : (state.searchFilterLoader || target.loader);
      const gameVersion = state.searchFilterVersion === "any" ? "any"
        : (state.searchFilterVersion || target.game_version);
      params.set("loader", loader);
      params.set("game_version", gameVersion);
      // Ziel-Instanz mitsenden → Treffer werden mit "installiert" markiert
      params.set("instance_id", target.instanceId);
      // Server/Client-Umgebung nur bei Modrinth senden (CurseForge ohne Daten)
      if (state.searchFilterEnvironment && state.searchSource !== "curseforge") {
        params.set("environment", state.searchFilterEnvironment);
      }
      const cf = state.searchSource === "curseforge";
      const path = cf ? "/api/curseforge/search" : "/api/modrinth/search";
      const data = await api(`${path}?${params}`);
      state.total = data.total;
      renderResults(data);
    } catch (e) {
      const hint = state.searchSource === "curseforge" ? cfNoKeyHint(e) : null;
      setText($("#search-error"), hint || `Suche fehlgeschlagen: ${e.message}`);
      show($("#search-error"), true);
      $("#search-results").textContent = "";
    } finally {
      show($("#search-loading"), false);
    }
  }

  function renderResults(data) {
    const container = $("#search-results");
    container.textContent = "";
    const tpl = $("#tpl-result");
    for (const hit of data.hits) {
      hit._source = data.source || "modrinth"; // Quelle pro Treffer merken
      const node = tpl.content.cloneNode(true);
      const icon = node.querySelector(".result-icon");
      if (hit.icon_url && hit.icon_url.startsWith("https://")) {
        const img = document.createElement("img");
        img.src = hit.icon_url;
        img.alt = "";
        img.loading = "lazy";
        icon.appendChild(img);
      } else {
        icon.textContent = (hit.title || "?").charAt(0).toUpperCase();
      }
      setText(node.querySelector(".name"), hit.title || hit.slug);
      setText(node.querySelector(".author"), hit.author ? "von " + hit.author : "");
      setText(node.querySelector(".desc"), hit.description || "");
      setText(node.querySelector(".version"),
        hit.latest_version ? `Version ${hit.latest_version}` : "Version unbekannt");
      setText(node.querySelector(".downloads"), `${fmtNumber(hit.downloads)} Downloads`);
      const sideMap = { required: "Server: nötig", optional: "Server: optional" };
      const side = sideMap[hit.server_side]
        ?? (hit.server_side === null ? "Server: unbekannt" : "Server: client-seitig");
      setText(node.querySelector(".side"), side);
      // „Installiert“-Markierung: Mod liegt bereits in der Ziel-Instanz
      // (per Datei-Hash erkannt, auch bei abweichendem Dateinamen) —
      // Karte grün hervorheben + Tag prominent neben dem Titel
      if (hit.installed) {
        node.querySelector(".result").classList.add("is-installed");
        const tag = document.createElement("span");
        tag.className = "installed-tag";
        tag.textContent = "Installiert";
        node.querySelector(".result-title").appendChild(tag);
      }
      const btn = node.querySelector(".install");
      const progressBox = node.querySelector(".dl-progress");
      const bar = node.querySelector(".dl-bar");
      if (hit.installed) {
        btn.disabled = true;
        btn.textContent = "Installiert ✓";
        btn.classList.add("installed");
        btn.title = "Bereits in der Ziel-Instanz installiert";
      }
      btn.addEventListener("click", () => installMod(hit, btn, progressBox, bar));
      container.appendChild(node);
    }
    setText($("#search-page"), `Seite ${Math.floor(state.offset / 20) + 1} · ${fmtNumber(state.total)} Treffer`);
    $("#search-prev").disabled = state.offset <= 0;
    $("#search-next").disabled = state.offset + 20 >= state.total;
  }

  /* ---------- One-Click-Download mit Fortschritt ---------- */
  async function installMod(hit, btn, progressBox, bar) {
    btn.disabled = true;
    btn.textContent = "Starte…";
    const target = searchTarget();
    if (!target) {
      toast("Keine Ziel-Instanz ausgewählt.", "error");
      btn.disabled = false;
      btn.textContent = "Installieren";
      return;
    }
    // Quelle des Treffers nutzen, nicht den aktuell gewählten Suchfilter
    // (verhindert Verwechslungen nach einem Quellenwechsel bei noch
    // sichtbaren Treffern der vorherigen Quelle)
    const endpoint = (hit._source || state.searchSource) === "curseforge"
      ? "/api/curseforge/download" : "/api/modrinth/download";
    const start = (overwrite) => api(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        project_id: hit.project_id,
        overwrite,
        instance_id: target.instanceId,
      }),
    });
    try {
      let job;
      try {
        job = await start(false);
      } catch (e) {
        if (e.status !== 409) throw e;
        const name = e.filename || hit.title || "Mod";
        if (!window.confirm(`"${name}" existiert bereits am gewählten Ziel.\nÜberschreiben?`)) {
          btn.disabled = false;
          btn.textContent = "Installieren";
          return;
        }
        job = await start(true);
      }
      show(progressBox, true);
      // Abhängigkeiten vorab anzeigen (welche Dateien mit geladen werden)
      const depsBox = progressBox.querySelector(".dl-deps");
      const depNames = (Array.isArray(job.dependencies) ? job.dependencies : [])
        .map((d) => d.filename).filter(Boolean);
      if (depNames.length) {
        const shown = depNames.slice(0, 3).join(", ");
        setText(depsBox, `Mit installiert: ${shown}` +
          (depNames.length > 3 ? ` (+${depNames.length - 3})` : ""));
      } else {
        setText(depsBox, "");
      }
      const finalJob = await pollJob(job, bar, btn);
      hit.installed = true; // lokal markieren (kein erneutes Installieren möglich)
      const card = btn.closest(".result");
      if (card) card.classList.add("is-installed");
      btn.disabled = true;
      btn.classList.add("installed");
      btn.textContent = "Installiert ✓";
      const deps = Array.isArray(finalJob.dependencies) ? finalJob.dependencies.length
        : Array.isArray(job.dependencies) ? job.dependencies.length : 0;
      toast(`"${hit.title}" installiert${deps ? ` (+${deps} Abhängigkeit${deps === 1 ? "" : "en"})` : ""} → ${target.label}.`,
        "success");
      if (Array.isArray(finalJob.dep_errors) && finalJob.dep_errors.length) {
        toast(`Abhängigkeiten fehlgeschlagen: ${finalJob.dep_errors.join(", ")}`, "error");
      }
    } catch (e) {
      toast(`Download fehlgeschlagen: ${e.message}`, "error");
      btn.disabled = false;
      btn.textContent = "Installieren";
    } finally {
      show(progressBox, false);
    }
  }

  function pollJob(job, bar, btn) {
    // resolve(finaler Job-Datenstand) — u. a. für dep_errors des Bundles
    return new Promise((resolve, reject) => {
      const timer = setInterval(async () => {
        try {
          const data = await api(`/api/modrinth/jobs/${job.job_id}`);
          if (data.total > 0) {
            const pct = Math.min(100, Math.round((data.downloaded / data.total) * 100));
            bar.style.width = pct + "%";
            btn.textContent = pct + " %";
          } else {
            btn.textContent = fmtBytes(data.downloaded);
          }
          if (data.status === "done") {
            clearInterval(timer);
            resolve(data);
          } else if (data.status === "error") {
            clearInterval(timer);
            reject(new Error(data.error || "Download fehlgeschlagen"));
          }
        } catch (e) {
          clearInterval(timer);
          reject(e);
        }
      }, 700);
    });
  }

  /* =====================================================================
     Multi-Server-Verwaltung (Instanzen, Katalog, Modpacks)
     ===================================================================== */
  state.detailId = null;
  state.createLoaded = false;
  state.packOffset = 0;
  state.packTotal = 0;
  state.packTimer = null;
  state.detailLogTimer = null;
  state.detailMods = [];     // letzte Mod-Liste der Detail-Ansicht (ungefiltert)
  state.modsCollapsed = false; // eingeklappte Mod-Liste
  state.cfgOpen = false;     // server.properties-Editor ausgeklappt?
  state.cfgProps = [];       // geparste server.properties (ungefiltert)
  state.cfgSchema = [];      // Schema bekannter Eigenschaften (vom Backend)
  state.cfgMode = "form";    // "form" (Formular-Editor) | "raw" (freie Liste)
  state.detailUpdates = null; // Update-Check-Ergebnis: filename → item
  state.wlEntries = [];      // Whitelist-Einträge im Editor
  state.wlRunning = false;   // Instanz läuft (für den RCON-Reload-Button)
  state.consoleHistory = []; // Befehlsverlauf der Konsole (ArrowUp/Down)
  state.consoleHistoryPos = 0;

  /* ---------- Instanz-Liste ---------- */
  async function loadInstances() {
    show($("#inst-loading"), true);
    show($("#inst-error"), false);
    try {
      const data = await api("/api/instances");
      setText($("#inst-dir"), `Speicherort: ${data.directory}`);
      setText($("#inst-count"), `(${data.instances.length})`);
      show($("#inst-empty"), data.instances.length === 0);
      renderInstances(data.instances);
    } catch (e) {
      setText($("#inst-error"), `Instanzen konnten nicht geladen werden: ${e.message}`);
      show($("#inst-error"), true);
    } finally {
      show($("#inst-loading"), false);
    }
  }
  $("#inst-reload").addEventListener("click", loadInstances);

  function stateBadgeClass(inst) {
    if (inst.status === "running" || inst.container?.running) return "on";
    if (inst.status === "starting") return "starting";
    if (inst.status === "error") return "error";
    return "off";
  }

  function renderInstances(list) {
    const grid = $("#inst-grid");
    grid.textContent = "";
    for (const inst of list) {
      const node = $("#tpl-inst-card").content.cloneNode(true);
      const card = node.querySelector(".inst-card");
      card.dataset.id = inst.id;
      card.classList.toggle("selected", inst.id === state.detailId);
      setText(node.querySelector(".inst-name"), inst.name);
      const badge = node.querySelector(".inst-badge");
      badge.classList.add(stateBadgeClass(inst));
      setText(node.querySelector(".inst-state"),
        inst.container?.running ? "läuft" :
        inst.status === "starting" ? "startet…" :
        inst.status === "error" ? "Fehler" : "gestoppt");
      setText(node.querySelector(".inst-meta"),
        `${inst.loader} · MC ${inst.game_version} · Port ${inst.port} · ${inst.memory || "2G"}`);
      const chipBox = node.querySelector(".chips");
      for (const tag of inst.tags || []) {
        const chip = document.createElement("span");
        chip.className = "chip tag";
        chip.textContent = `#${tag}`;
        chip.title = "Gruppe/Tag";
        chipBox.appendChild(chip);
      }
      const errBox = node.querySelector(".inst-error");
      if (inst.status === "error" && inst.error) {
        setText(errBox, inst.error);
        show(errBox, true);
      }
      node.querySelector(".start").addEventListener("click", () => instAction(inst, "start"));
      node.querySelector(".stop").addEventListener("click", () => instAction(inst, "stop"));
      node.querySelector(".restart").addEventListener("click", () => instAction(inst, "restart"));
      node.querySelector(".open").addEventListener("click", () => openDetail(inst.id));
      node.querySelector(".clone").addEventListener("click", () => cloneInstance(inst));
      node.querySelector(".delete").addEventListener("click", () => deleteInstance(inst));
      grid.appendChild(node);
    }
  }

  function updateInstSelection() {
    for (const card of document.querySelectorAll("#inst-grid .inst-card")) {
      card.classList.toggle("selected", card.dataset.id === state.detailId);
    }
  }

  async function instAction(inst, action) {
    const done = action === "start" ? "gestartet"
      : action === "stop" ? "gestoppt" : "neu gestartet";
    const failed = action === "start" ? "Start"
      : action === "stop" ? "Stop" : "Neustart";
    try {
      await api(`/api/instances/${inst.id}/${action}`, { method: "POST" });
      toast(`Server "${inst.name}" ${done}.`, "success");
    } catch (e) {
      toast(`${failed} fehlgeschlagen: ${e.message}`, "error");
    }
    loadInstances();
    refreshOverview(); // Übersicht inkl. Spielerdaten zügig aktualisieren
    setTimeout(refreshLive, 3000);
  }

  async function deleteInstance(inst) {
    const msg = inst.container?.running
      ? `"${inst.name}" läuft noch.\nStoppen und LÖSCHEN (inkl. aller Daten)?`
      : `"${inst.name}" inkl. aller Daten (Mods, Welt) wirklich löschen?`;
    if (!window.confirm(msg)) return;
    try {
      await api(`/api/instances/${inst.id}?force=true`, { method: "DELETE" });
      toast(`Server "${inst.name}" gelöscht.`, "success");
      if (state.detailId === inst.id) closeDetail();
    } catch (e) {
      toast(`Löschen fehlgeschlagen: ${e.message}`, "error");
    }
    loadInstances();
  }

  async function cloneInstance(inst) {
    const name = window.prompt(
      `Namen für den Klon von "${inst.name}" eingeben\n(abbrechen = automatisch "${inst.name} 2"):`,
      `${inst.name} 2`);
    if (name === null) return;
    try {
      const clone = await api(`/api/instances/${inst.id}/clone`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: name.trim() || null }),
      });
      toast(`Klon "${clone.name}" erstellt (Port ${clone.port}).`, "success");
      loadInstances();
    } catch (e) {
      toast(`Klonen fehlgeschlagen: ${e.message}`, "error");
    }
  }

  /* ---------- Erstellen: Katalog laden (Versionen + Loader) ---------- */
  function fillSelect(select, options, placeholder) {
    select.textContent = "";
    if (placeholder !== undefined) {
      const opt = document.createElement("option");
      opt.value = "";
      opt.textContent = placeholder;
      select.appendChild(opt);
    }
    for (const optData of options) {
      const opt = document.createElement("option");
      opt.value = optData.value;
      opt.textContent = optData.label;
      if (optData.disabled) opt.disabled = true;
      select.appendChild(opt);
    }
  }

  async function openCreate() {
    show($("#inst-create"), true);
    if (state.createLoaded) return;
    fillSelect($("#inst-mc-version"), [], "Versionen werden geladen…");
    fillSelect($("#inst-loader"), [], "–");
    try {
      const [versions, loaders] = await Promise.all([
        api("/api/catalog/mc-versions"),
        api("/api/catalog/loaders?game_version=latest").catch(() => null),
      ]);
      const options = versions.releases.map((v) => ({ value: v, label: v }));
      for (const snap of versions.snapshots.slice(0, 25)) {
        options.push({ value: snap, label: `${snap} (Snapshot)` });
      }
      fillSelect($("#inst-mc-version"), options, versions.latest_release || "Version wählen");
      if (versions.latest_release) $("#inst-mc-version").value = versions.latest_release;
      state.createLoaded = true;
      await reloadLoaders($("#inst-mc-version").value);
    } catch (e) {
      fillSelect($("#inst-mc-version"), [], "Katalog nicht erreichbar");
      toast(`Versionskatalog nicht verfügbar: ${e.message}`, "error");
    }
  }

  /* Generische Katalog-Selects: p = ID-Präfix ("inst" = Erstellen-Dialog,
     "imp" = Import-Dialog); erwartet #<p>-mc-version, -loader, -loader-version, -loader-note */
  async function reloadLoadersInto(p, gameVersion) {
    const loaderSel = $(`#${p}-loader`);
    const lvSel = $(`#${p}-loader-version`);
    fillSelect(loaderSel, [], "Loader werden geladen…");
    fillSelect(lvSel, [], "–");
    setText($(`#${p}-loader-note`), "");
    try {
      const data = await api(`/api/catalog/loaders?game_version=${encodeURIComponent(gameVersion)}`);
      const options = data.loaders.map((l) => ({
        value: l.loader,
        label: l.error ? `${l.loader} (Meta nicht erreichbar — Standard wird genutzt)`
          : l.versions.length === 0 ? `${l.loader}${l.note ? " – Hinweis verfügbar" : ""}`
          : `${l.loader} (${l.versions.length} Versionen)`,
      }));
      fillSelect(loaderSel, options, "Loader wählen");
    } catch (e) {
      fillSelect(loaderSel, [], "Katalog nicht erreichbar");
    }
    updateLoaderVersionInto(p);
  }

  function updateLoaderVersionInto(p) {
    const loader = $(`#${p}-loader`).value;
    const gv = $(`#${p}-mc-version`).value;
    const select = $(`#${p}-loader-version`);
    select.textContent = "";
    setText($(`#${p}-loader-note`), "");
    if (!loader || !gv) {
      fillSelect(select, [], "–");
      return;
    }
    api(`/api/catalog/loaders?game_version=${encodeURIComponent(gv)}`)
      .then((data) => {
        const info = data.loaders.find((l) => l.loader === loader);
        if (!info) return fillSelect(select, [], "Standard (automatisch)");
        if (info.note) setText($(`#${p}-loader-note`), info.note);
        const options = info.versions.map((v) => ({
          value: v.version,
          label: v.version === "auto"
            ? "Standard (automatisch)"
            : v.version + (v.stable === false ? " (Beta)" : ""),
        }));
        fillSelect(select, options, "Standard (neueste kompatible)");
        // Nur Beta-Versionen verfügbar? Dann die neueste direkt vorwählen —
        // der itzg-Installer löst "automatisch" nur auf stabile Versionen auf
        // und würde sonst beim Start scheitern (z. B. NeoForge für neue MC-Versionen).
        const allBeta = info.versions.length > 0
          && info.versions.every((v) => v.stable === false && v.version !== "auto");
        if (allBeta && info.versions[0]?.version) {
          select.value = info.versions[0].version;
          setText($(`#${p}-loader-note`),
            (info.note ? info.note + " · " : "") +
            "Nur Beta-Versionen verfügbar — automatisch gewählt: " + info.versions[0].version);
        }
      })
      .catch(() => fillSelect(select, [], "Standard (automatisch)"));
  }

  function reloadLoaders(gameVersion) { return reloadLoadersInto("inst", gameVersion); }
  function updateLoaderVersionSelect() { return updateLoaderVersionInto("inst"); }

  $("#inst-new").addEventListener("click", openCreate);
  $("#inst-cancel").addEventListener("click", () => show($("#inst-create"), false));
  $("#inst-mc-version").addEventListener("change", () => reloadLoaders($("#inst-mc-version").value));
  $("#inst-loader").addEventListener("change", updateLoaderVersionSelect);

  $("#inst-create-btn").addEventListener("click", async () => {
    const msgBox = $("#inst-create-msg");
    show(msgBox, false);
    const body = {
      name: $("#inst-name").value.trim(),
      loader: $("#inst-loader").value,
      game_version: $("#inst-mc-version").value,
      loader_version: $("#inst-loader-version").value || null,
      memory: $("#inst-memory").value || null,
      port: $("#inst-port").value ? parseInt($("#inst-port").value, 10) : null,
      accept_eula: $("#inst-eula").checked,
      tags: parseTagsInput("#inst-tags-create"),
    };
    if (!body.name || !body.loader || !body.game_version) {
      setText(msgBox, "Bitte Name, Minecraft-Version und Loader angeben.");
      show(msgBox, true);
      return;
    }
    const btn = $("#inst-create-btn");
    btn.disabled = true;
    try {
      const inst = await api("/api/instances", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      toast(`Server "${inst.name}" erstellt (Port ${inst.port}).`, "success");
      $("#inst-name").value = "";
      $("#inst-port").value = "";
      $("#inst-tags-create").value = "";
      show($("#inst-create"), false);
      loadInstances();
    } catch (e) {
      setText(msgBox, `Erstellung fehlgeschlagen: ${e.message}`);
      show(msgBox, true);
    } finally {
      btn.disabled = false;
    }
  });

  /* ---------- Bestehenden Server importieren ---------- */
  $("#inst-import").addEventListener("click", async () => {
    show($("#inst-import-box"), true);
    show($("#inst-create"), false);
    show($("#imp-msg"), false);
    if (state.importLoaded) return;
    fillSelect($("#imp-mc-version"), [], "Versionen werden geladen…");
    fillSelect($("#imp-loader"), [], "–");
    fillSelect($("#imp-loader-version"), [], "–");
    try {
      const versions = await api("/api/catalog/mc-versions");
      const options = versions.releases.map((v) => ({ value: v, label: v }));
      for (const snap of versions.snapshots.slice(0, 25)) {
        options.push({ value: snap, label: `${snap} (Snapshot)` });
      }
      fillSelect($("#imp-mc-version"), options, versions.latest_release || "Version wählen");
      if (versions.latest_release) $("#imp-mc-version").value = versions.latest_release;
      state.importLoaded = true;
      await reloadLoadersInto("imp", $("#imp-mc-version").value);
    } catch (e) {
      fillSelect($("#imp-mc-version"), [], "Katalog nicht erreichbar");
      toast(`Versionskatalog nicht verfügbar: ${e.message}`, "error");
    }
  });
  $("#imp-cancel").addEventListener("click", () => show($("#inst-import-box"), false));
  $("#imp-mc-version").addEventListener("change", () => reloadLoadersInto("imp", $("#imp-mc-version").value));
  $("#imp-loader").addEventListener("change", () => updateLoaderVersionInto("imp"));

  $("#imp-create-btn").addEventListener("click", async () => {
    const msgBox = $("#imp-msg");
    show(msgBox, false);
    const file = $("#imp-file").files[0];
    const name = $("#imp-name").value.trim();
    const loader = $("#imp-loader").value;
    const gameVersion = $("#imp-mc-version").value;
    if (!name || !loader || !gameVersion || !file) {
      setText(msgBox, "Bitte Name, Archiv, Minecraft-Version und Loader angeben.");
      show(msgBox, true);
      return;
    }
    if (!$("#imp-eula").checked) {
      setText(msgBox, "Bitte zuerst die Minecraft-EULA akzeptieren.");
      show(msgBox, true);
      return;
    }
    const form = new FormData();
    form.append("name", name);
    form.append("loader", loader);
    form.append("game_version", gameVersion);
    if ($("#imp-loader-version").value) form.append("loader_version", $("#imp-loader-version").value);
    if ($("#imp-port").value) form.append("port", $("#imp-port").value);
    if ($("#imp-memory").value) form.append("memory", $("#imp-memory").value);
    form.append("accept_eula", $("#imp-eula").checked);
    form.append("file", file);
    const btn = $("#imp-create-btn");
    btn.disabled = true;
    btn.textContent = "Importiere…";
    try {
      const inst = await api("/api/instances/import", { method: "POST", body: form });
      toast(`Server "${inst.name}" importiert (Port ${inst.port}).`, "success");
      $("#imp-name").value = "";
      $("#imp-port").value = "";
      $("#imp-file").value = "";
      show($("#inst-import-box"), false);
      loadInstances();
    } catch (e) {
      setText(msgBox, `Import fehlgeschlagen: ${e.message}`);
      show(msgBox, true);
    } finally {
      btn.disabled = false;
      btn.textContent = "Server importieren";
    }
  });

  /* ---------- Live-Logs (SSE, Fallback: Polling) ---------- */
  function closeDetailLogStream() {
    if (state.detailLogStream) {
      state.detailLogStream.abort();
      state.detailLogStream = null;
    }
    if (state.detailLogTimer) {
      clearInterval(state.detailLogTimer);
      state.detailLogTimer = null;
    }
  }

  function appendLogLine(line) {
    const pre = $("#detail-logs");
    const atBottom = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 8;
    pre.textContent = pre.textContent === "–" || !pre.textContent
      ? line : `${pre.textContent}\n${line}`;
    const lines = pre.textContent.split("\n");
    if (lines.length > 600) pre.textContent = lines.slice(-500).join("\n");
    if (atBottom) pre.scrollTop = pre.scrollHeight;
  }

  function startDetailLogPolling() {
    if (state.detailLogTimer || !state.detailId) return;
    loadDetailLogs();
    state.detailLogTimer = setInterval(loadDetailLogs, 5000);
  }

  async function openDetailLogStream() {
    if (!state.detailId || state.detailLogStream) return;
    const id = state.detailId;
    const controller = new AbortController();
    state.detailLogStream = controller;
    // Watchdog: stirbt die Verbindung lautlos (kein Fehler, kein Ende),
    // hängt der fetch sonst ewig → nach 30 s ohne Daten abbrechen und auf
    // Polling umschalten. Der Server schickt alle 15 s SSE-Keepalives.
    let watchdogFired = false;
    let lastData = Date.now();
    const watchdog = setInterval(() => {
      if (Date.now() - lastData > 30000) {
        watchdogFired = true;
        controller.abort();
      }
    }, 5000);
    try {
      // EventSource kann keine Header senden — SSE wird per fetch gelesen
      const res = await fetch(`/api/instances/${id}/logs/stream?tail=100`, {
        headers: state.apiKey ? { "X-API-Key": state.apiKey } : {},
        signal: controller.signal,
      });
      if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`);
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        lastData = Date.now(); // auch Keepalive-Kommentare zählen als Lebenzeichen
        buffer += decoder.decode(value, { stream: true });
        let idx;
        while ((idx = buffer.indexOf("\n\n")) >= 0) {
          const chunk = buffer.slice(0, idx);
          buffer = buffer.slice(idx + 2);
          for (const rawLine of chunk.split("\n")) {
            if (!rawLine.startsWith("data: ")) continue;
            if (state.detailId !== id) return; // inzwischen andere Instanz
            try {
              appendLogLine(JSON.parse(rawLine.slice(6)));
            } catch (e) { /* unvollständige Events überspringen */ }
          }
        }
      }
      if (state.detailId === id) {
        appendLogLine("(Stream beendet — Container gestoppt)");
        // Der Container kann neu starten (z. B. Crash-Loop) → Logs nicht
        // einschlafen lassen, sondern per Polling weiter verfolgen.
        startDetailLogPolling();
      }
    } catch (e) {
      if (controller.signal.aborted && !watchdogFired) return; // Dialog geschlossen
      if (state.detailId === id) startDetailLogPolling(); // Fallback: Polling
    } finally {
      clearInterval(watchdog);
      if (state.detailLogStream === controller) state.detailLogStream = null;
    }
  }

  /* ---------- Detail-Ansicht ---------- */
  async function openDetail(id) {
    state.detailId = id;
    updateInstSelection(); // Karte im Server-Tab hervorheben
    closeDetailLogStream(); // Stream/Timer der vorherigen Instanz beenden
    showSoft($("#inst-detail"), true);
    // Mobil/Tablet: Detail liegt unter dem Karten-Raster — dorthin scrollen
    if (window.matchMedia("(max-width: 760px)").matches) {
      $("#inst-detail").scrollIntoView({ behavior: "smooth", block: "start" });
    }
    $("#detail-logs").textContent = "–";
    // Filter der Mod-Liste zurücksetzen (eingeklappt bleibt eingeklappt)
    $("#mods-filter-input").value = "";
    // RCON-Ansicht zurücksetzen (Spielerliste lädt per Button)
    $("#rcon-players").textContent = "";
    $("#rcon-target").value = "";
    setRconOutput("");
    show($("#rcon-error"), false);
    show($("#rcon-players-empty"), true);
    setText($("#rcon-hint"), "Funktioniert nur bei laufendem Server. Bestehende Server müssen einmal neu gestartet werden, damit RCON aktiv ist.");
    // Konsole zurücksetzen (Verlauf bleibt sessionweit erhalten)
    show($("#console-error"), false);
    show($("#console-log"), false);
    $("#console-log").textContent = "";
    $("#console-input").value = "";
    // Whitelist-Editor zurücksetzen und Datei laden
    state.wlEntries = [];
    renderWhitelistStatic();
    loadWhitelist();
    // Mod-Update-Ansicht zurücksetzen (Ergebnis ist instanzbezogen)
    state.detailUpdates = null;
    setText($("#mods-update-summary"), "");
    show($("#mods-update-all"), false);
    show($("#mods-update-progress"), false);
    show($("#mods-update-error"), false);
    // Upload-UI für die neue Instanz zurücksetzen
    show($("#upload-error"), false);
    $("#pack-upload-file").value = "";
    $("#pack-upload-btn").disabled = true;
    // Welt-Upload zurücksetzen
    show($("#world-error"), false);
    $("#world-upload-file").value = "";
    $("#world-upload-btn").disabled = true;
    // Datei-Browser zurücksetzen (Wurzel der neuen Instanz laden)
    state.fb = { path: "", entries: [] };
    fbReload();
    dpReload();
    // Gamerules zurücksetzen (werden per „Laden" geholt — nur laufend)
    state.grRules = [];
    $("#gr-filter").value = "";
    renderGamerules();
    show($("#gr-empty"), true);
    grSetError("");
    const detail = await loadDetail();
    if (detail) {
      // JVM-Optionen + RAM der Instanz in den Editor laden
      $("#jvm-opts").value = detail.jvm_opts || "";
      $("#jvm-aikar").checked = !!detail.use_aikar;
      setRamFields(detail.memory);
      state.detailMemory = (detail.memory || "2G").toUpperCase();
      show($("#jvm-error"), false);
      // Zeitplan der Instanz in die Felder laden
      loadSchedule(detail);
      show($("#sched-error"), false);
      // Tags + Port in die Felder laden
      loadTagsAndPort(detail);
      // Modpack-Update-Check direkt beim Öffnen (still bei Fehlern)
      show($("#mp-update-btn"), false);
      setText($("#mp-update-info"), "Update-Check läuft…");
      show($("#mp-update-error"), false);
      checkPackUpdate(true);
    }
    if (state.cfgOpen) loadCfg();
    loadBackups();
    loadWorldInfo();
  }
  function closeDetail() {
    state.detailId = null;
    updateInstSelection(); // Hervorhebung aufheben
    closeDetailLogStream();
    showSoft($("#inst-detail"), false);
  }
  $("#detail-close").addEventListener("click", closeDetail);

  async function loadDetail() {
    if (!state.detailId) return null;
    const id = state.detailId;
    show($("#detail-error"), false);
    try {
      const detail = await api(`/api/instances/${id}`);
      if (state.detailId !== id) return null; // inzwischen gewechselt/geschlossen
      setText($("#detail-title"), detail.name);
      const badge = $("#detail-state");
      badge.className = "badge inst-badge";
      badge.classList.add(stateBadgeClass(detail));
      setText($("#detail-state-text"), OV_STATE_LABELS[instStateKey(detail)]);
      renderDetailInfo(detail);
      renderDetailMods(detail.mods || []);
      state.detailRunning = !!detail.container?.running;
      setText($("#pack-target"),
        `Ziel: ${detail.loader} ${detail.game_version}${detail.loader_version ? ` (${detail.loader_version})` : ""}`);
      if (detail.container?.running) openDetailLogStream();
      else loadDetailLogs();
      return detail;
    } catch (e) {
      if (e.status === 404) { closeDetail(); return null; }
      setText($("#detail-error"), `Details nicht abrufbar: ${e.message}`);
      show($("#detail-error"), true);
      return null;
    }
  }

  function renderDetailInfo(detail) {
    const box = $("#detail-info");
    box.textContent = "";
    const errBox = $("#detail-error");
    if (detail.status === "error" && detail.error) {
      setText(errBox, detail.error);
      show(errBox, true);
    } else {
      show(errBox, false);
    }
    const ping = detail.ping || {};
    const up = detail.container?.running && detail.container.started_at
      ? fmtUptime(detail.container.started_at) : null;
    const disk = detail.disk || null;
    const diskLine = disk ? `${fmtBytes(disk.total_bytes)}`
      + (disk.total_bytes ? ` · Mods ${fmtBytes(disk.mods_bytes)}`
        + ` · Welt ${fmtBytes(disk.world_bytes)}`
        + ` · Packs ${fmtBytes(disk.packs_bytes)}` : "")
      : null;
    const items = [
      ["Status", OV_STATE_LABELS[instStateKey(detail)]],
      ["Docker", detail.container?.state || "–"],
      ["Uptime", up || "–"],
      ["Loader", `${detail.loader}${detail.loader_version ? ` ${detail.loader_version}` : ""}`],
      ["Minecraft", detail.game_version],
      ["Port", String(detail.port)],
      ["RAM", detail.memory || "2G"],
      ["Speicher", diskLine || "–"],
      ["Erstellt", fmtDate(detail.created_at)],
      ["Erreichbar", ping.online ? "ja" : "nein"],
      ["Spieler", ping.online && ping.players
        ? `${ping.players.online}/${ping.players.max}` : "–"],
      ["Server-Version", ping.version || "–"],
      ["Verzeichnis", `${detail.id}/`],
    ];
    if (detail.modpack?.title) {
      items.push(["Modpack", `${detail.modpack.title} (${detail.modpack.files} Dateien)`]);
    }
    const dl = document.createElement("dl");
    dl.className = "kv";
    for (const [k, v] of items) {
      const dt = document.createElement("dt");
      dt.textContent = k;
      const dd = document.createElement("dd");
      dd.textContent = v;
      dl.append(dt, dd);
    }
    box.appendChild(dl);
  }

  function renderDetailMods(mods) {
    state.detailMods = mods || [];
    applyModFilter();
  }

  // Kompakte, filterbare Mod-Liste: Zusammenfassung + Zeilen
  function applyModFilter() {
    const list = $("#detail-mods");
    list.textContent = "";
    const all = state.detailMods;
    const q = ($("#mods-filter-input").value || "").trim().toLowerCase();
    const mods = q ? all.filter((m) => m.filename.toLowerCase().includes(q)) : all;

    const active = all.filter((m) => m.enabled).length;
    const bytes = all.reduce((s, m) => s + (m.size_bytes || 0), 0);
    setText($("#detail-mods-count"),
      q ? `(${mods.length} von ${all.length})` : `(${all.length})`);
    setText($("#mods-summary"), all.length
      ? `${active} aktiv · ${all.length - active} deaktiviert · ${fmtBytes(bytes)} gesamt`
      : "");
    show($("#detail-mods-empty"), mods.length === 0);

    list.classList.toggle("collapsed", state.modsCollapsed);
    setText($("#mods-collapse-btn"), state.modsCollapsed ? "Ausklappen" : "Einklappen");

    for (const mod of mods) {
      const node = $("#tpl-mod-row").content.cloneNode(true);
      const nameEl = node.querySelector(".mod-name");
      nameEl.textContent = mod.filename;
      nameEl.title = mod.filename; // volle Nummer bei Abschneiden per Tooltip
      const updateItem = state.detailUpdates?.[mod.filename];
      const updateAvailable = updateItem?.status === "update_available";
      setText(node.querySelector(".mod-meta"),
        `${fmtBytes(mod.size_bytes)}${mod.enabled ? "" : " · deaktiviert"}`
        + (updateAvailable
          ? ` · Update: ${updateItem.latest?.version_number || "?"}`
          : ""));
      if (!mod.enabled) node.querySelector(".mod-row").classList.add("off");
      const updateBtn = node.querySelector(".update");
      if (updateAvailable) {
        show(updateBtn, true);
        updateBtn.title = `Aktualisieren auf ${updateItem.latest?.version_number || "neueste Version"}`;
        updateBtn.addEventListener("click", (e) =>
          startModUpdates([mod.filename], e.target));
      }
      const toggleBtn = node.querySelector(".toggle");
      setText(toggleBtn, mod.enabled ? "Aus" : "An");
      toggleBtn.title = mod.enabled ? "Mod deaktivieren" : "Mod aktivieren";
      toggleBtn.addEventListener("click", () =>
        toggleInstanceMod(mod.filename, !mod.enabled, toggleBtn));
      node.querySelector(".delete").addEventListener("click", async () => {
        if (!window.confirm(`"${mod.filename}" löschen?`)) return;
        try {
          await api(`/api/instances/${state.detailId}/mods/${encodeURIComponent(mod.filename)}`,
            { method: "DELETE" });
          loadDetail();
        } catch (e) {
          toast(`Löschen fehlgeschlagen: ${e.message}`, "error");
        }
      });
      list.appendChild(node);
    }
  }

  async function toggleInstanceMod(filename, enabled, btn) {
    const id = state.detailId;
    if (!id) return;
    btn.disabled = true;
    try {
      await api(`/api/instances/${id}/mods/${encodeURIComponent(filename)}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled }),
      });
      toast(`"${filename}" ${enabled ? "aktiviert" : "deaktiviert"}.`, "success");
      loadDetail();
    } catch (e) {
      btn.disabled = false;
      toast(`${enabled ? "Aktivieren" : "Deaktivieren"} fehlgeschlagen: ${e.message}`, "error");
    }
  }

  /* ---------- Mod-Updates (Modrinth-SHA1 / CurseForge-Fingerprint) ---------- */
  function modUpdateSetError(msg) {
    const box = $("#mods-update-error");
    if (msg) {
      setText(box, msg);
      show(box, true);
    } else {
      show(box, false);
    }
  }

  async function checkModUpdates() {
    if (!state.detailId) return;
    const btn = $("#mods-update-check");
    btn.disabled = true;
    btn.textContent = "Prüfe…";
    modUpdateSetError("");
    setText($("#mods-update-summary"), "");
    show($("#mods-update-all"), false);
    try {
      const data = await api(`/api/instances/${state.detailId}/mods/update-check`,
        { method: "POST" });
      state.detailUpdates = {};
      for (const item of data.items || []) state.detailUpdates[item.filename] = item;
      const note = data.curseforge_enabled ? ""
        : " · CurseForge unprüft (kein CF_API_KEY)";
      setText($("#mods-update-summary"),
        data.checked
          ? `${data.updatable} von ${data.checked} Mods aktualisierbar${note}`
          : "Keine Mods gefunden");
      show($("#mods-update-all"), data.updatable > 0);
      if (data.updatable > 0) {
        toast(`${data.updatable} Mod-Update(s) verfügbar.`, "success");
      }
      applyModFilter(); // Update-Chips in der Mod-Liste auffrischen
    } catch (e) {
      modUpdateSetError(`Update-Check fehlgeschlagen: ${e.message}`);
    } finally {
      btn.disabled = false;
      btn.textContent = "Updates prüfen";
    }
  }

  async function startModUpdates(filenames, btn) {
    if (!state.detailId) return;
    modUpdateSetError("");
    const trigger = btn || $("#mods-update-all");
    const prevText = trigger.textContent;
    trigger.disabled = true;
    if (trigger === $("#mods-update-all")) trigger.textContent = "Aktualisiere…";
    try {
      const job = await api(`/api/instances/${state.detailId}/mods/update`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ filenames: filenames || null }),
      });
      await pollProgress(job, {
        box: $("#mods-update-progress"),
        bar: $("#mods-update-bar"),
        phase: $("#mods-update-phase"),
        pct: $("#mods-update-pct"),
      });
      const summary = job.summary
        ? `${job.summary.updated} aktualisiert${job.summary.failed ? `, ${job.summary.failed} fehlgeschlagen` : ""}.`
        : "Aktualisiert.";
      setText($("#mods-update-summary"), summary);
      if (job.summary?.failed) {
        modUpdateSetError(`Fehler bei: ${job.summary.errors.join(" · ")}`);
      } else {
        toast(`Mods aktualisiert: ${job.summary?.updated ?? "?"}.`, "success");
      }
      state.detailUpdates = null;
      show($("#mods-update-all"), false);
      loadDetail();
    } catch (e) {
      modUpdateSetError(`Update fehlgeschlagen: ${e.message}`);
    } finally {
      trigger.disabled = false;
      if (trigger === $("#mods-update-all")) trigger.textContent = prevText;
    }
  }

  $("#mods-update-check").addEventListener("click", checkModUpdates);
  $("#mods-update-all").addEventListener("click", () => startModUpdates(null));

  async function loadDetailLogs() {
    if (!state.detailId) return;
    const pre = $("#detail-logs");
    try {
      const data = await api(`/api/instances/${state.detailId}/logs?tail=150`);
      pre.textContent = data.logs.length ? data.logs.join("\n") : "(keine Logs)";
      pre.scrollTop = pre.scrollHeight;
    } catch (e) {
      pre.textContent = `Logs nicht abrufbar: ${e.message}`;
    }
  }
  $("#detail-logs-reload").addEventListener("click", loadDetailLogs);

  /* ---------- Statistik-Verlauf (persistente Charts) ---------- */
  const CHART_COLORS = ["#f97316", "#60a5fa", "#2dd4bf", "#fbbf24",
    "#a855f7", "#4ade80", "#ef4444", "#fda4af"];

  function renderChart(box, seriesList, w = 720, h = 150) {
    box.textContent = "";
    const all = seriesList.flatMap((s) => s.values.filter((v) => v != null));
    if (all.length < 2) return false;
    const vMax = Math.max(1e-9, ...all);
    const svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
    svg.setAttribute("preserveAspectRatio", "none");
    svg.classList.add("linechart");
    seriesList.forEach((s, idx) => {
      const segs = [];
      let cur = [];
      s.values.forEach((v, i) => {
        if (v == null) {
          if (cur.length > 1) segs.push(cur);
          cur = [];
          return;
        }
        const x = s.values.length > 1 ? (i / (s.values.length - 1)) * (w - 4) + 2 : w / 2;
        const y = h - 3 - (Math.max(0, v) / vMax) * (h - 6);
        cur.push(`${x.toFixed(1)},${y.toFixed(1)}`);
      });
      if (cur.length > 1) segs.push(cur);
      for (const pts of segs) {
        const line = document.createElementNS(SVG_NS, "polyline");
        line.setAttribute("points", pts.join(" "));
        line.setAttribute("fill", "none");
        line.setAttribute("stroke", s.color || CHART_COLORS[idx % CHART_COLORS.length]);
        line.setAttribute("stroke-width", "1.8");
        line.setAttribute("stroke-linejoin", "round");
        svg.appendChild(line);
      }
    });
    box.appendChild(svg);
    return vMax;
  }

  async function loadHistory() {
    const hours = parseInt($("#stats-range").value, 10) || 24;
    show($("#stats-error"), false);
    try {
      const data = await api(`/api/history?hours=${hours}`);
      renderHistory(data);
    } catch (e) {
      setText($("#stats-error"), `Verlauf nicht ladbar: ${e.message}`);
      show($("#stats-error"), true);
    }
  }

  function renderHistory(data) {
    const points = data.points || [];
    show($("#stats-empty"), points.length === 0);
    if (!points.length) {
      $("#chart-cpu").textContent = "";
      $("#chart-ram").textContent = "";
      $("#chart-players").textContent = "";
      $("#stats-players-legend").textContent = "";
      setText($("#stats-cpu-now"), "");
      setText($("#stats-ram-now"), "");
      setText($("#stats-players-now"), "");
      return;
    }
    const last = points[points.length - 1];
    setText($("#stats-cpu-now"), `aktuell: ${last.cpu} %`);
    setText($("#stats-ram-now"), `aktuell: ${fmtMb(last.ram_mb)}`);
    setText($("#stats-players-now"), `aktuell: ${Math.round(last.players)} online`);
    renderChart($("#chart-cpu"), [{ values: points.map((p) => p.cpu), color: "#f97316" }]);
    renderChart($("#chart-ram"), [{ values: points.map((p) => p.ram_mb), color: "#60a5fa" }]);

    // Spieler: Gesamtlinie + eine Linie je Instanz (auf dasselbe Zeitraster
    // gemappt; Lücken = null unterbrechen die Linie)
    const grid = points.map((p) => p.ts);
    const instanceSeries = [];
    for (const [key, entries] of Object.entries(data.instances || {})) {
      const byTs = new Map(entries.map((e) => [e.ts, e.players]));
      const values = grid.map((ts) => {
        const v = byTs.get(ts);
        return v == null ? null : v;
      });
      if (values.some((v) => v != null)) {
        instanceSeries.push({ values, color: null,
          label: data.names?.[key] || key.slice(0, 8) });
      }
    }
    const total = { values: points.map((p) => p.players), color: "#fbbf24",
      label: "Gesamt" };
    renderChart($("#chart-players"), [total, ...instanceSeries]);
    // Legende: Gesamt + Instanzen mit letztem Wert
    const legend = $("#stats-players-legend");
    legend.textContent = "";
    const legendItems = [total, ...instanceSeries];
    legendItems.forEach((s, idx) => {
      const item = document.createElement("span");
      item.className = "legend-item";
      const dot = document.createElement("span");
      dot.className = "legend-dot";
      dot.style.background = s.color || CHART_COLORS[idx % CHART_COLORS.length];
      const lastV = [...s.values].reverse().find((v) => v != null);
      item.append(dot, document.createTextNode(
        `${s.label} (${lastV != null ? Math.round(lastV) : 0})`));
      legend.appendChild(item);
    });
  }

  $("#stats-reload").addEventListener("click", loadHistory);
  $("#stats-range").addEventListener("change", loadHistory);

  /* ---------- Spielzeit-Leaderboard ---------- */
  function fmtDuration(seconds) {
    if (typeof seconds !== "number" || seconds <= 0) return "0 min";
    const s = Math.round(seconds);
    const d = Math.floor(s / 86400);
    const h = Math.floor((s % 86400) / 3600);
    const m = Math.floor((s % 3600) / 60);
    if (d > 0) return `${d} d ${h} h`;
    if (h > 0) return m > 0 ? `${h} h ${m} min` : `${h} h`;
    return `${m} min`;
  }

  async function loadPlaytime() {
    const range = $("#playtime-range").value || "24";
    show($("#playtime-error"), false);
    try {
      const data = await api(`/api/players/playtime?hours=${encodeURIComponent(range)}`);
      renderPlaytime(data.players || []);
    } catch (e) {
      show($("#playtime-list"), false);
      setText($("#playtime-error"), `Spielzeit nicht ladbar: ${e.message}`);
      show($("#playtime-error"), true);
    }
  }

  function renderPlaytime(players) {
    const list = $("#playtime-list");
    list.textContent = "";
    show($("#playtime-empty"), players.length === 0);
    show($("#playtime-list"), players.length > 0);
    const names = state.ovInstances?.instances
      ? new Map(state.ovInstances.instances.map((i) => [i.id, i.name]))
      : new Map();
    players.slice(0, 50).forEach((p, idx) => {
      const li = document.createElement("li");
      li.className = "mod-row";
      const info = document.createElement("div");
      info.className = "mod-info";
      const rank = document.createElement("span");
      rank.className = "pt-rank muted";
      rank.textContent = String(idx + 1);
      const name = document.createElement("span");
      name.className = "mod-name";
      name.textContent = p.player;
      const meta = document.createElement("span");
      meta.className = "mod-meta muted small";
      const parts = [];
      if (p.last_seen) parts.push(`zuletzt ${fmtDate(p.last_seen)}`);
      const instNames = Object.entries(p.per_instance || {})
        .map(([id, secs]) => `${names.get(id) || id.slice(0, 8)}: ${fmtDuration(secs)}`);
      if (instNames.length) parts.push(instNames.join(", "));
      meta.textContent = parts.join(" · ");
      info.append(rank, name, meta);
      const value = document.createElement("span");
      value.className = "pt-value";
      value.textContent = fmtDuration(p.seconds);
      li.append(info, value);
      list.appendChild(li);
    });
  }

  $("#playtime-reload").addEventListener("click", loadPlaytime);
  $("#playtime-range").addEventListener("change", loadPlaytime);

  // Mod-Liste: Filter + Ein-/Ausklappen
  $("#mods-filter-input").addEventListener("input", applyModFilter);
  $("#mods-collapse-btn").addEventListener("click", () => {
    state.modsCollapsed = !state.modsCollapsed;
    applyModFilter();
  });

  /* ---------- Modpack-Suche + Installation ---------- */
  $("#pack-search-input").addEventListener("input", () => {
    clearTimeout(state.packTimer);
    state.packTimer = setTimeout(() => searchPacks(0), 400);
  });

  async function searchPacks(offset) {
    if (!state.detailId) return;
    state.packOffset = offset;
    show($("#pack-loading"), true);
    show($("#pack-error"), false);
    try {
      const params = new URLSearchParams({
        q: $("#pack-search-input").value.trim(),
        offset: String(offset),
        source: state.packSource,
      });
      const data = await api(`/api/instances/${state.detailId}/modpacks/search?${params}`);
      state.packTotal = data.total;
      renderPacks(data, "#pack-results");
    } catch (e) {
      setText($("#pack-error"), `Modpack-Suche fehlgeschlagen: ${e.message}`);
      show($("#pack-error"), true);
      $("#pack-results").textContent = "";
    } finally {
      show($("#pack-loading"), false);
    }
  }

  function renderPacks(data, containerSel, onInstall) {
    const container = $(containerSel);
    container.textContent = "";
    const target = data.instance || null; // globale Suche hat kein Instanz-Ziel
    for (const hit of data.hits) {
      hit._source = data.source || "modrinth"; // Quelle pro Treffer merken
      const node = $("#tpl-pack-row").content.cloneNode(true);
      const icon = node.querySelector(".result-icon");
      if (hit.icon_url && hit.icon_url.startsWith("https://")) {
        const img = document.createElement("img");
        img.src = hit.icon_url;
        img.alt = "";
        img.loading = "lazy";
        icon.appendChild(img);
      } else {
        icon.textContent = (hit.title || "?").charAt(0).toUpperCase();
      }
      setText(node.querySelector(".name"), hit.title || hit.slug);
      const compat = node.querySelector(".pack-compat");
      if (hit.compatible === true) {
        compat.classList.add("ok");
        compat.textContent = "kompatibel";
      } else if (hit.compatible === false) {
        compat.classList.add("no");
        compat.textContent = "inkompatibel";
      } else if (target) {
        compat.classList.add("warn");
        compat.textContent = "Kompatibilität wird bei Installation geprüft";
      }
      setText(node.querySelector(".desc"), hit.description || "");
      setText(node.querySelector(".loaders"), `Loader: ${hit.loaders.join(", ") || "–"}`);
      setText(node.querySelector(".versions"),
        `MC: ${(hit.versions || []).slice(-3).join(", ") || "–"}`);
      setText(node.querySelector(".downloads"), `${fmtNumber(hit.downloads)} Downloads`);
      const btn = node.querySelector(".install");
      if (hit.compatible === false) {
        btn.disabled = true;
        btn.title = "Nicht kompatibel mit dieser Instanz";
      } else if (!target) {
        btn.disabled = true;
        btn.title = "Keine Instanz gewählt — „Neuer Server“ nutzen";
      }
      btn.addEventListener("click", () => (onInstall || installPack)(hit, btn));
      node.querySelector(".server-create").addEventListener("click", () =>
        openMpCreate({ mode: "pack", hit }));
      container.appendChild(node);
    }
  }

  async function installPackAt(instanceId, hit, btn, onSuccess, els) {
    if (!instanceId) return;
    btn.disabled = true;
    btn.textContent = "Starte…";
    const start = (force) => api(`/api/instances/${instanceId}/modpacks/install`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        project_id: hit.project_id, force,
        source: hit._source || state.packSource,
      }),
    });
    try {
      let job;
      try {
        job = await start(false);
      } catch (e) {
        if (e.status !== 409) throw e;
        if (!window.confirm(`"${e.message}"\n\nModpack ersetzen und neu installieren?`)) {
          btn.disabled = false;
          btn.textContent = "Installieren";
          return;
        }
        job = await start(true);
      }
      await (els ? pollProgress(job, els) : pollPackJob(job));
      toast(`Modpack "${hit.title}" installiert.`, "success");
      if (onSuccess) onSuccess();
      btn.textContent = "Installiert";
    } catch (e) {
      toast(`Modpack-Installation fehlgeschlagen: ${e.message}`, "error");
      btn.disabled = false;
      btn.textContent = "Installieren";
    }
  }

  async function installPack(hit, btn) {
    return installPackAt(state.detailId, hit, btn, loadDetail);
  }

  function pollProgress(job, els) {
    const { box, bar, phase, pct } = els;
    show(box, true);
    return new Promise((resolve, reject) => {
      const timer = setInterval(async () => {
        try {
          const data = await api(`/api/jobs/${job.job_id}`);
          const progress = data.total > 0
            ? Math.min(100, Math.round((data.downloaded / data.total) * 100)) : 0;
          bar.style.width = progress + "%";
          setText(phase, data.phase || data.status);
          setText(pct, data.total > 0 ? `${progress} %` : "");
          if (data.status === "done") {
            clearInterval(timer);
            show(box, false);
            resolve(data);
          } else if (data.status === "error") {
            clearInterval(timer);
            show(box, false);
            reject(new Error(data.error || "Installation fehlgeschlagen"));
          }
        } catch (e) {
          clearInterval(timer);
          show(box, false);
          reject(e);
        }
      }, 900);
    });
  }

  function pollPackJob(job) {
    return pollProgress(job, {
      box: $("#pack-progress"),
      bar: $("#pack-bar"),
      phase: $("#pack-phase"),
      pct: $("#pack-pct"),
    });
  }

  /* ---------- Modpack-Upload (.mrpack / CurseForge-.zip) ---------- */
  async function uploadPackCore(instanceId, file, els) {
    const { btn, errorBox, fileInput, poll, onSuccess } = els;
    const fileName = file.name;
    show(errorBox, false);
    const problem = await packFileProblem(file);
    if (problem) {
      setText(errorBox, problem);
      show(errorBox, true);
      return;
    }
    btn.disabled = true;
    btn.textContent = "Lade hoch…";
    const send = (force) => {
      const form = new FormData();
      form.append("file", file);
      if (force) form.append("force", "true");
      return apiUpload(`/api/instances/${instanceId}/modpacks/upload`, form,
        (pct) => { btn.textContent = `Lade hoch… ${pct} %`; });
    };
    try {
      let job;
      try {
        job = await send(false);
      } catch (e) {
        if (e.status !== 409) throw e;
        // Laufende Installation (kein force möglich) → Fehlermeldung durchreichen
        if (/läuft bereits/i.test(e.message || "")) throw e;
        if (!window.confirm(`${e.message}\n\nModpack ersetzen und neu installieren?`)) {
          btn.textContent = "Installieren";
          return;
        }
        job = await send(true);
      }
      const result = await poll(job);
      const adapted = result?.summary?.version_adapted;
      if (adapted?.to) {
        toast(`Modpack "${fileName}" installiert — Server auf Minecraft `
          + `${adapted.to.game_version} (${adapted.to.loader}) umgestellt.`, "success");
      } else {
        toast(`Modpack "${fileName}" installiert.`, "success");
      }
      fileInput.value = "";
      btn.textContent = "Installieren";
      if (onSuccess) onSuccess();
    } catch (e) {
      setText(errorBox, `Upload/Installation fehlgeschlagen: ${e.message}`);
      show(errorBox, true);
      btn.textContent = "Installieren";
    } finally {
      btn.disabled = !fileInput.files.length;
    }
  }

  $("#pack-upload-file").addEventListener("change", () => {
    $("#pack-upload-btn").disabled = !$("#pack-upload-file").files.length;
    show($("#upload-error"), false);
  });
  $("#pack-upload-btn").addEventListener("click", () => {
    const file = $("#pack-upload-file").files[0];
    if (file && state.detailId) {
      uploadPackCore(state.detailId, file, {
        btn: $("#pack-upload-btn"),
        errorBox: $("#upload-error"),
        fileInput: $("#pack-upload-file"),
        poll: pollPackJob,
        onSuccess: loadDetail,
      });
    }
  });

  /* ---------- Eigener Modpack-Upload-Tab ---------- */
  const upEls = {
    file: $("#up-file"),
    btn: $("#up-install-btn"),
    info: $("#up-file-info"),
    target: $("#up-target-select"),
    existingOpts: $("#up-existing-opts"),
    newOpts: $("#up-new-opts"),
    autoVersion: $("#up-auto-version"),
    force: $("#up-force"),
    name: $("#up-name"),
    memory: $("#up-memory"),
    eula: $("#up-eula"),
    progress: { box: $("#up-progress"), bar: $("#up-bar"),
                phase: $("#up-phase"), pct: $("#up-pct") },
    error: $("#up-error"),
    summary: $("#up-summary"),
    summaryText: $("#up-summary-text"),
    skippedList: $("#up-skipped-list"),
    failedBox: $("#up-failed-box"),
    failedText: $("#up-failed-text"),
    failedList: $("#up-failed-list"),
  };

  async function loadUploadTargets() {
    try {
      const data = await api("/api/instances");
      const options = data.instances.map((i) => ({
        value: i.id, label: `${i.name} (${i.loader} ${i.game_version})`,
      }));
      const current = upEls.target.value;
      fillSelect(upEls.target, options, "Neuen Server aus dem Pack erstellen");
      if (current && options.some((o) => o.value === current)) {
        upEls.target.value = current;
      }
    } catch (e) {
      fillSelect(upEls.target, [], "Instanzen nicht abrufbar — neuer Server");
    }
    syncUploadMode();
  }

  function syncUploadMode() {
    const existing = !!upEls.target.value;
    show(upEls.existingOpts, existing);
    show(upEls.newOpts, !existing);
  }
  upEls.target.addEventListener("change", syncUploadMode);

  upEls.file.addEventListener("change", async () => {
    const file = upEls.file.files[0];
    show(upEls.error, false);
    show(upEls.summary, false);
    show(upEls.info, false);
    upEls.btn.disabled = !file;
    if (!file) return;
    const problem = await packFileProblem(file);
    if (problem) {
      setText(upEls.error, problem);
      show(upEls.error, true);
      upEls.btn.disabled = true;
      return;
    }
    setText(upEls.info, `Datei: ${file.name} · ${fmtBytes(file.size)} · Format wird beim Installieren aus dem Archiv gelesen`);
    show(upEls.info, true);
  });

  // Job-Polling mit Ergebnis-/Fehlerdetails (failed-Liste, Summary)
  function pollUploadJob(job, els) {
    const { box, bar, phase, pct } = els;
    show(box, true);
    return new Promise((resolve, reject) => {
      const timer = setInterval(async () => {
        try {
          const data = await api(`/api/jobs/${job.job_id}`);
          const progress = data.total > 0
            ? Math.min(100, Math.round((data.downloaded / data.total) * 100)) : 0;
          bar.style.width = progress + "%";
          setText(phase, data.phase || data.status);
          setText(pct, data.total > 0 ? `${progress} %` : "");
          if (data.status === "done") {
            clearInterval(timer);
            show(box, false);
            resolve(data);
          } else if (data.status === "error") {
            clearInterval(timer);
            show(box, false);
            reject(Object.assign(
              new Error(data.error || "Installation fehlgeschlagen"),
              { failed: data.failed || [], summary: data.summary }));
          }
        } catch (e) {
          clearInterval(timer);
          show(box, false);
          reject(e);
        }
      }, 900);
    });
  }

  function showUploadSummary(text, skipped, failed) {
    setText(upEls.summaryText, text);
    const skippedLines = (skipped || []).slice(0, 10);
    if (skippedLines.length) {
      setText(upEls.skippedList,
        `Übersprungen (${skipped.length}): ${skippedLines.join(" · ")}` +
        (skipped.length > skippedLines.length
          ? ` … +${skipped.length - skippedLines.length} weitere` : ""));
      show(upEls.skippedList, true);
    } else {
      show(upEls.skippedList, false);
    }
    if (failed && failed.length) {
      upEls.failedList.textContent = "";
      for (const line of failed.slice(0, 10)) {
        const li = document.createElement("li");
        li.textContent = line; // textContent schützt vor XSS
        upEls.failedList.appendChild(li);
      }
      if (failed.length > 10) {
        const li = document.createElement("li");
        li.textContent = `… +${failed.length - 10} weitere`;
        upEls.failedList.appendChild(li);
      }
      setText(upEls.failedText, "Nicht installiert:");
      show(upEls.failedBox, true);
    } else {
      show(upEls.failedBox, false);
    }
    show(upEls.summary, true);
  }

  upEls.btn.addEventListener("click", async () => {
    const file = upEls.file.files[0];
    if (!file) return;
    show(upEls.error, false);
    show(upEls.summary, false);
    const instanceId = upEls.target.value;
    if (!instanceId && !upEls.eula.checked) {
      setText(upEls.error, "Bitte die Minecraft-EULA akzeptieren.");
      show(upEls.error, true);
      return;
    }
    upEls.btn.disabled = true;
    upEls.btn.textContent = "Lade hoch…";
    const sendExisting = (force) => {
      const form = new FormData();
      form.append("file", file);
      if (force) form.append("force", "true");
      if (!upEls.autoVersion.checked) form.append("auto_version", "false");
      return apiUpload(`/api/instances/${instanceId}/modpacks/upload`, form,
        (pct) => { upEls.btn.textContent = `Lade hoch… ${pct} %`; });
    };
    try {
      let job;
      if (instanceId) {
        try {
          job = await sendExisting(upEls.force.checked);
        } catch (e) {
          if (e.status !== 409) throw e;
          if (/läuft bereits/i.test(e.message || "")) throw e;
          if (!window.confirm(`${e.message}\n\nModpack ersetzen und neu installieren?`)) {
            upEls.btn.textContent = "Installieren";
            return;
          }
          job = await sendExisting(true);
        }
      } else {
        const form = new FormData();
        form.append("file", file);
        const name = upEls.name.value.trim();
        if (name) form.append("name", name);
        const memory = upEls.memory.value;
        if (memory) form.append("memory", memory);
        form.append("accept_eula", "true");
        job = await apiUpload("/api/instances/from-pack-upload", form,
          (pct) => { upEls.btn.textContent = `Lade hoch… ${pct} %`; });
      }
      const created = job.instance || null;
      if (created) job = { job_id: job.job.id }; // from-pack-upload: {instance, job}
      const result = await pollUploadJob(job, upEls.progress);
      const summary = result?.summary || {};
      const targetLabel = created
        ? `Neuer Server "${created.name}" (Port ${created.port})`
        : `Instanz aktualisiert`;
      showUploadSummary(
        `${targetLabel} · Modpack "${result?.filename || file.name}" installiert — ${summary.files ?? "?"} Dateien.`,
        summary.skipped || [], []);
      toast(`Modpack "${file.name}" installiert.`, "success");
      upEls.file.value = "";
      show(upEls.info, false);
    } catch (e) {
      setText(upEls.error, `Upload/Installation fehlgeschlagen: ${e.message}`);
      show(upEls.error, true);
      // Teilfortschritt + fehlgeschlagene Dateien zeigen
      const skipped = e.summary?.skipped || [];
      showUploadSummary(
        `Fehlgeschlagen — installiert: ${e.summary?.files ?? 0} Dateien` +
        (skipped.length ? ` · übersprungen: ${skipped.length}` : "") + ".",
        skipped, e.failed || []);
    } finally {
      upEls.btn.disabled = !upEls.file.files.length;
      upEls.btn.textContent = "Installieren";
    }
  });

  /* ---------- Geteilte MC-Version-Liste (Mod-/Modpack-Filter) ---------- */
  state.mcVersionOptions = null;

  async function fetchMcVersionOptions() {
    if (state.mcVersionOptions) return state.mcVersionOptions;
    const data = await api("/api/catalog/mc-versions");
    const options = data.releases.map((v) => ({ value: v, label: v }));
    for (const snap of data.snapshots.slice(0, 25)) {
      options.push({ value: snap, label: `${snap} (Snapshot)` });
    }
    state.mcVersionOptions = options;
    return options;
  }

  /* ---------- Modpack-Tab (globale Modpack-Verwaltung) ---------- */
  state.mpTimer = null;
  state.mpOffset = 0;
  state.mpTotal = 0;
  state.mpSource = "modrinth";
  state.mpFilterVersion = "";
  state.mpFilterLoader = "";
  state.mpFiltersLoaded = false;
  state.mpVersions = null;

  async function loadMpFilters() {
    if (state.mpFiltersLoaded) return;
    state.mpFiltersLoaded = true;
    try {
      const options = await fetchMcVersionOptions();
      fillSelect($("#mp-filter-version"), options, "Alle");
      $("#mp-filter-version").value = state.mpFilterVersion;
    } catch (e) {
      fillSelect($("#mp-filter-version"), [], "Alle (Katalog nicht erreichbar)");
    }
  }

  $("#mp-filter-version").addEventListener("change", (e) => {
    state.mpFilterVersion = e.target.value;
    mpSearch(0);
  });
  $("#mp-filter-loader").addEventListener("change", (e) => {
    state.mpFilterLoader = e.target.value;
    mpSearch(0);
  });

  $("#mp-search-input").addEventListener("input", () => {
    clearTimeout(state.mpTimer);
    state.mpTimer = setTimeout(() => mpSearch(0), 400); // Debounce
  });

  async function mpSearch(offset) {
    const target = mpTarget();
    state.mpOffset = offset;
    show($("#mp-loading"), true);
    show($("#mp-error"), false);
    try {
      const params = new URLSearchParams({
        q: $("#mp-search-input").value.trim(),
        offset: String(offset),
        source: state.mpSource,
      });
      // Mit Ziel: Suche passend zur Instanz; ohne Ziel: globale Suche
      // (Treffer können direkt als neuer Server erstellt werden)
      if (!target) {
        if (state.mpFilterVersion) params.set("game_version", state.mpFilterVersion);
        if (state.mpFilterLoader) params.set("loader", state.mpFilterLoader);
      }
      const data = target
        ? await api(`/api/instances/${target.instanceId}/modpacks/search?${params}`)
        : await api(`/api/modpacks/search?${params}`);
      state.mpTotal = data.total;
      renderPacks(data, "#mp-results", mpInstall);
    } catch (e) {
      const hint = state.mpSource === "curseforge" ? cfNoKeyHint(e) : null;
      setText($("#mp-error"), hint || `Modpack-Suche fehlgeschlagen: ${e.message}`);
      show($("#mp-error"), true);
      $("#mp-results").textContent = "";
    } finally {
      show($("#mp-loading"), false);
    }
  }

  function mpInstall(hit, btn) {
    const target = mpTarget();
    if (!target) {
      toast("Keine Ziel-Instanz ausgewählt.", "error");
      return;
    }
    return installPackAt(target.instanceId, hit, btn, () => mpSearch(state.mpOffset), {
      box: $("#mp-progress"),
      bar: $("#mp-bar"),
      phase: $("#mp-phase"),
      pct: $("#mp-pct"),
    });
  }

  /* ---------- Server direkt aus Modpack erstellen (ohne bestehende Instanz) ---------- */
  state.mpCreate = null;

  function packVersionLabel(v, source) {
    const gvs = v.game_versions || [];
    const gv = gvs.length ? gvs.slice(0, 2).join(", ") + (gvs.length > 2 ? ` +${gvs.length - 2}` : "") : "MC ?";
    const ld = (v.loaders || []).join(", ") || "?";
    const date = v.date ? new Date(v.date).toLocaleDateString("de-DE") : "";
    let extra = "";
    if (source === "curseforge") {
      if (v.release && v.release !== 1) extra += ` · ${v.release_label || "Vorab"}`;
      if (v.server_pack) extra += " · Server-Pack";
    } else if (v.featured) {
      extra += " · empfohlen";
    }
    return `${v.name} · ${gv} (${ld})${date ? ` · ${date}` : ""}${extra}`;
  }

  function loadMpVersions(hit) {
    const source = hit._source || state.mpSource;
    const vSel = $("#mp-create-version");
    fillSelect(vSel, [], "Neueste (Standard)");
    vSel.disabled = true;
    state.mpVersions = null;
    api(`/api/modpacks/versions?project_id=${encodeURIComponent(hit.project_id)}&source=${source}`)
      .then((data) => {
        // Dialog inzwischen gewechselt/geschlossen? Dann nichts mehr setzen.
        if (!state.mpCreate || state.mpCreate.mode !== "pack"
            || !state.mpCreate.hit || state.mpCreate.hit.project_id !== hit.project_id) return;
        state.mpVersions = data;
        const opts = data.versions.map((v) => ({
          value: data.source === "curseforge" ? v.file_id : v.version_id,
          label: packVersionLabel(v, data.source),
        }));
        fillSelect(vSel, opts, "Neueste (Standard)");
        vSel.disabled = false;
      })
      .catch((e) => {
        if (!state.mpCreate || state.mpCreate.mode !== "pack"
            || !state.mpCreate.hit || state.mpCreate.hit.project_id !== hit.project_id) return;
        setText($("#mp-create-info"),
          `Modpack-Versionen konnten nicht geladen werden (${e.message}) — „Neueste“ wird installiert.`);
      });
  }

  function openMpCreate(cfg) {
    // cfg: {mode: "pack", hit} oder {mode: "upload", file}
    activateTab("modpacks");
    state.mpCreate = cfg;
    show($("#mp-create-error"), false);
    show($("#mp-create"), true);
    const info = $("#mp-create-info");
    info.textContent = "";
    const vSel = $("#mp-create-version");
    fillSelect(vSel, [], "Neueste (Standard)");
    vSel.disabled = true;
    if (cfg.mode === "upload") {
      setText($("#mp-create-title"), "Server aus hochgeladenem Modpack erstellen");
      setText($("#mp-create-name"), cfg.file.name.replace(/\.(mrpack|zip)$/i, ""));
      setText(info, `Datei: ${cfg.file.name} · Loader/MC-Version werden aus dem Archiv gelesen`);
    } else {
      const hit = cfg.hit;
      setText($("#mp-create-title"), "Server aus Modpack erstellen");
      setText($("#mp-create-name"), hit.title || hit.slug || "");
      const parts = [];
      if (hit.loaders?.length) parts.push(`Loader: ${hit.loaders.join(", ")}`);
      if (hit.versions?.length) parts.push(`MC: ${hit.versions.slice(-3).join(", ")}`);
      setText(info, `Modpack: "${hit.title || hit.slug}"` +
        (parts.length ? ` · ${parts.join(" · ")}` : "") +
        " · Version und Loader werden automatisch aus dem Modpack übernommen");
      loadMpVersions(hit);
    }
    $("#mp-create").scrollIntoView({ behavior: "smooth", block: "nearest" });
    $("#mp-create-name").focus();
  }

  function closeMpCreate() {
    state.mpCreate = null;
    state.mpVersions = null;
    show($("#mp-create"), false);
  }
  $("#mp-create-cancel").addEventListener("click", closeMpCreate);

  $("#mp-create-btn").addEventListener("click", async () => {
    const cfg = state.mpCreate;
    if (!cfg) return;
    const msgBox = $("#mp-create-error");
    const btn = $("#mp-create-btn");
    const progressEls = {
      box: $("#mp-progress"),
      bar: $("#mp-bar"),
      phase: $("#mp-phase"),
      pct: $("#mp-pct"),
    };
    show(msgBox, false);
    if (!$("#mp-create-eula").checked) {
      setText(msgBox, "Bitte die Minecraft-EULA akzeptieren.");
      show(msgBox, true);
      return;
    }
    if (cfg.mode === "upload") {
      const problem = await packFileProblem(cfg.file);
      if (problem) {
        setText(msgBox, problem);
        show(msgBox, true);
        return;
      }
    }
    const name = $("#mp-create-name").value.trim() || null;
    const memory = $("#mp-create-memory").value || null;
    btn.disabled = true;
    btn.textContent = "Erstelle…";
    let created = null;
    let jobId = null;
    try {
      if (cfg.mode === "upload") {
        const form = new FormData();
        form.append("file", cfg.file);
        if (name) form.append("name", name);
        if (memory) form.append("memory", memory);
        form.append("accept_eula", "true");
        const result = await apiUpload("/api/instances/from-pack-upload", form,
          (pct) => { btn.textContent = `Lade hoch… ${pct} %`; });
        created = result.instance;
        jobId = result.job.id;
        $("#mp-upload-file").value = "";
        $("#mp-upload-btn").disabled = true;
      } else {
        const body = {
          project_id: cfg.hit.project_id,
          source: cfg.hit._source || state.mpSource,
          memory,
          accept_eula: true,
        };
        const chosen = $("#mp-create-version").value;
        if (chosen) {
          if (body.source === "curseforge") body.file_id = chosen;
          else body.version_id = chosen;
        }
        if (name) body.name = name;
        const result = await api("/api/instances/from-pack", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        created = result.instance;
        jobId = result.job.id;
      }
      toast(`Server "${created.name}" erstellt (Port ${created.port}) — Installation läuft.`, "success");
      closeMpCreate();
      await pollProgress({ job_id: jobId }, progressEls);
      toast(`Modpack installiert — Server "${created.name}" ist bereit zum Starten.`, "success");
      activateTab("servers");
      loadInstances();
    } catch (e) {
      if (created) {
        // Server existiert, aber Installation fehlgeschlagen
        toast(`Installation fehlgeschlagen: ${e.message} — Server "${created.name}" kann verwaltet oder gelöscht werden.`, "error");
        activateTab("servers");
        loadInstances();
      } else {
        setText(msgBox, `Erstellung fehlgeschlagen: ${e.message}`);
        show(msgBox, true);
      }
    } finally {
      btn.disabled = false;
      btn.textContent = "Server erstellen & installieren";
    }
  });

  $("#mp-upload-file").addEventListener("change", () => {
    $("#mp-upload-btn").disabled = !$("#mp-upload-file").files.length;
    show($("#mp-upload-error"), false);
  });
  $("#mp-upload-btn").addEventListener("click", () => {
    const file = $("#mp-upload-file").files[0];
    if (!file) return;
    const target = mpTarget();
    if (target) {
      uploadPackCore(target.instanceId, file, {
        btn: $("#mp-upload-btn"),
        errorBox: $("#mp-upload-error"),
        fileInput: $("#mp-upload-file"),
        poll: (job) => pollProgress(job, {
          box: $("#mp-progress"),
          bar: $("#mp-bar"),
          phase: $("#mp-phase"),
          pct: $("#mp-pct"),
        }),
        onSuccess: () => mpSearch(state.mpOffset),
      });
    } else {
      // Keine Instanz vorhanden: direkt einen Server aus der Datei erstellen
      openMpCreate({ mode: "upload", file });
    }
  });
  /* ---------- Backups je Instanz ---------- */
  async function loadBackups() {
    if (!state.detailId) return;
    show($("#backup-error"), false);
    const list = $("#backup-list");
    try {
      const data = await api(`/api/instances/${state.detailId}/backups`);
      list.textContent = "";
      show($("#backup-empty"), data.backups.length === 0);
      for (const b of data.backups) {
        const row = document.createElement("li");
        row.className = "mod-row";
        const info = document.createElement("div");
        info.className = "mod-info";
        const nm = document.createElement("span");
        nm.className = "mod-name";
        nm.textContent = b.name;
        const meta = document.createElement("span");
        meta.className = "mod-meta muted small";
        meta.textContent = `${fmtBytes(b.size_bytes)} · ${fmtDate(b.created)}`;
        info.append(nm, meta);
        const btns = document.createElement("div");
        btns.className = "row";
        const dl = document.createElement("button");
        dl.className = "btn";
        dl.textContent = "Download";
        dl.addEventListener("click", () => downloadBackup(b.name, dl));
        const rb = document.createElement("button");
        rb.className = "btn";
        rb.textContent = "Wiederherstellen";
        rb.addEventListener("click", () => restoreBackup(b.name));
        const del = document.createElement("button");
        del.className = "btn danger";
        del.textContent = "Löschen";
        del.addEventListener("click", () => deleteBackup(b.name));
        btns.append(dl, rb, del);
        row.append(info, btns);
        list.appendChild(row);
      }
    } catch (e) {
      show($("#backup-empty"), false);
      setText($("#backup-error"), `Backups nicht abrufbar: ${e.message}`);
      show($("#backup-error"), true);
    }
  }

  function backupAction(name, method, confirmMsg, doneMsg) {
    return async () => {
      if (confirmMsg && !window.confirm(confirmMsg)) return;
      try {
        await api(`/api/instances/${state.detailId}/backups/${encodeURIComponent(name)}${method.suffix}`,
          { method: method.type });
        toast(doneMsg, "success");
        loadBackups();
      } catch (e) {
        toast(`${method.label} fehlgeschlagen: ${e.message}`, "error");
      }
    };
  }

  function deleteBackup(name) {
    return backupAction(name, { type: "DELETE", suffix: "", label: "Löschen" },
      `"${name}" wirklich löschen?`, `"${name}" gelöscht.`)();
  }

  async function restoreBackup(name) {
    if (!window.confirm(
      `Wiederherstellen von "${name}"?\n\nDie Instanz wird gestoppt und ALLE aktuellen\nDateien (Welt, Configs, Mods) werden durch den\nSnapshot ersetzt! Weiter?`)) return;
    try {
      await api(`/api/instances/${state.detailId}/backups/${encodeURIComponent(name)}/restore`,
        { method: "POST" });
      toast(`"${name}" wiederhergestellt — Instanz gestoppt, zum Starten "Starten" drücken.`, "success");
      loadDetail();
      loadBackups();
    } catch (e) {
      toast(`Wiederherstellen fehlgeschlagen: ${e.message}`, "error");
    }
  }

  async function downloadBackup(name, btn) {
    btn.disabled = true;
    try {
      const res = await fetch(
        `/api/instances/${state.detailId}/backups/${encodeURIComponent(name)}/download`,
        { headers: state.apiKey ? { "X-API-Key": state.apiKey } : {} });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = name;
      a.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      toast(`Download fehlgeschlagen: ${e.message}`, "error");
    } finally {
      btn.disabled = false;
    }
  }

  $("#backup-create-btn").addEventListener("click", async () => {
    const btn = $("#backup-create-btn");
    btn.disabled = true;
    btn.textContent = "Erstelle…";
    try {
      const result = await api(`/api/instances/${state.detailId}/backups`, { method: "POST" });
      toast(`Backup "${result.name}" erstellt (${fmtBytes(result.size_bytes)}).`, "success");
      loadBackups();
    } catch (e) {
      toast(`Backup fehlgeschlagen: ${e.message}`, "error");
    } finally {
      btn.disabled = false;
      btn.textContent = "Backup erstellen";
    }
  });
  $("#backup-reload-btn").addEventListener("click", loadBackups);

  /* ---------- Welt je Instanz (Info, Download, Upload) ---------- */
  async function loadWorldInfo() {
    if (!state.detailId) return;
    try {
      const info = await api(`/api/instances/${state.detailId}/world`);
      setText($("#world-info"), info.exists
        ? `Welt "${info.world_dir}" · ${fmtBytes(info.size_bytes)}`
        : "Keine Welt gefunden (Server einmal starten oder Welt hochladen).");
      $("#world-download-btn").disabled = !info.exists;
      $("#world-upload-btn").disabled = !$("#world-upload-file").files.length
        || state.detailRunning;
      if (state.detailRunning) {
        setText($("#world-info"),
          `${$("#world-info").textContent} — Upload nur bei gestoppter Instanz`);
      }
    } catch (e) {
      setText($("#world-info"), "–");
      setText($("#world-error"), `Welt-Info nicht abrufbar: ${e.message}`);
      show($("#world-error"), true);
    }
  }
  $("#world-reload").addEventListener("click", loadWorldInfo);

  async function downloadWorld(btn) {
    btn.disabled = true;
    try {
      const res = await fetch(
        `/api/instances/${state.detailId}/world/download`,
        { headers: state.apiKey ? { "X-API-Key": state.apiKey } : {} });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const blob = await res.blob();
      const cd = res.headers.get("content-disposition") || "";
      const match = cd.match(/filename="?([^";]+)"?/);
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = match ? match[1] : `welt-${state.detailId}.zip`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      toast(`Welt-Download fehlgeschlagen: ${e.message}`, "error");
    } finally {
      btn.disabled = false;
      loadWorldInfo();
    }
  }
  $("#world-download-btn").addEventListener("click", (e) => downloadWorld(e.target));

  $("#world-upload-file").addEventListener("change", () => {
    $("#world-upload-btn").disabled = !$("#world-upload-file").files.length
      || state.detailRunning;
    show($("#world-error"), false);
  });
  $("#world-upload-btn").addEventListener("click", async () => {
    const file = $("#world-upload-file").files[0];
    if (!file || !state.detailId) return;
    if (!window.confirm(
      `Welt wirklich ersetzen?\n\nDer aktuelle Welt-Ordner der Instanz wird\ndurch "${file.name}" ersetzt!\nWeiter?`)) return;
    const form = new FormData();
    form.append("file", file);
    const btn = $("#world-upload-btn");
    btn.disabled = true;
    btn.textContent = "Lade hoch…";
    show($("#world-error"), false);
    try {
      const result = await api(`/api/instances/${state.detailId}/world/upload`,
        { method: "POST", body: form });
      toast(`Welt "${result.world_dir}" ersetzt.`, "success");
      $("#world-upload-file").value = "";
      loadWorldInfo();
    } catch (e) {
      setText($("#world-error"), `Welt-Upload fehlgeschlagen: ${e.message}`);
      show($("#world-error"), true);
    } finally {
      btn.disabled = false;
      btn.textContent = "Welt hochladen";
    }
  });

  /* ---------- Datei-Browser je Instanz ---------- */
  state.fb = { path: "", entries: [] };

  function fbSetError(msg) { authError("#fb-error", msg); }

  function fbJoin(dir, name) {
    return dir ? `${dir}/${name}` : name;
  }

  function fbParent(rel) {
    const idx = rel.lastIndexOf("/");
    return idx >= 0 ? rel.slice(0, idx) : "";
  }

  function fbBaseName(rel) {
    return rel.split("/").pop() || rel;
  }

  async function fbReload() {
    if (!state.detailId) return;
    fbSetError("");
    try {
      const data = await api(`/api/instances/${state.detailId}/files`
        + `?path=${encodeURIComponent(state.fb.path)}`);
      state.fb.entries = data.entries || [];
      renderFbList();
    } catch (e) {
      state.fb.entries = [];
      renderFbList();
      fbSetError(`Ordner nicht lesbar: ${e.message}`);
    }
  }

  function renderFbBreadcrumb() {
    const nav = $("#fb-breadcrumb");
    nav.textContent = "";
    const root = document.createElement("button");
    root.className = "fb-crumb";
    root.textContent = "Instanz";
    root.addEventListener("click", () => {
      state.fb.path = "";
      fbReload();
    });
    nav.appendChild(root);
    let acc = "";
    for (const part of state.fb.path.split("/").filter(Boolean)) {
      acc = fbJoin(acc, part);
      const sep = document.createElement("span");
      sep.className = "fb-crumb-sep";
      sep.textContent = "›";
      const crumb = document.createElement("button");
      crumb.className = "fb-crumb";
      crumb.textContent = part;
      const target = acc;
      crumb.addEventListener("click", () => {
        state.fb.path = target;
        fbReload();
      });
      nav.append(sep, crumb);
    }
  }

  function renderFbList() {
    renderFbBreadcrumb();
    const list = $("#fb-list");
    list.textContent = "";
    const entries = state.fb.entries || [];
    show($("#fb-empty"), entries.length === 0);
    for (const entry of entries) {
      const li = document.createElement("li");
      li.className = `mod-row${entry.managed ? " fb-managed" : ""}`;
      const info = document.createElement("div");
      info.className = "mod-info";
      const icon = document.createElement("span");
      icon.className = "fb-icon";
      icon.textContent = entry.type === "dir" ? "📁" : "📄";
      const name = document.createElement("span");
      name.className = "mod-name";
      name.textContent = entry.name;
      if (entry.type === "dir" && !entry.managed) {
        name.classList.add("fb-dir-link");
        name.title = "Ordner öffnen";
        const target = fbJoin(state.fb.path, entry.name);
        name.addEventListener("click", () => {
          state.fb.path = target;
          fbReload();
        });
      }
      const meta = document.createElement("span");
      meta.className = "mod-meta muted small";
      meta.textContent = entry.type === "dir"
        ? "Ordner"
        : [fmtBytes(entry.size), fmtDate(entry.mtime)].filter(Boolean).join(" · ");
      info.append(icon, name, meta);

      const actions = document.createElement("div");
      actions.className = "row";
      const relPath = fbJoin(state.fb.path, entry.name);
      if (entry.type === "file") {
        const dl = document.createElement("button");
        dl.className = "btn small-btn";
        dl.textContent = "Download";
        dl.addEventListener("click", () => fbDownload(relPath, dl));
        actions.appendChild(dl);
        if (!entry.managed) {
          const edit = document.createElement("button");
          edit.className = "btn small-btn admin-only";
          edit.textContent = "Bearbeiten";
          edit.addEventListener("click", () => fbOpenEditor(relPath));
          actions.appendChild(edit);
        }
      }
      if (!entry.managed) {
        const ren = document.createElement("button");
        ren.className = "btn small-btn admin-only";
        ren.textContent = "Umbenennen";
        ren.addEventListener("click", () => {
          const next = window.prompt("Neuer Name/Pfad:", relPath);
          if (!next || next.trim() === relPath) return;
          fbRename(relPath, next.trim());
        });
        const del = document.createElement("button");
        del.className = "btn danger small-btn admin-only";
        del.textContent = "Löschen";
        del.addEventListener("click", () => {
          if (!window.confirm(`"${relPath}" löschen?`)) return;
          fbDelete(relPath);
        });
        actions.append(ren, del);
      }
      if (!actions.childElementCount) {
        const hint = document.createElement("span");
        hint.className = "muted small";
        hint.textContent = "geschützt";
        actions.appendChild(hint);
      }
      li.append(info, actions);
      list.appendChild(li);
    }
  }

  async function fbDownload(relPath, btn) {
    btn.disabled = true;
    try {
      const res = await fetch(
        `/api/instances/${state.detailId}/files/download`
        + `?path=${encodeURIComponent(relPath)}`,
        { headers: state.apiKey ? { "X-API-Key": state.apiKey } : {} });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        throw new Error(data.detail || `HTTP ${res.status}`);
      }
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = fbBaseName(relPath);
      a.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      toast(`Download fehlgeschlagen: ${e.message}`, "error");
    } finally {
      btn.disabled = false;
    }
  }

  async function fbOpenEditor(relPath) {
    $("#fb-editor-title").textContent = relPath;
    $("#fb-editor-text").value = "";
    fbEditorError("");
    $("#fb-editor").dataset.path = relPath;
    $("#fb-editor").showModal();
    const btn = $("#fb-editor-save");
    btn.disabled = true;
    try {
      const data = await api(`/api/instances/${state.detailId}/files/content`
        + `?path=${encodeURIComponent(relPath)}`);
      $("#fb-editor-text").value = data.content ?? "";
      setText($("#fb-editor-meta"),
        `${fmtBytes(data.size)} · UTF-8 · max. 1 MiB`);
    } catch (e) {
      fbEditorError(e.message);
      closeDialog($("#fb-editor"));
    } finally {
      btn.disabled = false;
    }
  }

  function fbEditorError(msg) { authError("#fb-editor-error", msg); }

  $("#fb-editor-save").addEventListener("click", async () => {
    const dlg = $("#fb-editor");
    const relPath = dlg.dataset.path;
    if (!relPath) return;
    const btn = $("#fb-editor-save");
    btn.disabled = true;
    btn.textContent = "Speichere…";
    fbEditorError("");
    try {
      await api(`/api/instances/${state.detailId}/files/content`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: relPath, content: $("#fb-editor-text").value }),
      });
      closeDialog(dlg);
      toast(`"${relPath}" gespeichert.`, "success");
      fbReload();
    } catch (e) {
      fbEditorError(e.message);
    } finally {
      btn.disabled = false;
      btn.textContent = "Speichern";
    }
  });
  $("#fb-editor-close").addEventListener("click", () => closeDialog($("#fb-editor")));

  async function fbRename(fromPath, toPath) {
    try {
      await api(`/api/instances/${state.detailId}/files/rename`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ from_path: fromPath, to_path: toPath }),
      });
      toast(`"${fromPath}" → "${toPath}".`, "success");
      fbReload();
    } catch (e) {
      toast(`Umbenennen fehlgeschlagen: ${e.message}`, "error");
    }
  }

  async function fbDelete(relPath) {
    try {
      await api(`/api/instances/${state.detailId}/files`
        + `?path=${encodeURIComponent(relPath)}`, { method: "DELETE" });
      toast(`"${relPath}" gelöscht.`, "success");
      fbReload();
    } catch (e) {
      toast(`Löschen fehlgeschlagen: ${e.message}`, "error");
    }
  }

  $("#fb-reload").addEventListener("click", fbReload);
  $("#fb-mkdir-btn").addEventListener("click", async () => {
    const name = window.prompt("Name des neuen Ordners:", "neuer-ordner");
    if (!name) return;
    try {
      await api(`/api/instances/${state.detailId}/files/mkdir`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path: fbJoin(state.fb.path, name.trim()) }),
      });
      fbReload();
    } catch (e) {
      toast(`Ordner nicht anlegbar: ${e.message}`, "error");
    }
  });
  $("#fb-upload-file").addEventListener("change", () => {
    $("#fb-upload-btn").disabled = !$("#fb-upload-file").files.length;
  });
  $("#fb-upload-btn").addEventListener("click", async () => {
    const files = [...$("#fb-upload-file").files];
    if (!files.length || !state.detailId) return;
    const btn = $("#fb-upload-btn");
    btn.disabled = true;
    btn.textContent = "Lade hoch…";
    try {
      for (const file of files) {
        const form = new FormData();
        form.append("path", fbJoin(state.fb.path, file.name));
        form.append("overwrite", "false");
        form.append("file", file);
        try {
          await api(`/api/instances/${state.detailId}/files/upload`,
            { method: "POST", body: form });
          toast(`"${file.name}" hochgeladen.`, "success");
        } catch (e) {
          // 409 (existiert) mit Hinweis zeigen; Rest weiter versuchen
          if (e.status === 409 && window.confirm(
            `"${file.name}" existiert bereits. Überschreiben?`)) {
            form.set("overwrite", "true");
            try {
              await api(`/api/instances/${state.detailId}/files/upload`,
                { method: "POST", body: form });
              toast(`"${file.name}" überschrieben.`, "success");
            } catch (e2) {
              toast(`Upload fehlgeschlagen: ${e2.message}`, "error");
            }
          } else {
            toast(`Upload "${file.name}" fehlgeschlagen: ${e.message}`, "error");
          }
        }
      }
      $("#fb-upload-file").value = "";
      $("#fb-upload-btn").disabled = true;
      fbReload();
    } finally {
      btn.disabled = false;
      btn.textContent = "Hochladen";
    }
  });

  /* ---------- Datapacks je Instanz ---------- */
  function dpSetError(msg) { authError("#dp-error", msg); }

  async function dpReload() {
    if (!state.detailId) return;
    dpSetError("");
    $("#dp-upload-btn").disabled = !$("#dp-upload-file").files.length
      || state.detailRunning;
    try {
      const data = await api(`/api/instances/${state.detailId}/datapacks`);
      renderDatapacks(data.datapacks || []);
    } catch (e) {
      renderDatapacks([]);
      dpSetError(`Datapacks nicht ladbar: ${e.message}`);
    }
  }

  function renderDatapacks(packs) {
    const list = $("#dp-list");
    list.textContent = "";
    show($("#dp-empty"), packs.length === 0);
    for (const pack of packs) {
      const li = document.createElement("li");
      li.className = "mod-row";
      const info = document.createElement("div");
      info.className = "mod-info";
      const name = document.createElement("span");
      name.className = "mod-name";
      name.textContent = pack.name;
      const meta = document.createElement("span");
      meta.className = "mod-meta muted small";
      meta.textContent = [fmtBytes(pack.size), fmtDate(pack.mtime),
        pack.enabled ? "aktiv" : "deaktiviert"].filter(Boolean).join(" · ");
      info.append(name, meta);
      const actions = document.createElement("div");
      actions.className = "row";
      const toggle = document.createElement("button");
      toggle.className = "btn small-btn admin-only";
      toggle.textContent = pack.enabled ? "Deaktivieren" : "Aktivieren";
      toggle.addEventListener("click", () => {
        dpToggle(pack.name, pack.enabled ? "disable" : "enable");
      });
      const del = document.createElement("button");
      del.className = "btn danger small-btn admin-only";
      del.textContent = "Löschen";
      del.addEventListener("click", () => {
        if (!window.confirm(`Datapack "${pack.name}" löschen?`)) return;
        dpDelete(pack.name, pack.enabled);
      });
      actions.append(toggle, del);
      li.append(info, actions);
      list.appendChild(li);
    }
  }

  async function dpToggle(name, action) {
    try {
      await api(`/api/instances/${state.detailId}/datapacks/${action}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name }),
      });
      toast(`Datapack "${name}" ${action === "enable" ? "aktiviert" : "deaktiviert"}.`,
        "success");
    } catch (e) {
      toast(`${action === "enable" ? "Aktivieren" : "Deaktivieren"} fehlgeschlagen: `
        + e.message, "error");
    }
    dpReload();
  }

  async function dpDelete(name, enabled) {
    try {
      await api(`/api/instances/${state.detailId}/datapacks/`
        + `${encodeURIComponent(name)}?enabled=${enabled}`, { method: "DELETE" });
      toast(`Datapack "${name}" gelöscht.`, "success");
    } catch (e) {
      toast(`Löschen fehlgeschlagen: ${e.message}`, "error");
    }
    dpReload();
  }

  $("#dp-reload").addEventListener("click", dpReload);
  $("#dp-upload-file").addEventListener("change", () => {
    $("#dp-upload-btn").disabled = !$("#dp-upload-file").files.length
      || state.detailRunning;
  });
  $("#dp-upload-btn").addEventListener("click", async () => {
    const file = $("#dp-upload-file").files[0];
    if (!file || !state.detailId) return;
    const btn = $("#dp-upload-btn");
    btn.disabled = true;
    btn.textContent = "Lade hoch…";
    dpSetError("");
    const form = new FormData();
    form.append("file", file);
    try {
      const result = await api(`/api/instances/${state.detailId}/datapacks/upload`,
        { method: "POST", body: form });
      toast(`Datapack "${result.name}" hochgeladen.`, "success");
      $("#dp-upload-file").value = "";
    } catch (e) {
      dpSetError(`Upload fehlgeschlagen: ${e.message}`);
    } finally {
      btn.disabled = false;
      btn.textContent = "Hochladen";
    }
    dpReload();
  });

  /* ---------- JVM-Optionen + RAM je Instanz ---------- */
  function setRamFields(memory) {
    const match = /^(\d{1,4})([GgMm])$/.exec(memory || "2G");
    $("#jvm-memory-value").value = match ? parseInt(match[1], 10) : 2;
    $("#jvm-memory-unit").value = match ? match[2].toUpperCase() : "G";
  }

  $("#jvm-save").addEventListener("click", async () => {
    const btn = $("#jvm-save");
    btn.disabled = true;
    btn.textContent = "Speichere…";
    show($("#jvm-error"), false);
    const value = $("#jvm-memory-value").value;
    const unit = $("#jvm-memory-unit").value;
    const memory = value ? `${parseInt(value, 10)}${unit}` : null;
    const body = {
      jvm_opts: $("#jvm-opts").value || "",
      use_aikar: $("#jvm-aikar").checked,
    };
    if (memory && memory !== state.detailMemory) body.memory = memory;
    try {
      await api(`/api/instances/${state.detailId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (body.memory) {
        state.detailMemory = body.memory;
        toast("JVM & RAM gespeichert — RAM wirksam beim nächsten (Neu-)Start.", "success");
      } else {
        toast("JVM-Optionen gespeichert — wirksam beim nächsten (Neu-)Start.", "success");
      }
    } catch (e) {
      setText($("#jvm-error"), `Speichern fehlgeschlagen: ${e.message}`);
      show($("#jvm-error"), true);
    } finally {
      btn.disabled = false;
      btn.textContent = "Speichern";
    }
  });

  /* ---------- Zeitplan je Instanz (Scheduler) ---------- */
  function schedInt(value, lo, hi, fallback) {
    const num = parseInt(value, 10);
    if (Number.isNaN(num)) return fallback;
    return Math.min(hi, Math.max(lo, num));
  }

  function loadSchedule(detail) {
    const sched = detail.schedule || {};
    $("#sched-autostart").checked = !!sched.auto_start;
    const rs = sched.restart || {};
    $("#sched-restart-enabled").checked = !!rs.enabled;
    $("#sched-restart-time").value = rs.time || "04:00";
    $("#sched-restart-warn").value = rs.warn_minutes ?? 5;
    const bs = sched.backup || {};
    $("#sched-backup-enabled").checked = !!bs.enabled;
    $("#sched-backup-hours").value = bs.interval_hours ?? 6;
    $("#sched-backup-keep").value = bs.keep ?? 5;
    const us = sched.update_check || {};
    $("#sched-update-enabled").checked = !!us.enabled;
    $("#sched-update-hours").value = us.interval_hours ?? 24;
  }

  $("#sched-save").addEventListener("click", async () => {
    const btn = $("#sched-save");
    btn.disabled = true;
    btn.textContent = "Speichere…";
    show($("#sched-error"), false);
    const schedule = {
      auto_start: $("#sched-autostart").checked,
      restart: {
        enabled: $("#sched-restart-enabled").checked,
        time: $("#sched-restart-time").value || "04:00",
        warn_minutes: schedInt($("#sched-restart-warn").value, 0, 30, 5),
      },
      backup: {
        enabled: $("#sched-backup-enabled").checked,
        interval_hours: schedInt($("#sched-backup-hours").value, 1, 168, 6),
        keep: schedInt($("#sched-backup-keep").value, 1, 20, 5),
      },
      update_check: {
        enabled: $("#sched-update-enabled").checked,
        interval_hours: schedInt($("#sched-update-hours").value, 1, 168, 24),
      },
    };
    try {
      await api(`/api/instances/${state.detailId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ schedule }),
      });
      toast("Zeitplan gespeichert.", "success");
    } catch (e) {
      setText($("#sched-error"), `Speichern fehlgeschlagen: ${e.message}`);
      show($("#sched-error"), true);
    } finally {
      btn.disabled = false;
      btn.textContent = "Speichern";
    }
  });

  /* ---------- Spieler-Verwaltung (RCON: OP/Kick/Ban) ---------- */
  function setRconOutput(text) {
    const box = $("#rcon-output");
    if (text) {
      setText(box, text);
      show(box, true);
    } else {
      show(box, false);
    }
  }

  async function loadRconPlayers() {
    show($("#rcon-error"), false);
    const hint = $("#rcon-hint");
    try {
      const data = await api(`/api/instances/${state.detailId}/players`);
      const list = $("#rcon-players");
      list.textContent = "";
      const names = data.names || [];
      setText(hint, data.online != null
        ? `${data.online} von ${data.max} Slots belegt` : `Antwort: ${data.raw || "?"}`);
      show($("#rcon-players-empty"), names.length === 0);
      for (const name of names) {
        const node = document.createElement("li");
        node.className = "mod-row";
        const info = document.createElement("div");
        info.className = "mod-info";
        const nm = document.createElement("span");
        nm.className = "mod-name";
        nm.textContent = name;
        nm.title = name;
        info.appendChild(nm);
        const btns = document.createElement("div");
        btns.className = "row";
        const mkBtn = (label, action, cls = "", title = "") => {
          const b = document.createElement("button");
          b.className = `btn${cls ? ` ${cls}` : ""}`.trim();
          b.textContent = label;
          if (title) b.title = title;
          b.addEventListener("click", (e) => rconAction(action, name, e.target));
          btns.appendChild(b);
        };
        mkBtn("OP", "op", "", "Operator-Rechte geben");
        mkBtn("Kick", "kick", "", "Spieler kicken");
        mkBtn("Ban", "ban", "danger", "Spieler bannen");
        node.append(info, btns);
        list.appendChild(node);
      }
    } catch (e) {
      list.textContent = "";
      show($("#rcon-players-empty"), true);
      setText(hint, /läuft nicht/.test(e.message)
        ? "Server läuft nicht — zum Verwalten der Spieler zuerst starten."
        : "Spieler nicht abrufbar.");
      setText($("#rcon-error"), e.message);
      show($("#rcon-error"), true);
    }
  }

  async function rconAction(action, target, btn) {
    show($("#rcon-error"), false);
    if (btn) btn.disabled = true;
    try {
      const data = await api(`/api/instances/${state.detailId}/rcon`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action, target: target || null, reason: null }),
      });
      setRconOutput(`${data.command} → ${data.output || "(keine Antwort)"}`);
      toast(`Ausgeführt: ${data.command}`, "success");
      if (action !== "banlist") loadRconPlayers();
    } catch (e) {
      setText($("#rcon-error"), `${action} fehlgeschlagen: ${e.message}`);
      show($("#rcon-error"), true);
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  $("#rcon-reload").addEventListener("click", loadRconPlayers);
  for (const [sel, action] of [
    ["#rcon-op", "op"], ["#rcon-deop", "deop"], ["#rcon-kick", "kick"],
    ["#rcon-ban", "ban"], ["#rcon-pardon", "pardon"],
  ]) {
    $(sel).addEventListener("click", (e) => {
      const target = $("#rcon-target").value.trim();
      if (!target) {
        toast("Bitte zuerst einen Spielernamen eingeben.", "error");
        return;
      }
      if (action === "ban" && !window.confirm(`"${target}" wirklich bannen?`)) return;
      if (action === "kick" && !window.confirm(`"${target}" kicken?`)) return;
      rconAction(action, target, e.target);
    });
  }
  $("#rcon-banlist-btn").addEventListener("click", (e) => rconAction("banlist", null, e.target));

  /* ---------- Freie RCON-Konsole ---------- */
  function appendConsoleLine(text) {
    const log = $("#console-log");
    show(log, true);
    log.textContent += (log.textContent ? "\n" : "") + text;
    log.scrollTop = log.scrollHeight;
  }

  async function sendConsoleCommand() {
    if (!state.detailId) return;
    const input = $("#console-input");
    const cmd = input.value.trim().replace(/^\/+/, "");
    if (!cmd) return;
    show($("#console-error"), false);
    input.value = "";
    // Verlauf (neueste zuerst, ohne direkte Duplikate)
    if (state.consoleHistory[0] !== cmd) state.consoleHistory.unshift(cmd);
    state.consoleHistoryPos = -1;
    if (state.consoleHistory.length > 50) state.consoleHistory.pop();
    appendConsoleLine(`> ${cmd}`);
    try {
      const data = await api(`/api/instances/${state.detailId}/console`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ command: cmd }),
      });
      appendConsoleLine(data.output || "(keine Antwort)");
    } catch (e) {
      appendConsoleLine(`Fehler: ${e.message}`);
    }
  }

  $("#console-send").addEventListener("click", sendConsoleCommand);
  $("#console-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      sendConsoleCommand();
      return;
    }
    // Befehlsverlauf mit Pfeiltasten
    const history = state.consoleHistory;
    if (e.key === "ArrowUp" && history.length) {
      e.preventDefault();
      state.consoleHistoryPos = Math.min(state.consoleHistoryPos + 1, history.length - 1);
      $("#console-input").value = history[state.consoleHistoryPos];
    } else if (e.key === "ArrowDown" && state.consoleHistoryPos >= 0) {
      e.preventDefault();
      state.consoleHistoryPos -= 1;
      $("#console-input").value = state.consoleHistoryPos >= 0
        ? history[state.consoleHistoryPos] : "";
    }
  });

  async function loadConsoleMode() {
    try {
      const s = await api("/api/settings");
      setText($("#console-mode"), s.rcon_console_mode === "free"
        ? "(freier Modus)" : "(Whitelist-Modus)");
    } catch (e) { /* Hinweis optional */ }
  }
  loadConsoleMode();

  /* ---------- Gamerule-Quick-Editor ---------- */
  state.grRules = [];

  function grSetError(msg) { authError("#gr-error", msg); }

  async function grReload() {
    if (!state.detailId) return;
    grSetError("");
    show($("#gr-empty"), false);
    try {
      const data = await api(`/api/instances/${state.detailId}/gamerules`);
      state.grRules = data.gamerules || [];
      renderGamerules();
    } catch (e) {
      state.grRules = [];
      renderGamerules();
      grSetError(e.status === 409
        ? "Gamerules sind nur bei laufender Instanz verfügbar — Server starten."
        : `Gamerules nicht ladbar: ${e.message}`);
    }
  }

  function renderGamerules() {
    const list = $("#gr-list");
    list.textContent = "";
    const query = ($("#gr-filter").value || "").trim().toLowerCase();
    const matches = state.grRules.filter((g) => !query
      || g.name.toLowerCase().includes(query)
      || (g.desc || "").toLowerCase().includes(query));
    for (const rule of matches) {
      const li = document.createElement("li");
      li.className = "mod-row";
      const info = document.createElement("div");
      info.className = "mod-info";
      const name = document.createElement("span");
      name.className = "mod-name";
      name.textContent = rule.name;
      name.title = rule.desc || rule.name;
      const meta = document.createElement("span");
      meta.className = "mod-meta muted small";
      meta.textContent = rule.value == null
        ? `Default: ${rule.default}`
        : (rule.value !== rule.default ? "geändert" : "Standard");
      info.append(name, meta);
      const actions = document.createElement("div");
      actions.className = "row";
      if (rule.type === "bool") {
        const seg = document.createElement("div");
        seg.className = "seg";
        for (const option of [true, false]) {
          const btn = document.createElement("button");
          btn.type = "button";
          btn.className = "seg-btn admin-only";
          btn.textContent = option ? "An" : "Aus";
          btn.classList.toggle("active", rule.value === option);
          btn.addEventListener("click", () => grSet(rule.name, option));
          seg.appendChild(btn);
        }
        actions.appendChild(seg);
      } else {
        const input = document.createElement("input");
        input.type = "number";
        input.className = "cfg-input gr-num";
        input.value = rule.value == null ? rule.default : rule.value;
        if (rule.min != null) input.min = String(rule.min);
        if (rule.max != null) input.max = String(rule.max);
        input.dataset.rule = rule.name;
        input.title = `Bereich ${rule.min}–${rule.max} · ${rule.desc || ""}`;
        const apply = document.createElement("button");
        apply.type = "button";
        apply.className = "btn small-btn admin-only";
        apply.textContent = "Setzen";
        apply.addEventListener("click", () => grSet(rule.name, input.value));
        actions.append(input, apply);
      }
      li.append(info, actions);
      list.appendChild(li);
    }
    if (!matches.length && state.grRules.length) {
      const li = document.createElement("li");
      li.className = "muted small";
      li.textContent = "Keine Treffer.";
      list.appendChild(li);
    }
  }

  async function grSet(name, value) {
    try {
      const data = await api(`/api/instances/${state.detailId}/gamerules`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, value }),
      });
      const rule = state.grRules.find((g) => g.name === name);
      if (rule) rule.value = data.value;
      renderGamerules();
      toast(`${name} = ${data.value}`, "success");
    } catch (e) {
      toast(`Gamerule nicht gesetzt: ${e.message}`, "error");
    }
  }

  $("#gr-reload").addEventListener("click", grReload);
  $("#gr-filter").addEventListener("input", renderGamerules);

  /* ---------- Whitelist (whitelist.json) ---------- */
  function wlSetError(msg) {
    const box = $("#wl-error");
    if (msg) {
      setText(box, msg);
      show(box, true);
    } else {
      show(box, false);
    }
  }

  function renderWhitelistStatic() {
    const list = $("#wl-list");
    list.textContent = "";
    const entries = state.wlEntries;
    setText($("#wl-summary"), entries.length
      ? `${entries.length} Eintrag${entries.length === 1 ? "" : "träge"} — Änderungen erst nach „Speichern“.`
      : "");
    show($("#wl-empty"), entries.length === 0);
    for (const name of entries) {
      const node = document.createElement("li");
      node.className = "mod-row";
      const info = document.createElement("div");
      info.className = "mod-info";
      const nm = document.createElement("span");
      nm.className = "mod-name";
      nm.textContent = name;
      nm.title = name;
      info.appendChild(nm);
      const btns = document.createElement("div");
      btns.className = "row";
      const rm = document.createElement("button");
      rm.className = "btn";
      rm.textContent = "Entfernen";
      rm.addEventListener("click", () => {
        state.wlEntries = state.wlEntries.filter(
          (n) => n.toLowerCase() !== name.toLowerCase());
        renderWhitelistStatic();
      });
      btns.appendChild(rm);
      node.append(info, btns);
      list.appendChild(node);
    }
  }

  async function loadWhitelist() {
    wlSetError("");
    try {
      const data = await api(`/api/instances/${state.detailId}/whitelist`);
      state.wlEntries = (data.entries || []).map((e) => e.name);
      state.wlRunning = !!data.running;
      const mode = data.online_mode ? "online-mode" : "offline-mode";
      setText($("#wl-hint"),
        `Direkter Datei-Zugriff (${mode}) — funktioniert auch bei gestoppter Instanz. `
        + "Änderungen wirken beim nächsten Start bzw. per „Auf Server laden“ (RCON).");
      if (data.corrupt) wlSetError("whitelist.json ist beschädigt — beim Speichern wird sie neu aufgebaut.");
      renderWhitelistStatic();
    } catch (e) {
      state.wlEntries = [];
      renderWhitelistStatic();
      wlSetError(`Whitelist nicht ladbar: ${e.message}`);
    }
  }

  function wlAddEntry() {
    const input = $("#wl-input");
    const name = input.value.trim();
    input.value = "";
    if (!/^[A-Za-z0-9_]{1,16}$/.test(name)) {
      wlSetError("Spielername: 1-16 Zeichen, Buchstaben/Zahlen/Unterstrich.");
      return;
    }
    wlSetError("");
    if (state.wlEntries.some((n) => n.toLowerCase() === name.toLowerCase())) {
      wlSetError(`"${name}" steht bereits in der Liste.`);
      return;
    }
    state.wlEntries.push(name);
    renderWhitelistStatic();
  }

  async function saveWhitelist() {
    const btn = $("#wl-save");
    btn.disabled = true;
    btn.textContent = "Speichere…";
    wlSetError("");
    try {
      const data = await api(`/api/instances/${state.detailId}/whitelist`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ entries: state.wlEntries }),
      });
      state.wlEntries = data.entries.map((e) => e.name);
      renderWhitelistStatic();
      if (data.unresolved?.length) {
        wlSetError(`UUID konnte nicht aufgelöst werden: ${data.unresolved.join(", ")} `
          + "— Einträge sind gespeichert, greifen ggf. erst nach UUID-Auflösung durch den Server.");
      } else if (data.reloaded === true) {
        toast(`Whitelist gespeichert und auf dem Server neu geladen (${data.entries.length} Einträge).`, "success");
      } else {
        toast(`Whitelist gespeichert (${data.entries.length} Einträge) — wirksam beim nächsten Start.`, "success");
      }
    } catch (e) {
      wlSetError(`Speichern fehlgeschlagen: ${e.message}`);
    } finally {
      btn.disabled = false;
      btn.textContent = "Speichern";
    }
  }

  async function reloadWhitelistOnServer() {
    const btn = $("#wl-reload-server");
    btn.disabled = true;
    wlSetError("");
    try {
      await api(`/api/instances/${state.detailId}/whitelist/reload`, { method: "POST" });
      toast("Whitelist auf dem Server neu geladen.", "success");
    } catch (e) {
      wlSetError(`Reload fehlgeschlagen: ${e.message}`);
    } finally {
      btn.disabled = false;
    }
  }

  $("#wl-add").addEventListener("click", wlAddEntry);
  $("#wl-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); wlAddEntry(); }
  });
  $("#wl-save").addEventListener("click", saveWhitelist);
  $("#wl-reload-file").addEventListener("click", loadWhitelist);
  $("#wl-reload-server").addEventListener("click", reloadWhitelistOnServer);

  /* ---------- Tags (Gruppen) + Port-Wechsel (Detail-Dialog) ---------- */
  function tagsSetError(msg) {
    const box = $("#tags-error");
    if (msg) {
      setText(box, msg);
      show(box, true);
    } else {
      show(box, false);
    }
  }

  function parseTagsInput(selector) {
    const el = $(selector || "#inst-tags");
    return el.value.split(",").map((t) => t.trim()).filter(Boolean);
  }

  async function saveInstanceTags() {
    if (!state.detailId) return;
    const btn = $("#tags-save");
    btn.disabled = true;
    tagsSetError("");
    try {
      const inst = await api(`/api/instances/${state.detailId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ tags: parseTagsInput("#inst-tags") }),
      });
      $("#inst-tags").value = (inst.tags || []).join(", ");
      toast("Tags gespeichert.", "success");
      refreshOverview();
    } catch (e) {
      tagsSetError(`Tags nicht gespeichert: ${e.message}`);
    } finally {
      btn.disabled = false;
    }
  }

  async function saveInstancePort() {
    if (!state.detailId) return;
    const btn = $("#port-save");
    const input = $("#inst-port-new");
    const value = parseInt(input.value, 10);
    if (!Number.isFinite(value)) {
      tagsSetError("Bitte einen gültigen Port angeben.");
      return;
    }
    btn.disabled = true;
    tagsSetError("");
    try {
      const inst = await api(`/api/instances/${state.detailId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ port: value }),
      });
      input.value = inst.port;
      toast(`Port geändert auf ${inst.port} (RCON ${inst.rcon_port ?? inst.port + 1000}) — wirksam beim nächsten Start.`, "success");
      refreshOverview();
    } catch (e) {
      tagsSetError(`Port nicht geändert: ${e.message}`);
    } finally {
      btn.disabled = false;
    }
  }
  $("#tags-save").addEventListener("click", saveInstanceTags);
  $("#port-save").addEventListener("click", saveInstancePort);
  $("#inst-port-new").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); saveInstancePort(); }
  });

  function loadTagsAndPort(detail) {
    $("#inst-tags").value = (detail.tags || []).join(", ");
    $("#inst-port-new").value = detail.port ?? "";
    const running = !!detail.container?.running;
    $("#port-save").disabled = running;
    $("#port-save").title = running
      ? "Instanz läuft — bitte zuerst stoppen" : "Port ändern";
    tagsSetError("");
  }

  /* ---------- Installiertes Modpack: Update-Check + Pack-Update ---------- */
  function mpUpdateSetError(msg) {
    const box = $("#mp-update-error");
    if (msg) {
      setText(box, msg);
      show(box, true);
    } else {
      show(box, false);
    }
  }

  function renderPackUpdateState(data) {
    const info = $("#mp-update-info");
    const btn = $("#mp-update-btn");
    if (!data.installed) {
      setText(info, data.reason || "Noch kein Modpack installiert.");
      show(btn, false);
      return;
    }
    const inst = data.installed_pack || {};
    let text = `Installiert: ${inst.name || "?"}`;
    if (inst.date) text += ` (${new Date(inst.date).toLocaleDateString("de-DE")})`;
    if (!data.checkable) {
      setText(info, `${text} · ${data.reason || "Update-Check nicht möglich"}`);
      show(btn, false);
      return;
    }
    if (!data.latest) {
      setText(info, `${text} · keine neuere Version gefunden` +
        (data.reason ? ` (${data.reason})` : ""));
      show(btn, false);
      return;
    }
    if (data.update_available) {
      const date = data.latest.date
        ? new Date(data.latest.date).toLocaleDateString("de-DE") : "";
      text += ` → Update verfügbar: ${data.latest.name || "?"}`
        + (date ? ` (${date})` : "");
      setText(info, text);
      btn.disabled = false;
      btn.title = "Neueste Pack-Version erneut installieren (Mods/Configs werden ersetzt)";
      show(btn, true);
    } else if (data.compatible === false) {
      const date = data.latest.date
        ? new Date(data.latest.date).toLocaleDateString("de-DE") : "";
      text += ` · Neueste Version "${data.latest.name || "?"}"${date} `
        + "ist inkompatibel mit Loader/MC-Version dieser Instanz.";
      setText(info, text);
      show(btn, false);
    } else {
      setText(info, `${text} · neueste Version installiert`);
      show(btn, false);
    }
  }

  async function checkPackUpdate(quiet = false) {
    if (!state.detailId) return;
    const btn = $("#mp-update-check");
    btn.disabled = true;
    if (!quiet) mpUpdateSetError("");
    try {
      const data = await api(`/api/instances/${state.detailId}/modpacks/update-check`);
      if (state.detailId && data) renderPackUpdateState(data);
    } catch (e) {
      setText($("#mp-update-info"), "Update-Check fehlgeschlagen.");
      show($("#mp-update-btn"), false);
      if (!quiet) mpUpdateSetError(`Update-Check fehlgeschlagen: ${e.message}`);
    } finally {
      btn.disabled = false;
    }
  }

  async function startPackUpdate() {
    if (!state.detailId) return;
    const btn = $("#mp-update-btn");
    if (!window.confirm(
      "Modpack-Update starten?\n\nDie Pack-Dateien werden erneut heruntergeladen und "
      + "bestehende Mods/Configs ersetzt (Sicherheits-Snapshot wird automatisch "
      + "angelegt). Wirksam nach einem Neustart.")) return;
    btn.disabled = true;
    btn.textContent = "Starte…";
    mpUpdateSetError("");
    try {
      const job = await api(`/api/instances/${state.detailId}/modpacks/update`,
        { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({}) });
      await pollPackJob(job);
      toast("Modpack-Update abgeschlossen — Neustart, damit es wirkt.", "success");
      loadDetail().then(() => checkPackUpdate(true));
    } catch (e) {
      mpUpdateSetError(`Pack-Update fehlgeschlagen: ${e.message}`);
    } finally {
      btn.disabled = false;
      btn.textContent = "Pack aktualisieren";
      show(btn, false);
    }
  }
  $("#mp-update-check").addEventListener("click", () => checkPackUpdate(false));
  $("#mp-update-btn").addEventListener("click", startPackUpdate);

  /* ---------- Einstellungen (server.properties) ---------- */
  function cfgSetError(msg) {
    const box = $("#cfg-error");
    if (msg) {
      setText(box, msg);
      show(box, true);
    } else {
      show(box, false);
    }
  }

  function toggleCfgEditor() {
    state.cfgOpen = !state.cfgOpen;
    show($("#cfg-editor"), state.cfgOpen);
    setText($("#cfg-toggle"), state.cfgOpen ? "Ausblenden" : "Anzeigen");
    if (state.cfgOpen) loadCfg();
  }

  async function loadCfg() {
    cfgSetError("");
    try {
      const data = await api(`/api/instances/${state.detailId}/config`);
      state.cfgProps = data.properties || [];
      state.cfgSchema = data.schema || [];
      renderCfg();
    } catch (e) {
      $("#cfg-list").textContent = "";
      $("#cfg-form").textContent = "";
      cfgSetError(`Konfiguration nicht ladbar: ${e.message}`);
    }
  }

  function cfgFilterQuery() {
    return ($("#cfg-filter").value || "").trim().toLowerCase();
  }

  // Input/Select für einen bekannten Eigenschafts-Schema-Eintrag bauen
  function cfgFieldFor(entry, value) {
    let field;
    if (entry.type === "bool" || entry.type === "enum") {
      field = document.createElement("select");
      const choices = entry.type === "bool"
        ? ["true", "false"] : (entry.choices || []);
      for (const choice of choices) {
        const opt = document.createElement("option");
        opt.value = choice;
        opt.textContent = choice;
        field.appendChild(opt);
      }
      // Gespeicherter Wert liegt außerhalb der Auswahl (z. B. leer oder
      // Altwert)? Dann als "aktueller Wert" anbieten statt stillschweigend
      // auf die erste Wahl umzuschreiben — sonst mutiert ein unberührter
      // Eintrag bei jedem Speichern.
      const raw = value ?? "";
      const current = String(raw).trim().toLowerCase();
      if (!choices.includes(current)) {
        const keep = document.createElement("option");
        keep.value = raw;
        keep.textContent = raw === "" ? "(leer — aktueller Wert)" : `${raw} (aktueller Wert)`;
        field.appendChild(keep);
      }
      field.value = choices.includes(current) ? current : raw;
    } else if (entry.type === "int") {
      field = document.createElement("input");
      field.type = "number";
      if (entry.min !== undefined) field.min = String(entry.min);
      if (entry.max !== undefined) field.max = String(entry.max);
      field.value = value ?? "";
    } else {
      field = document.createElement("input");
      field.type = "text";
      field.value = value ?? "";
      field.spellcheck = false;
    }
    field.dataset.key = entry.key;
    field.className = "cfg-input";
    if (entry.managed) {
      field.disabled = true;
      field.title = entry.hint || "Wird vom Dashboard verwaltet";
    } else if (entry.hint) {
      field.title = entry.hint;
    }
    return field;
  }

  function cfgFormRow(entry, prop) {
    const row = document.createElement("div");
    row.className = "cfg-row cfg-row-form";
    const wrap = document.createElement("div");
    wrap.className = "cfg-keywrap";
    const key = document.createElement("span");
    key.className = "cfg-key";
    key.textContent = entry.key;
    key.title = entry.key;
    const label = document.createElement("span");
    label.className = "cfg-label muted small";
    label.textContent = entry.label || entry.key;
    if (entry.hint) label.title = entry.hint;
    wrap.append(key, label);
    row.append(wrap, cfgFieldFor(entry, prop.value));
    return row;
  }

  function cfgRawRow(prop) {
    const row = document.createElement("label");
    row.className = "cfg-row";
    const key = document.createElement("span");
    key.className = "cfg-key";
    key.textContent = prop.key;
    key.title = prop.key;
    const input = document.createElement("input");
    input.type = "text";
    input.value = prop.value ?? "";
    input.dataset.key = prop.key;
    input.spellcheck = false;
    row.append(key, input);
    return row;
  }

  function renderCfg() {
    const form = $("#cfg-form");
    const rawList = $("#cfg-list");
    const head = $("#cfg-unknown-head");
    const hint = $("#cfg-hint");
    form.textContent = "";
    rawList.textContent = "";
    const q = cfgFilterQuery();
    const matches = (prop, label) =>
      !q || prop.key.toLowerCase().includes(q)
        || (label || "").toLowerCase().includes(q);

    if (state.cfgMode === "raw") {
      show(form, false);
      show(head, false);
      show(rawList, true);
      setText(hint, "Alle Eigenschaften als freies Textfeld.");
      for (const prop of state.cfgProps) {
        if (matches(prop)) rawList.appendChild(cfgRawRow(prop));
      }
    } else {
      show(form, true);
      setText(hint, "Bekannte Eigenschaften mit passenden Eingabefeldern und "
        + "Bereichs-/Auswahl-Prüfung — verwaltete Einträge (Port, RCON, Welt) sind gesperrt.");
      const schemaByKey = new Map(state.cfgSchema.map((s) => [s.key, s]));
      const unknown = [];
      for (const prop of state.cfgProps) {
        const entry = schemaByKey.get(prop.key);
        if (entry) {
          if (matches(prop, entry.label)) form.appendChild(cfgFormRow(entry, prop));
        } else {
          unknown.push(prop);
        }
      }
      show(head, unknown.length > 0);
      show(rawList, unknown.length > 0);
      for (const prop of unknown) {
        if (matches(prop)) rawList.appendChild(cfgRawRow(prop));
      }
      if (!form.childElementCount) {
        const p = document.createElement("p");
        p.className = "muted small";
        p.textContent = state.cfgProps.length
          ? (q ? "Keine Treffer." : "Keine bekannten Eigenschaften in der Datei.")
          : "server.properties ist leer (einmal starten, damit der Server sie schreibt).";
        form.appendChild(p);
      }
    }
  }

  async function saveCfg() {
    const btn = $("#cfg-save");
    btn.disabled = true;
    btn.textContent = "Speichere…";
    cfgSetError("");
    try {
      // Sichtbare Felder auslesen; nicht gerenderte Keys behalten ihren Wert
      const values = {};
      document.querySelectorAll("#cfg-form [data-key], #cfg-list [data-key]")
        .forEach((field) => {
          values[field.dataset.key] = field.value;
        });
      const properties = state.cfgProps.map((p) =>
        ({ key: p.key, value: values[p.key] ?? p.value }));
      const data = await api(`/api/instances/${state.detailId}/config`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ properties }),
      });
      toast(`server.properties gespeichert (${data.saved} Einträge).`, "success");
      show($("#cfg-restart-row"), !!data.restart_required);
      loadCfg();
    } catch (e) {
      cfgSetError(`Speichern fehlgeschlagen: ${e.message}`);
    } finally {
      btn.disabled = false;
      btn.textContent = "Speichern";
    }
  }

  document.querySelectorAll("#cfg-mode .seg-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.cfgMode = btn.dataset.cfgMode === "raw" ? "raw" : "form";
      document.querySelectorAll("#cfg-mode .seg-btn").forEach((b) =>
        b.classList.toggle("active", b === btn));
      renderCfg();
    });
  });
  $("#cfg-toggle").addEventListener("click", toggleCfgEditor);
  $("#cfg-reload").addEventListener("click", loadCfg);
  $("#cfg-save").addEventListener("click", saveCfg);
  $("#cfg-filter").addEventListener("input", renderCfg);
  $("#cfg-restart").addEventListener("click", () => {
    if (!state.detailId) return;
    instAction({ id: state.detailId }, "restart");
  });

})();