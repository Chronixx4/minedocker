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
      // Abfragen ohne Body: 60-s-Deckel, sonst läuft der Ladespinner ewig,
      // wenn eine Verbindung lautlos stirbt (Uploads bleiben ausgenommen).
      const signal = opts.signal
        || (opts.body ? undefined : AbortSignal.timeout(60000));
      res = await fetch(path, Object.assign({}, opts, { headers, signal }));
    } catch (e) {
      if (e?.name === "TimeoutError") {
        throw Object.assign(
          new Error("Zeitüberschreitung — der Anbieter antwortet nicht (erneut versuchen)"),
          { status: 0 });
      }
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
        { status: res.status, filename: data.filename, data }
      );
    }
    return data;
  }

  // Upload mit Fortschrittsanzeige (fetch kennt keinen Upload-Progress → XHR).
  // Fehlverhalten wie api(): Error-Objekt mit .status und .message (Detail).
  // method="PUT" z. B. für den Server-Icon-Upload.
  function apiUpload(path, form, onProgress, method = "POST") {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      xhr.open(method, path);
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
  /* 8×8-Pixel-Icons als SVG-Data-URI (ein Zeichen = ein Pixel, "." = leer) */
  const PIXEL_COLORS = {
    g: "#5ccf4a", G: "#3e9a32", d: "#7a5434", D: "#5a3c24", s: "#a0a0a0", S: "#6b6b6b",
    k: "#1b1b1b", w: "#f2f2f2", y: "#f6c544", Y: "#c8961e", b: "#5fd8e6", r: "#ff5b4f",
    R: "#a8281e", t: "#c8a26a", o: "#e88a3a", e: "#3fd47a",
  };
  function pixelIcon(rows) {
    let rects = "";
    rows.forEach((row, y) => [...row].forEach((c, x) => {
      if (c !== ".") rects += `<rect x="${x}" y="${y}" width="1" height="1" fill="${PIXEL_COLORS[c]}"/>`;
    }));
    return "data:image/svg+xml;utf8," + encodeURIComponent(
      `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 8 8" shape-rendering="crispEdges">${rects}</svg>`);
  }

  // Hinweise im Stil von „Fortschritt erzielt!“: Pixel-Icon, Titel, Text
  const TOAST_STYLE = {
    success: ["Erledigt!", ["...e....", "..eee...", ".eewee..", "eeweeee.", ".eeeee..", "..eee...", "...e....", "........"]],
    error: ["Fehler", ["........", "..rr....", ".rRRr...", "rRRRRr..", ".rRRrr..", "..rr.rr.", "......r.", "........"]],
    info: ["Hinweis", [".RRRRRR.", ".RwwwwR.", ".RwkkwR.", ".RwwwwR.", ".RwkkwR.", ".RwwwwR.", ".RRRRRR.", "..YYYY.."]],
  };
  const toastIcons = {};
  function toast(message, type = "info") {
    const [title, rows] = TOAST_STYLE[type] || TOAST_STYLE.info;
    const el = document.createElement("div");
    el.className = `toast ${type}`;
    el.setAttribute("role", type === "error" ? "alert" : "status");
    const icon = document.createElement("img");
    icon.className = "toast-icon";
    icon.alt = "";
    icon.src = toastIcons[type] ||= pixelIcon(rows);
    const body = document.createElement("div");
    const head = document.createElement("b");
    head.className = "toast-title";
    head.textContent = title;
    const text = document.createElement("span");
    text.textContent = message; // textContent schützt vor XSS
    body.append(head, text);
    el.append(icon, body);
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

  // Rückfrage im Dashboard-Stil statt window.confirm: zeigt Titel, Text und
  // optional eine Liste (z. B. betroffene Dateien). Promise<boolean>.
  // typeToConfirm: Text, den man abtippen muss, bevor OK freigegeben wird
  // (für nicht rückgängig zu machende Aktionen wie „Server löschen“).
  function confirmDialog({ title, message = "", list = [], ok = "OK", danger = false,
    typeToConfirm = "" }) {
    const dlg = $("#confirm-dialog");
    setText($("#cd-title"), title);
    setText($("#cd-message"), message);
    const ul = $("#cd-list");
    ul.textContent = "";
    for (const item of list) {
      const li = document.createElement("li");
      li.textContent = item;
      ul.appendChild(li);
    }
    show(ul, list.length > 0);
    const okBtn = $("#cd-ok");
    setText(okBtn, ok);
    okBtn.classList.toggle("danger", danger);
    okBtn.classList.toggle("primary", !danger);
    const typeWrap = $("#cd-type-wrap");
    const typeInput = $("#cd-type");
    show(typeWrap, !!typeToConfirm);
    typeInput.value = "";
    typeInput.oninput = null;
    okBtn.disabled = false;
    if (typeToConfirm) {
      setText($("#cd-type-label"), `Zur Bestätigung „${typeToConfirm}“ eintippen:`);
      okBtn.disabled = true;
      typeInput.oninput = () => { okBtn.disabled = typeInput.value.trim() !== typeToConfirm; };
    }
    dlg.returnValue = "";
    return new Promise((resolve) => {
      dlg.addEventListener("close", () => resolve(dlg.returnValue === "ok"), { once: true });
      dlg.showModal();
      (typeToConfirm ? typeInput : okBtn).focus();
    });
  }

  // ESC schließt die Modal-Dialoge ebenfalls animiert
  ["users-dialog", "password-dialog", "fb-editor", "mod-detail"].forEach((id) => {
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

  /* ---------- Version + Update-Hinweis (Header-Chip) ---------- */
  async function loadAppMeta() {
    try {
      const data = await api("/api/meta");
      setText($("#app-version"), data.version || "–");
      const upd = data.update;
      if (upd?.available) {
        const el = $("#update-badge");
        if (upd.url) el.href = upd.url;
        setText(el, `Update verfügbar: ${upd.latest}`);
        show(el, true);
      }
    } catch (e) { /* nicht erreichbar/abgemeldet: Chip unverändert lassen */ }
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
        renderInstTabs(state.ovInstances?.instances || []);
        syncHotbar();
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
      if (state.tab === "servers") {
        loadInstances();
        // Mod-Suche zurück in den Server-Bereich holen, falls dort offen
        if (state.detailId && state.wsTab === "mods" && state.modsView === "add") {
          mountSearch(true);
        }
      }
      if (state.tab === "search") {
        // Erst Ziele (und Filter) laden, dann direkt suchen — sonst läuft
        // die Suche ins Leere, bevor die Instanz-Ziele da sind.
        loadInstanceTargets().then(() => {
          if (!mountSearch(false) && !$("#search-results").childElementCount) doSearch(0);
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

  // Anzeige-Zustand: wie instStateKey, aber schlafende Server (Schlafmodus)
  // heißen „schläft“; für Steuerung zählen sie weiter als laufend
  function instDisplayKey(inst) {
    const key = instStateKey(inst);
    return key === "running" && inst.container?.paused ? "paused" : key;
  }

  const OV_STATE_LABELS = {
    running: "läuft", starting: "startet…", error: "Fehler", stopped: "gestoppt",
    paused: "schläft",
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
    const live = liveEntry(inst);
    if (live?.paused || inst.container?.paused) {
      return "Schläft, niemand online. Wacht beim Verbinden auf.";
    }
    const ping = live?.ping;
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
      const card = node.querySelector(".ov-card");
      card.dataset.state = instStateKey(inst);
      card.dataset.id = inst.id;
      node.querySelector(".ov-card-icon").src = cardIcon(inst);
      setText(node.querySelector(".inst-name"), inst.name);
      setText(node.querySelector(".ov-card-sub"),
        `${inst.loader}${inst.loader_version ? ` ${inst.loader_version}` : ""} · MC ${inst.game_version}`);
      const badge = node.querySelector(".inst-badge");
      badge.classList.add(stateBadgeClass(inst));
      setText(node.querySelector(".inst-state"), OV_STATE_LABELS[instDisplayKey(inst)]);

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

      // Live-Info: MOTD bzw. Startzustand (aus /api/instances/live)
      const livePing = inst.container?.running ? liveEntry(inst)?.ping : null;
      const pingBox = node.querySelector(".ov-ping");
      if (inst.container?.running && !livePing?.online) {
        setText(pingBox, pingLine(inst));
        show(pingBox, true);
      } else if (livePing?.motd) {
        setText(pingBox, livePing.motd.replace(/\s+/g, " ").trim());
        show(pingBox, true);
      }

      // Spieler: Pixel-Köpfe (aus dem SLP-Sample) + Zähler
      const pcount = node.querySelector(".ov-pcount");
      const playersBox = node.querySelector(".ov-players");
      if (livePing?.online) {
        const p = livePing.players || {};
        setText(pcount, `${p.online || 0} / ${p.max || 0}`);
        pcount.title = "Spieler online";
        const sample = (p.sample || []).map((x) => x.name).filter(Boolean);
        for (const name of sample.slice(0, 8)) {
          const head = document.createElement("img");
          head.className = "ov-head";
          head.src = playerFace(name);
          head.alt = name;
          head.title = name;
          playersBox.appendChild(head);
        }
        if (sample.length > 8) {
          const more = document.createElement("span");
          more.className = "ov-head more";
          more.textContent = `+${sample.length - 8}`;
          more.title = sample.join(", ");
          playersBox.appendChild(more);
        }
        if (!sample.length) {
          const none = document.createElement("span");
          none.className = "muted small";
          none.textContent = p.online ? "Namen nicht freigegeben" : "niemand online";
          playersBox.appendChild(none);
        }
      } else {
        const off = document.createElement("span");
        off.className = "muted small";
        off.textContent = inst.container?.running ? "–" : "offline";
        playersBox.appendChild(off);
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
        const hist = state.ovHistory.cont[`mc-inst-${inst.id}`];
        if (hist) {
          node.querySelector(".ov-cpu-spark").appendChild(sparkline(hist.cpu, "", 120, 16, 100));
          node.querySelector(".ov-ram-spark").appendChild(sparkline(hist.ram, "", 120, 16));
        }
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
      node.querySelector(".logs").addEventListener("click", () => openServer(inst.id, "konsole"));
      node.querySelector(".open").addEventListener("click", () => openServer(inst.id));
      // Klick auf die Karte (außer auf Buttons/Chips) öffnet den Server
      card.addEventListener("click", (e) => {
        if (e.target.closest("button, a, .chip-btn, input, select")) return;
        openServer(inst.id);
      });
      grid.appendChild(node);
    }

    // „+ Neuer Server“ als letzte Karte (nur Admins, nur ohne aktiven Filter)
    if (state.ovInstances !== null && state.ovFilter === "all" && !q && !state.ovGroup) {
      const add = document.createElement("button");
      add.type = "button";
      add.className = "ov-card-add admin-only";
      const plus = document.createElement("span");
      plus.className = "ov-card-add-plus";
      plus.textContent = "+";
      const label = document.createElement("span");
      label.textContent = "Neuer Server";
      add.append(plus, label);
      add.addEventListener("click", () => {
        activateTab("servers");
        openCreate();
      });
      grid.appendChild(add);
    }
  }

  /* Öffnet eine Instanz im Server-Tab, optional direkt in einem Hotbar-Slot */
  function openServer(id, slot) {
    activateTab("servers");
    // openDetail setzt detailId und Slot synchron vor dem ersten await —
    // den Ziel-Slot daher direkt danach wählen (nicht erst nach allen Ladevorgängen)
    const p = Promise.resolve(id === state.detailId ? null : openDetail(id)).catch(() => {});
    if (slot) activateWsTab(slot, true);
    return p;
  }

  /* Karten-Icon: echtes server-icon.png (einmal je Instanz geladen), sonst Pixel-Block */
  state.iconCache = {}; // id -> Object-URL | null (kein Icon) | "pending"
  function cardIcon(inst) {
    const cached = state.iconCache[inst.id];
    if (typeof cached === "string" && cached !== "pending") return cached;
    if (cached === undefined) {
      state.iconCache[inst.id] = "pending";
      const headers = {};
      if (state.apiKey) headers["X-API-Key"] = state.apiKey;
      fetch(`/api/instances/${inst.id}/icon`, { headers })
        .then((res) => {
          if (res.ok) return res.blob();
          if (res.status === 404) return null;            // kein Icon hochgeladen
          throw new Error(`HTTP ${res.status}`);          // später erneut versuchen
        })
        .then((blob) => {
          state.iconCache[inst.id] = blob ? URL.createObjectURL(blob) : null;
          if (blob) {
            document.querySelectorAll(`.ov-card[data-id="${CSS.escape(inst.id)}"] .ov-card-icon`)
              .forEach((img) => { img.src = state.iconCache[inst.id]; });
          }
        })
        .catch(() => { delete state.iconCache[inst.id]; });
    }
    return serverBlockIcon(inst.name);
  }
  function forgetCardIcon(id) {
    const cached = state.iconCache[id];
    if (typeof cached === "string" && cached !== "pending") URL.revokeObjectURL(cached);
    delete state.iconCache[id];
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
        metaParts.push(OV_STATE_LABELS[instDisplayKey(inst)]);
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
    updateWsOverview();
    updateHotbarCounts();
    renderOverviewSummary(list);
    updateInstTabCounts();
    checkNotifications(list);
  }

  /* Ein-Satz-Zusammenfassung über der Kartenansicht + Status im Browser-Tab */
  function onlinePlayers(inst) {
    if (!inst.container?.running) return null;
    const ping = liveEntry(inst)?.ping;
    return ping?.online ? (ping.players?.online || 0) : null;
  }
  function renderOverviewSummary(list) {
    const running = list.filter((i) => instStateKey(i) === "running").length;
    const players = list.reduce((n, i) => n + (onlinePlayers(i) || 0), 0);
    const parts = [];
    if (state.ovInstances === null) {
      parts.push("Server konnten nicht geladen werden");
    } else if (!list.length) {
      parts.push("Noch keine Welt angelegt");
    } else {
      parts.push(`${running} von ${list.length} ${list.length === 1 ? "Server läuft" : "Servern laufen"}`);
      parts.push(`${players} ${players === 1 ? "Spieler" : "Spieler"} online`);
      const r = state.ovStatus?.resources || {};
      if (r.found) {
        parts.push(r.ram_limit_mb
          ? `RAM ${fmtMb(r.ram_mb)} / ${fmtMb(r.ram_limit_mb)}`
          : `RAM ${fmtMb(r.ram_mb)}`);
      }
    }
    setText($("#ov-summary"), parts.join(" · "));
    document.title = running
      ? `● ${players} online · Minedocker`
      : "Minedocker";
  }

  /* Spielerzahl je Server-Chip in der Leiste oben (ohne Neuaufbau der Leiste) */
  function updateInstTabCounts() {
    for (const inst of state.ovInstances?.instances || []) {
      const b = document.querySelector(`#inst-tabs .inst-tab[data-id="${CSS.escape(inst.id)}"]`);
      if (!b) continue;
      const dot = b.querySelector(".inst-tab-dot");
      const key = instStateKey(inst);
      dot.className = `inst-tab-dot ${key}`;
      dot.title = OV_STATE_LABELS[key];
      const n = onlinePlayers(inst);
      const count = b.querySelector(".inst-tab-count");
      setText(count, n === null ? "" : String(n));
      count.title = n === null ? "" : `${n} Spieler online`;
      show(count, n !== null);
    }
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
    renderInstTabs(state.ovInstances?.instances || []);
    updateWsControls();
    updateWsPerf();
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
  /* Tab im Hintergrund: normales Polling aus, aber bei aktiven
     Benachrichtigungen langsam weiter abfragen — sonst bemerkt
     checkNotifications Absturz/Beitritt erst beim Zurückkehren. */
  async function backgroundPoll() {
    if (!state.notify || !state.pollingAllowed) return;
    await refreshOverview(); // Container-Status (Absturz/Stopp)
    await refreshLive();     // Ping (bereit, Spieler beigetreten)
  }
  function startBackgroundPolling() {
    if (state.bgTimer || !state.notify) return;
    state.bgTimer = setInterval(backgroundPoll, 30000);
  }
  function stopBackgroundPolling() {
    clearInterval(state.bgTimer);
    state.bgTimer = null;
  }
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      stopPolling();
      startBackgroundPolling();
    } else {
      stopBackgroundPolling();
      startPolling();
    }
  });
  // Erst Auth prüfen (Login-Overlay?), dann Polling starten — sonst laufen
  // die 5-s-Polls gegen 401, während der Login noch offen ist.
  initAuth().then((authorized) => {
    state.pollingAllowed = authorized;
    if (authorized) startPolling();
    loadAppMeta();
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
    for (const i of targets) state.userActionAt[i.id] = Date.now();
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
    // Im Server-Bereich eingebettet: Ziel ist immer der geöffnete Server
    if (state.searchEmbedded) {
      if (!state.detailId) return null;
      const d = state.detailData?.id === state.detailId ? state.detailData
        : currentDetailInst();
      if (!d) return null;
      return { instanceId: d.id, label: d.name, isInstance: true,
        loader: d.loader, game_version: d.game_version };
    }
    return targetFromSelect($("#search-target-select"));
  }

  // Die Mod-Suche gibt es einmal: im Tab „Mods suchen“ (mit Zielauswahl)
  // oder im Server-Bereich unter Mods › Hinzufügen (Ziel = offener Server).
  // Die Karte wird dafür zwischen beiden Orten verschoben.
  function mountSearch(embedded) {
    const card = $("#search-card");
    const host = embedded ? $("#ws-mods-add") : $("#tab-search");
    if (!card || !host) return;
    if (card.parentElement !== host) host.appendChild(card);
    card.classList.toggle("embedded", embedded);
    state.searchEmbedded = embedded;
    const id = searchTarget()?.instanceId || null;
    const changed = state.searchMountedFor !== `${embedded}:${id}`;
    state.searchMountedFor = `${embedded}:${id}`;
    cartSyncTarget();
    renderCart();
    updateSearchHint();
    if (changed) {
      loadSearchFilters();
      doSearch(0);
    }
    return changed;
  }

  function mpTarget() {
    return targetFromSelect($("#mp-target-select"));
  }

  function updateSearchHint() {
    const target = searchTarget();
    setText($("#search-target"), !target
      ? "Keine Ziel-Instanz ausgewählt — Suche deaktiviert."
      : state.searchEmbedded
        ? `Mods für ${target.label} · ${target.loader} ${target.game_version}`
        : `Installationsziel: ${target.label} · Mods landen im mods-Ordner dieser Instanz`);
  }

  function updateMpHint() {
    const target = mpTarget();
    setText($("#mp-target"), target
      ? `Ziel: ${target.label} · Modpack wird in dieser Instanz installiert`
      : "Keine Ziel-Instanz gewählt — Treffer können direkt als neuer Server erstellt werden (Button „Neuer Server“).");
  }

  $("#search-target-select").addEventListener("change", () => {
    cartSyncTarget();
    renderCart();
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
    // Kategorie-IDs gelten nur je Anbieter → zurücksetzen, Liste neu laden
    state.searchFilterCategory = "";
    loadSearchCategories();
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
  // Standard: reine Client-Mods ausblenden (nur Modrinth kennt die Angabe)
  state.searchFilterEnvironment = "server_ok";
  state.searchFilterCategory = "";   // "" = alle Kategorien
  state.searchFiltersLoaded = false;
  state.searchSeq = 0;               // nur die jüngste Suche darf rendern
  state.searchPrefetch = null;       // vorab geladene nächste Seite

  for (const [sel, key] of [
    ["#search-filter-sort", "searchFilterSort"],
    ["#search-filter-loader", "searchFilterLoader"],
    ["#search-filter-version", "searchFilterVersion"],
    ["#search-filter-environment", "searchFilterEnvironment"],
    ["#search-filter-category", "searchFilterCategory"],
  ]) {
    $(sel).addEventListener("change", (e) => {
      state[key] = e.target.value;
      if (key === "searchFilterEnvironment") state.searchEnvironmentTouched = true;
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
    } else if (!state.searchFilterEnvironment && !state.searchEnvironmentTouched) {
      sel.value = "server_ok";
      state.searchFilterEnvironment = "server_ok";
    }
  }
  updateEnvironmentFilter();

  // Kategorien („Was kann die Mod?“) kommen vom jeweiligen Anbieter —
  // Modrinth und CurseForge führen unterschiedliche Listen.
  const searchCategoryCache = {};
  async function loadSearchCategories() {
    const source = state.searchSource || "modrinth";
    const sel = $("#search-filter-category");
    try {
      searchCategoryCache[source] ??= api(`/api/mods/categories?source=${source}`)
        .then((d) => d.categories || []);
      const cats = await searchCategoryCache[source];
      if ((state.searchSource || "modrinth") !== source) return; // inzwischen gewechselt
      fillSelect(sel, cats.map((c) => ({ value: c.id, label: c.name })), "Alle Kategorien");
      sel.value = cats.some((c) => c.id === state.searchFilterCategory)
        ? state.searchFilterCategory : "";
      sel.disabled = !cats.length;
    } catch (e) {
      delete searchCategoryCache[source]; // später erneut versuchen
      fillSelect(sel, [], "Alle Kategorien");
      sel.disabled = true;
    }
  }

  async function loadSearchFilters() {
    if (state.searchFiltersLoaded) return;
    state.searchFiltersLoaded = true;
    loadSearchCategories();
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
  // Seitenwechsel: zurück an den Anfang der Trefferliste, damit man die
  // neue Seite wieder von oben nach unten durchgehen kann
  function searchPage(offset) {
    doSearch(offset);
    $("#search-card").scrollIntoView({ behavior: "smooth", block: "start" });
  }
  $("#search-prev").addEventListener("click", () => searchPage(Math.max(0, state.offset - 20)));
  $("#search-next").addEventListener("click", () => searchPage(state.offset + 20));

  // CurseForge ohne API-Key: Backend antwortet mit 503 („CF_API_KEY" im Detail).
  // Statt der generischen Meldung einen Alternativ-Hinweis zeigen (keylos möglich:
  // Modrinth-Quelle oder CF-Pack als .zip hochladen).
  function cfNoKeyHint(e) {
    if (e.status !== 503 && !(e.message || "").includes("CF_API_KEY")) return null;
    return "CurseForge-Suche benötigt einen API-Key (wird nicht mehr vergeben). " +
      "Alternativen ohne Key: Quelle „Modrinth“ wählen oder ein CF-Pack als .zip " +
      "hochladen (Upload-Box im Modpacks-Tab) — beides funktioniert ohne Key.";
  }

  async function doSearch(offset, pendingRetry = 0) {
    clearTimeout(state.searchPendingTimer);
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
    const seq = ++state.searchSeq;
    show($("#search-loading"), true);
    show($("#search-error"), false);
    try {
      const url = searchUrl(target, q, offset);
      // Vorab geladene Seite nutzen (Weiter-Klick ohne Wartezeit)
      const pre = state.searchPrefetch;
      const fresh = pre && pre.url === url && Date.now() - pre.at < 60000;
      state.searchPrefetch = null;
      const data = await (fresh ? pre.promise : api(url));
      if (seq !== state.searchSeq) return; // überholt von neuerer Suche
      state.total = data.total;
      renderResults(data);
      if (data.installed_pending) {
        // Server erkennt die installierten Mods noch (erstes Hashen großer
        // Modpacks) → gleich noch einmal fragen, dann mit Markierung.
        // Nicht vorladen: die Seite käme ebenfalls ohne Markierung.
        if (pendingRetry < 40) {
          state.searchPendingTimer = setTimeout(() => {
            if (seq === state.searchSeq) doSearch(offset, pendingRetry + 1);
          }, 3000);
        }
      } else {
        prefetchSearch(target, q, offset + 20);
      }
    } catch (e) {
      if (seq !== state.searchSeq) return;
      const hint = state.searchSource === "curseforge" ? cfNoKeyHint(e) : null;
      setText($("#search-error"), hint || `Suche fehlgeschlagen: ${e.message}`);
      show($("#search-error"), true);
      $("#search-results").textContent = "";
    } finally {
      if (seq === state.searchSeq) show($("#search-loading"), false);
    }
  }

  // Lädt die nächste Seite im Hintergrund, solange man die aktuelle liest
  function prefetchSearch(target, q, offset) {
    if (offset >= state.total) return;
    const url = searchUrl(target, q, offset);
    const promise = api(url);
    promise.catch(() => {}); // Fehler zeigt erst der echte Seitenwechsel
    state.searchPrefetch = { url, promise, at: Date.now() };
  }

  function searchUrl(target, q, offset) {
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
    if (state.searchFilterCategory) params.set("category", state.searchFilterCategory);
    const cf = state.searchSource === "curseforge";
    const path = cf ? "/api/curseforge/search" : "/api/modrinth/search";
    return `${path}?${params}`;
  }

  function renderResults(data) {
    const container = $("#search-results");
    container.textContent = "";
    const tpl = $("#tpl-result");
    for (const hit of data.hits) {
      hit._source = data.source || "modrinth"; // Quelle pro Treffer merken
      const node = tpl.content.cloneNode(true);
      const icon = node.querySelector(".result-icon");
      fillModIcon(icon, hit.icon_url, hit.title);
      setText(node.querySelector(".name"), hit.title || hit.slug);
      setText(node.querySelector(".author"), hit.author ? "von " + hit.author : "");
      setText(node.querySelector(".desc"), hit.description || "");
      setText(node.querySelector(".version"),
        hit.latest_version ? `Version ${hit.latest_version}` : "");
      setText(node.querySelector(".downloads"), `${fmtNumber(hit.downloads)} Downloads`);
      setText(node.querySelector(".side"), sideLabel(hit.server_side));
      const card = node.querySelector(".result");
      if (hit.server_side === "unsupported") card.classList.add("is-client-only");
      // „Installiert“-Markierung: Mod liegt bereits in der Ziel-Instanz
      // (per Datei-Hash erkannt, auch bei abweichendem Dateinamen) —
      // Karte grün hervorheben + Tag prominent neben dem Titel
      if (hit.installed) {
        card.classList.add("is-installed");
        const tag = document.createElement("span");
        tag.className = "installed-tag";
        tag.textContent = "Installiert";
        node.querySelector(".result-title").appendChild(tag);
      }
      const btn = node.querySelector(".install");
      const progressBox = node.querySelector(".dl-progress");
      const bar = node.querySelector(".dl-bar");
      const pick = node.querySelector(".pick");
      if (hit.installed) {
        btn.disabled = true;
        btn.textContent = "Installiert ✓";
        btn.classList.add("installed");
        btn.title = "Bereits in der Ziel-Instanz installiert";
        pick.disabled = true;
      }
      pick.checked = state.cart.has(cartKey(hit._source, hit.project_id));
      pick.addEventListener("change", () => {
        if (pick.checked) cartAdd(hit, null);
        else cartRemove(hit._source, hit.project_id);
      });
      card.dataset.cartKey = cartKey(hit._source, hit.project_id);
      btn.addEventListener("click", () => installMod(hit, btn, progressBox, bar));
      node.querySelector(".details").addEventListener("click", () => openModDetail(hit));
      container.appendChild(node);
    }
    setText($("#search-page"), `Seite ${Math.floor(state.offset / 20) + 1} · ${fmtNumber(state.total)} Treffer`);
    $("#search-prev").disabled = state.offset <= 0;
    $("#search-next").disabled = state.offset + 20 >= state.total;
  }

  // Modrinth: required/optional/unsupported/unknown; CurseForge: null
  function sideLabel(side) {
    const map = { required: "Server: nötig", optional: "Server: optional",
      unsupported: "Nur Client" };
    return map[side] ?? "Server: unbekannt";
  }

  function fillModIcon(box, url, title) {
    box.textContent = "";
    if (url && String(url).startsWith("https://")) {
      const img = document.createElement("img");
      img.src = url;
      img.alt = "";
      img.loading = "lazy";
      box.appendChild(img);
    } else {
      box.textContent = (title || "?").charAt(0).toUpperCase();
    }
  }

  /* ---------- Installation (einzeln, aus den Details oder vorgemerkt) ---------- */
  // Reine Client-Mods (Sodium, Iris …) lassen den Server oft abstürzen
  async function confirmClientOnly(entries) {
    const names = entries.filter((e) => e.server_side === "unsupported")
      .map((e) => e.title || e.project_id);
    if (!names.length) return true;
    return confirmDialog({
      title: names.length === 1 ? "Reine Client-Mod installieren?" : "Reine Client-Mods installieren?",
      message: "Laut Modrinth laufen diese Mods nur im Spiel-Client und können den "
        + "Serverstart verhindern.",
      list: names,
      ok: "Trotzdem installieren",
      danger: true,
    });
  }

  // Startet eine Installation über /mods/install (ein Job für alle Dateien).
  // ui: {btn, bar} für den Fortschritt. Rückgabe: finaler Job oder null
  // (abgebrochen). Wirft bei Fehlern.
  async function runModInstall(items, target, ui) {
    const start = (overwrite) => api(`/api/instances/${target.instanceId}/mods/install`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ items, overwrite }),
    });
    let job;
    try {
      job = await start(false);
    } catch (e) {
      if (e.status !== 409) throw e;
      const ok = await confirmDialog({
        title: "Dateien ersetzen?",
        message: "Diese Dateien liegen schon im mods-Ordner. Die bisherigen Versionen "
          + "kommen beim Ersetzen in den Papierkorb.",
        list: e.data?.conflicts || [],
        ok: "Ersetzen",
      });
      if (!ok) return null;
      job = await start(true);
    }
    if (ui.onStart) ui.onStart(job);
    state.searchPrefetch = null; // vorab geladene Seite kennt die neue Mod noch nicht
    const finalJob = await pollJob(job, ui.bar, ui.btn);
    const deps = (job.dependencies || []).length;
    const done = (job.items || []).length - (finalJob.item_errors || []).length;
    const what = done === 1 && items.length === 1
      ? `"${items[0].title || job.items[0].filename}"`
      : `${done} Mod${done === 1 ? "" : "s"}`;
    toast(`${what} installiert${deps ? ` (+${deps} Abhängigkeit${deps === 1 ? "" : "en"})` : ""} → ${target.label}.`,
      "success");
    const problems = [...(finalJob.item_errors || []),
      ...(job.errors || []).map((e) => `${e.project_id}: ${e.detail}`)];
    if (problems.length) toast(`Nicht installiert: ${problems.join(" · ")}`, "error");
    if (Array.isArray(finalJob.dep_errors) && finalJob.dep_errors.length) {
      toast(`Abhängigkeiten fehlgeschlagen: ${finalJob.dep_errors.join(", ")}`, "error");
    }
    // Mod-Liste des Servers auffrischen, falls gerade geöffnet
    if (state.detailId === target.instanceId) loadDetail();
    return finalJob;
  }

  function markInstalled(source, projectId) {
    const card = document.querySelector(
      `#search-results .result[data-cart-key="${CSS.escape(cartKey(source, projectId))}"]`);
    if (!card) return;
    card.classList.add("is-installed");
    const btn = card.querySelector(".install");
    btn.disabled = true;
    btn.classList.add("installed");
    btn.textContent = "Installiert ✓";
    const pick = card.querySelector(".pick");
    pick.checked = false;
    pick.disabled = true;
  }

  async function installMod(hit, btn, progressBox, bar) {
    const target = searchTarget();
    if (!target) {
      toast("Keine Ziel-Instanz ausgewählt.", "error");
      return;
    }
    const source = hit._source || state.searchSource;
    if (!(await confirmClientOnly([hit]))) return;
    btn.disabled = true;
    btn.textContent = "Starte…";
    try {
      const job = await runModInstall(
        [{ source, project_id: hit.project_id, title: hit.title || null }], target, {
          btn, bar,
          onStart: (started) => {
            show(progressBox, true);
            // Abhängigkeiten vorab anzeigen (welche Dateien mit geladen werden)
            const depNames = (started.dependencies || []).map((d) => d.filename).filter(Boolean);
            setText(progressBox.querySelector(".dl-deps"), depNames.length
              ? `Mit installiert: ${depNames.slice(0, 3).join(", ")}`
                + (depNames.length > 3 ? ` (+${depNames.length - 3})` : "")
              : "");
          },
        });
      if (!job) {
        btn.disabled = false;
        btn.textContent = "Installieren";
        return;
      }
      hit.installed = true;
      cartRemove(source, hit.project_id);
      markInstalled(source, hit.project_id);
    } catch (e) {
      toast(`Installation fehlgeschlagen: ${e.message}`, "error");
      btn.disabled = false;
      btn.textContent = "Installieren";
    } finally {
      show(progressBox, false);
    }
  }

  /* ---------- Vormerken: mehrere Mods gemeinsam installieren ---------- */
  state.cart = new Map();    // "quelle:projekt" → {source, project_id, version_id, title, server_side}
  state.cartTarget = null;   // Instanz, für die vorgemerkt wurde
  state.cartPlanSeq = 0;

  function cartKey(source, projectId) {
    return `${source}:${projectId}`;
  }

  function cartSyncTarget() {
    const id = searchTarget()?.instanceId || null;
    if (state.cartTarget !== id) {
      state.cart.clear();
      state.cartTarget = id;
    }
  }

  function cartAdd(hit, version) {
    cartSyncTarget();
    const source = hit._source || state.searchSource;
    state.cart.set(cartKey(source, hit.project_id), {
      source,
      project_id: hit.project_id,
      version_id: version?.id || null,
      version_label: version?.version_number || null,
      title: hit.title || hit.slug || hit.project_id,
      server_side: hit.server_side,
    });
    renderCart();
  }

  function cartRemove(source, projectId) {
    if (!state.cart.delete(cartKey(source, projectId))) return;
    renderCart();
  }

  function cartClear() {
    state.cart.clear();
    document.querySelectorAll("#search-results .pick").forEach((p) => { p.checked = false; });
    renderCart();
  }

  function renderCart() {
    const n = state.cart.size;
    show($("#mod-cart"), n > 0);
    document.querySelectorAll("#search-results .result").forEach((card) => {
      const pick = card.querySelector(".pick");
      if (pick && !pick.disabled) pick.checked = state.cart.has(card.dataset.cartKey);
    });
    if (!n) return;
    const entries = [...state.cart.values()];
    setText($("#mod-cart-title"), n === 1 ? "1 Mod vorgemerkt" : `${n} Mods vorgemerkt`);
    const names = entries.map((e) => e.title + (e.version_label ? ` ${e.version_label}` : ""));
    setText($("#mod-cart-detail"), `· ${names.join(", ")} · Abhängigkeiten werden geprüft…`);
    // Vorschau (Abhängigkeiten + Größe) kurz verzögert, damit schnelles
    // Anhaken nicht für jeden Klick eine Anfrage auslöst
    clearTimeout(state.cartPlanTimer);
    const seq = ++state.cartPlanSeq;
    state.cartPlanTimer = setTimeout(async () => {
      const target = searchTarget();
      if (!target) return;
      try {
        const plan = await api(`/api/instances/${target.instanceId}/mods/plan`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ items: cartItems() }),
        });
        if (seq !== state.cartPlanSeq) return;
        const deps = plan.dependencies.length;
        const parts = [names.join(", ")];
        parts.push(deps === 0 ? "keine zusätzlichen Abhängigkeiten"
          : deps === 1 ? "1 Abhängigkeit kommt dazu" : `${deps} Abhängigkeiten kommen dazu`);
        parts.push(fmtBytes(plan.total_size));
        if (plan.errors.length) parts.push(`${plan.errors.length} nicht installierbar`);
        setText($("#mod-cart-detail"), `· ${parts.join(" · ")}`);
      } catch (e) {
        if (seq === state.cartPlanSeq) {
          setText($("#mod-cart-detail"), `· ${names.join(", ")} · Vorschau nicht möglich: ${e.message}`);
        }
      }
    }, 500);
  }

  function cartItems() {
    return [...state.cart.values()].map((e) => ({
      source: e.source, project_id: e.project_id,
      version_id: e.version_id, title: e.title,
    }));
  }

  $("#mod-cart-clear").addEventListener("click", cartClear);
  $("#mod-cart-install").addEventListener("click", async () => {
    const target = searchTarget();
    if (!target || !state.cart.size) return;
    const entries = [...state.cart.values()];
    if (!(await confirmClientOnly(entries))) return;
    const btn = $("#mod-cart-install");
    btn.disabled = true;
    btn.textContent = "Starte…";
    $("#mod-cart-bar").style.width = "0%";
    try {
      const job = await runModInstall(cartItems(), target, {
        btn, bar: $("#mod-cart-bar"),
        onStart: () => show($("#mod-cart-progress"), true),
      });
      if (job) {
        const failed = new Set((job.bundle || [])
          .filter((e) => e.kind === "item" && e.status !== "fertig")
          .map((e) => cartKey(e.source, e.project_id)));
        for (const e of entries) {
          const key = cartKey(e.source, e.project_id);
          if (!failed.has(key)) {
            state.cart.delete(key);
            markInstalled(e.source, e.project_id);
          }
        }
        renderCart();
      }
    } catch (e) {
      toast(`Installation fehlgeschlagen: ${e.message}`, "error");
    } finally {
      btn.disabled = false;
      btn.textContent = "Alle installieren";
      show($("#mod-cart-progress"), false);
    }
  });

  /* ---------- Details: Versionen, Changelog, Abhängigkeits-Vorschau ---------- */
  state.md = null; // {hit, source, data, version, seq}

  function openModDetail(hit) {
    const target = searchTarget();
    if (!target) {
      toast("Keine Ziel-Instanz ausgewählt.", "error");
      return;
    }
    // loadSeq/planSeq verwerfen veraltete Antworten (schnelles Umschalten)
    state.md = { hit, source: hit._source || state.searchSource, target, data: null,
      version: null, loadSeq: 0, planSeq: 0 };
    fillModIcon($("#md-icon"), hit.icon_url, hit.title);
    setText($("#md-title"), hit.title || hit.slug || hit.project_id);
    setText($("#md-desc"), hit.description || "");
    setText($("#md-meta"), "");
    $("#md-link").removeAttribute("href");
    setText($("#md-link"), hit.author ? `von ${hit.author}` : "");
    $("#md-all-versions").checked = false;
    $("#md-versions").textContent = "";
    setText($("#md-changelog"), "–");
    setText($("#md-plan"), "–");
    $("#md-install").disabled = true;
    $("#md-cart").disabled = true;
    setText($("#md-install"), hit.installed ? "Andere Version installieren" : "Installieren");
    $("#mod-detail").showModal();
    loadModDetail();
  }

  async function loadModDetail() {
    const md = state.md;
    if (!md) return;
    const seq = ++md.loadSeq;
    show($("#md-loading"), true);
    show($("#md-error"), false);
    show($("#md-versions-empty"), false);
    $("#md-versions").textContent = "";
    try {
      const all = $("#md-all-versions").checked ? "true" : "false";
      const data = await api(`/api/instances/${md.target.instanceId}/mods/browse/`
        + `${md.source}/${encodeURIComponent(md.hit.project_id)}?all_versions=${all}`);
      if (state.md !== md || seq !== md.loadSeq) return;
      md.data = data;
      const p = data.project;
      setText($("#md-title"), p.title);
      if (p.description) setText($("#md-desc"), p.description);
      fillModIcon($("#md-icon"), p.icon_url || md.hit.icon_url, p.title);
      if (p.page_url && p.page_url.startsWith("https://")) {
        $("#md-link").href = p.page_url;
        setText($("#md-link"), `${md.hit.author ? `von ${md.hit.author} · ` : ""}`
          + `auf ${md.source === "curseforge" ? "CurseForge" : "Modrinth"} öffnen ↗`);
      }
      const meta = [`${fmtNumber(p.downloads)} Downloads`];
      if (p.server_side !== null && p.server_side !== undefined) meta.push(sideLabel(p.server_side));
      if (p.categories?.length) meta.push(p.categories.join(", "));
      setText($("#md-meta"), meta.join(" · "));
      setText($("#md-versions-title"), data.filtered
        ? `Versionen für ${data.loader} ${data.game_version}` : "Alle Versionen");
      renderModVersions(data.versions);
    } catch (e) {
      if (state.md !== md || seq !== md.loadSeq) return;
      const hint = md.source === "curseforge" ? cfNoKeyHint(e) : null;
      setText($("#md-error"), hint || `Details nicht abrufbar: ${e.message}`);
      show($("#md-error"), true);
    } finally {
      if (state.md === md && seq === md.loadSeq) show($("#md-loading"), false);
    }
  }

  const VERSION_TYPE_LABELS = { release: "", beta: "Beta", alpha: "Alpha" };

  function renderModVersions(versions) {
    const list = $("#md-versions");
    list.textContent = "";
    const md = state.md;
    if (!versions.length) {
      setText($("#md-versions-empty"), md.data.filtered
        ? `Keine Version für ${md.data.loader} ${md.data.game_version}. `
          + "„Alle Versionen zeigen“ listet auch unpassende."
        : "Keine Versionen gefunden.");
      show($("#md-versions-empty"), true);
      setText($("#md-changelog"), "–");
      setText($("#md-plan"), "–");
      $("#md-install").disabled = true;
      $("#md-cart").disabled = true;
      return;
    }
    versions.forEach((v, i) => {
      const li = document.createElement("li");
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "md-version";
      btn.setAttribute("role", "option");
      const num = document.createElement("span");
      num.className = "md-vnum";
      num.textContent = v.version_number || v.name || v.filename;
      const info = document.createElement("span");
      info.className = "md-vinfo muted small";
      const date = v.date ? new Date(v.date) : null;
      const parts = [];
      if (VERSION_TYPE_LABELS[v.type]) parts.push(VERSION_TYPE_LABELS[v.type]);
      if (date && !Number.isNaN(date.getTime())) parts.push(date.toLocaleDateString("de-DE"));
      if (!md.data.filtered && v.game_versions?.length) parts.push(v.game_versions.join(", "));
      if (!md.data.filtered && v.loaders?.length) parts.push(v.loaders.join("/"));
      parts.push(fmtBytes(v.size));
      info.textContent = parts.join(" · ");
      if (v.type !== "release") btn.classList.add("is-pre");
      btn.append(num, info);
      btn.addEventListener("click", () => selectModVersion(v, btn));
      li.appendChild(btn);
      list.appendChild(li);
      if (i === 0) selectModVersion(v, btn);
    });
  }

  async function selectModVersion(v, btn) {
    const md = state.md;
    if (!md) return;
    md.version = v;
    const seq = ++md.planSeq;
    document.querySelectorAll("#md-versions .md-version").forEach((b) => {
      b.classList.toggle("active", b === btn);
      b.setAttribute("aria-selected", b === btn ? "true" : "false");
    });
    $("#md-install").disabled = false;
    $("#md-cart").disabled = false;
    setText($("#md-install"), `${v.version_number || "Version"} installieren`);
    // Changelog: Modrinth liefert ihn mit, CurseForge nur einzeln
    if (v.changelog !== null && v.changelog !== undefined) {
      setText($("#md-changelog"), v.changelog || "Kein Changelog angegeben.");
    } else {
      setText($("#md-changelog"), "Lade Changelog…");
      api(`/api/curseforge/project/${encodeURIComponent(md.data.project.project_id)}`
        + `/files/${encodeURIComponent(v.id)}/changelog`)
        .then((data) => {
          if (state.md === md && md.version === v) {
            v.changelog = data.changelog || "";
            setText($("#md-changelog"), v.changelog || "Kein Changelog angegeben.");
          }
        })
        .catch((e) => {
          if (state.md === md && md.version === v) {
            setText($("#md-changelog"), `Changelog nicht abrufbar: ${e.message}`);
          }
        });
    }
    // Vorschau: welche Dateien diese Version mitbringt
    setText($("#md-plan"), "Prüfe Abhängigkeiten…");
    try {
      const plan = await api(`/api/instances/${md.target.instanceId}/mods/plan`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ items: [{ source: md.source,
          project_id: md.data.project.project_id, version_id: v.id }] }),
      });
      if (state.md !== md || seq !== md.planSeq) return;
      renderModPlan(plan);
    } catch (e) {
      if (state.md === md && seq === md.planSeq) {
        setText($("#md-plan"), `Vorschau nicht möglich: ${e.message}`);
      }
    }
  }

  function renderModPlan(plan) {
    const box = $("#md-plan");
    box.textContent = "";
    if (plan.errors.length) {
      box.textContent = plan.errors.map((e) => e.detail).join(" · ");
      return;
    }
    const lines = [];
    for (const d of plan.dependencies) lines.push(`${d.filename} (${fmtBytes(d.size)})`);
    if (!lines.length) lines.push("Keine zusätzlichen Pflicht-Abhängigkeiten.");
    const installed = plan.skipped.filter((s) => /installiert|existiert/.test(s.reason || "")).length;
    if (installed) lines.push(`${installed} Abhängigkeit(en) sind schon installiert.`);
    if (plan.conflicts.length) {
      lines.push(`${plan.conflicts.join(", ")} liegt schon im mods-Ordner und wird ersetzt.`);
    }
    const ul = document.createElement("ul");
    for (const line of lines) {
      const li = document.createElement("li");
      li.textContent = line;
      ul.appendChild(li);
    }
    box.appendChild(ul);
  }

  $("#md-all-versions").addEventListener("change", loadModDetail);
  $("#md-close").addEventListener("click", () => closeDialog($("#mod-detail")));
  $("#mod-detail").addEventListener("close", () => { state.md = null; });
  $("#md-cart").addEventListener("click", () => {
    const md = state.md;
    if (!md?.version) return;
    cartAdd({ ...md.hit, _source: md.source, project_id: md.data.project.project_id },
      md.version);
    toast(`"${md.data.project.title}" ${md.version.version_number} vorgemerkt.`, "success");
    closeDialog($("#mod-detail"));
  });
  $("#md-install").addEventListener("click", async () => {
    const md = state.md;
    if (!md?.version) return;
    const hit = md.hit;
    if (!(await confirmClientOnly([{ ...hit, server_side: md.data.project.server_side }]))) return;
    const btn = $("#md-install");
    btn.disabled = true;
    try {
      const job = await runModInstall([{ source: md.source,
        project_id: md.data.project.project_id, version_id: md.version.id,
        title: md.data.project.title }], md.target, { btn, bar: null });
      if (job) {
        hit.installed = true;
        cartRemove(md.source, hit.project_id);
        markInstalled(md.source, hit.project_id);
        closeDialog($("#mod-detail"));
      }
    } catch (e) {
      toast(`Installation fehlgeschlagen: ${e.message}`, "error");
    } finally {
      btn.disabled = false;
      if (md.version) setText(btn, `${md.version.version_number || "Version"} installieren`);
    }
  });

  function pollJob(job, bar, btn) {
    // resolve(finaler Job-Datenstand) — u. a. für dep_errors des Bundles
    return new Promise((resolve, reject) => {
      const timer = setInterval(async () => {
        try {
          const data = await api(`/api/modrinth/jobs/${job.job_id}`);
          if (data.total > 0) {
            const pct = Math.min(100, Math.round((data.downloaded / data.total) * 100));
            if (bar) bar.style.width = pct + "%";
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
  state.detailData = null;   // letzte /api/instances/{id}-Antwort (Workspace-Steuerung)
  state.wsTab = null;        // aktiver Workspace-Bereich (Mods, Welt, …)
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
      state.instList = data.instances;
      setText($("#inst-dir"), `Speicherort: ${data.directory}`);
      setText($("#inst-count"), `(${data.instances.length})`);
      show($("#inst-empty"), data.instances.length === 0);
      renderInstances(data.instances);
      renderInstTabs(data.instances);
    } catch (e) {
      setText($("#inst-error"), `Instanzen konnten nicht geladen werden: ${e.message}`);
      show($("#inst-error"), true);
    } finally {
      show($("#inst-loading"), false);
    }
  }
  $("#inst-reload").addEventListener("click", loadInstances);

  function stateBadgeClass(inst) {
    if (inst.container?.running && inst.container?.paused) return "sleep";
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
    state.userActionAt[inst.id] = Date.now();
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
    const running = inst.container?.running || inst.status === "running";
    if (!(await confirmDialog({
      title: `Server „${inst.name}“ löschen?`,
      message: (running ? "Der Server läuft noch und wird dafür gestoppt. " : "")
        + "Welt, Mods und Configs werden endgültig gelöscht und lassen sich "
        + "nicht wiederherstellen. Vorhandene Backups bleiben erhalten.",
      ok: "Endgültig löschen",
      danger: true,
      typeToConfirm: inst.name,
    }))) return;
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
    if (state.detailId) closeDetail(); // Workspace schließen, Formular freilegen
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
    if (state.detailId) closeDetail(); // Workspace schließen, Import freilegen
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
  state.instList = [];      // letzte /api/instances-Antwort (Sofort-Vorbefüllung)
    }
  }

  function appendLogLine(line) {
    feedActivity(line);
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

  /* ---------- Detail-Ansicht (Instanz-Workspace) ---------- */
  async function openDetail(id) {
    state.detailId = id;
    // Daten der Bereiche laden erst, wenn man sie öffnet (weniger Anfragen
    // beim Öffnen eines Servers); der aktuelle Bereich lädt nach den Details
    state.wsLoaded = null; // erst nach dem Zurücksetzen der Bereiche laden
    show($("#inst-list-wrap"), false); // Workspace ersetzt die Liste (Tab-Look)
    show($("#inst-create"), false);
    show($("#inst-import-box"), false);
    activateWsTab(state.wsTab || "uebersicht");
    const known = (state.ovInstances?.instances || []).find((i) => i.id === id);
    $("#detail-icon").src = serverBlockIcon(known?.name || id);
    syncHotbar();
    renderInstTabs(state.ovInstances?.instances || []);
    updateWsOverview();
    updateWsControls();
    updateWsPerf();
    updateInstSelection(); // Karte im Server-Tab hervorheben
    closeDetailLogStream(); // Stream/Timer der vorherigen Instanz beenden
    showSoft($("#inst-detail"), true);
    // Mobil: Workspace liegt über der Liste — von oben einblenden
    if (window.matchMedia("(max-width: 760px)").matches) {
      $("#inst-detail").scrollIntoView({ behavior: "smooth", block: "start" });
    }
    $("#detail-logs").textContent = "–";
    rebuildActivity([]);
    // Logs SOFORT starten (Live-Stream; endet der Stream sofort, z. B. bei
    // gestopptem Container, springt der Fallback auf Polling). Nicht erst
    // nach dem Detail-Laden — sonst bleibt das Log-Fenster stehen, solange
    // Docker/Ping/Scan dauern.
    openDetailLogStream();
    // Einstellungen sofort aus der zuletzt geladenen Liste vorbefüllen —
    // der Workspace wirkt damit sofort bedienbar, während die Details
    // (Ping, Mods, Speicher) noch nachladen.
    const cached = (state.instList || []).find((i) => i.id === id);
    if (cached) {
      setText($("#detail-title"), cached.name);
      $("#jvm-opts").value = cached.jvm_opts || "";
      $("#jvm-aikar").checked = !!cached.use_aikar;
      $("#jvm-java").value = cached.java || "auto";
      loadHibernate(cached);
      setRamFields(cached.memory);
      state.detailMemory = (cached.memory || "2G").toUpperCase();
      loadSchedule(cached);
      show($("#sched-error"), false);
      loadTagsAndPort(cached);
    }
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
    // Mod-Update-Ansicht zurücksetzen (Ergebnis ist instanzbezogen)
    state.detailUpdates = null;
    setText($("#mods-update-summary"), "");
    show($("#mods-update-all"), false);
    show($("#mods-update-progress"), false);
    show($("#mods-update-error"), false);
    // Welten/Karte zurücksetzen
    show($("#world-error"), false);
    $("#world-upload-file").value = "";
    $("#world-upload-btn").disabled = true;
    $("#worlds-list").textContent = "";
    show($("#map-error"), false);
    resetMapFrame();
    // Server-Icon zurücksetzen
    iconSetError("");
    $("#icon-upload-file").value = "";
    $("#icon-upload-btn").disabled = true;
    // Datei-Browser zurücksetzen (Wurzel der neuen Instanz laden)
    state.fb = { path: "", entries: [] };
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
      $("#jvm-java").value = detail.java || "auto";
      loadHibernate(detail);
      setRamFields(detail.memory);
      state.detailMemory = (detail.memory || "2G").toUpperCase();
      show($("#jvm-error"), false);
      // Zeitplan der Instanz in die Felder laden
      loadSchedule(detail);
      show($("#sched-error"), false);
      // Tags + Port in die Felder laden
      loadTagsAndPort(detail);
      show($("#mp-update-btn"), false);
      setText($("#mp-update-info"), "Update-Check läuft…");
      show($("#mp-update-error"), false);
    }
    // server.properties laden: füttert den Editor UND die Info-Box
    // (Schwierigkeit, Spielmodus, MOTD) — nach dem Laden Info neu rendern
    const openedId = state.detailId;
    loadCfg().then(() => {
      if (state.detailId === openedId && state.detailData) {
        renderDetailInfo(state.detailData);
      }
    });
    updateWsControls();
    if (state.detailId === openedId) {
      state.wsLoaded = new Set();
      loadWsTabData(state.wsTab);
    }
  }

  /* Daten eines Workspace-Bereichs einmal je geöffnetem Server laden */
  function loadWsTabData(key) {
    if (!state.detailId || !state.wsLoaded || state.wsLoaded.has(key)) return;
    state.wsLoaded.add(key);
    if (key === "welt") { wsetLoad(); loadWorldInfo(); dpReload(); loadIconPreview(); }
    else if (key === "dateien") fbReload();
    else if (key === "spieler") loadWhitelist();
    else if (key === "backups") loadBackups();
    else if (key === "mods") { checkPackUpdate(true); loadModTrash(); }
    else if (key === "einstellungen") loadVersionForm();
  }

  /* ---------- Versionswechsel (MC-Version/Loader) ---------- */
  function verSetError(msg) {
    setText($("#ver-error"), msg);
    show($("#ver-error"), !!msg);
  }

  function verTarget() {
    return {
      loader: $("#ver-loader").value,
      game_version: $("#ver-mc-version").value,
      loader_version: $("#ver-loader-version").value || null,
    };
  }

  async function loadVersionForm() {
    const inst = state.detailData || currentDetailInst();
    if (!inst) return;
    show($("#ver-preview"), false);
    verSetError("");
    fillSelect($("#ver-mc-version"), [], "Versionen werden geladen…");
    try {
      const versions = await api("/api/catalog/mc-versions");
      const options = versions.releases.map((v) => ({ value: v, label: v }));
      if (!versions.releases.includes(inst.game_version)) {
        options.unshift({ value: inst.game_version, label: inst.game_version });
      }
      fillSelect($("#ver-mc-version"), options, "Version wählen");
      $("#ver-mc-version").value = inst.game_version;
      await reloadLoadersInto("ver", inst.game_version);
      $("#ver-loader").value = inst.loader;
      updateLoaderVersionInto("ver");
    } catch (e) {
      fillSelect($("#ver-mc-version"), [], "Katalog nicht erreichbar");
    }
  }

  function verRow(text, cls) {
    const li = document.createElement("li");
    li.className = `mod-row ${cls || ""}`;
    li.textContent = text;
    return li;
  }

  $("#ver-mc-version").addEventListener("change", async () => {
    const loader = $("#ver-loader").value;
    await reloadLoadersInto("ver", $("#ver-mc-version").value);
    if (loader) { $("#ver-loader").value = loader; updateLoaderVersionInto("ver"); }
    show($("#ver-preview"), false);
  });
  $("#ver-loader").addEventListener("change", () => {
    updateLoaderVersionInto("ver");
    show($("#ver-preview"), false);
  });

  $("#ver-check").addEventListener("click", async () => {
    if (!state.detailId) return;
    const btn = $("#ver-check");
    const target = verTarget();
    if (!target.loader || !target.game_version) { verSetError("Bitte Version und Loader wählen."); return; }
    btn.disabled = true;
    btn.textContent = "Prüfe…";
    verSetError("");
    try {
      const res = await api(`/api/instances/${state.detailId}/version/check`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ loader: target.loader, game_version: target.game_version }),
      });
      const m = res.mods;
      const parts = [`${res.current.loader} ${res.current.game_version} → ${res.target.loader} ${res.target.game_version}.`];
      const updatable = m.available.filter((x) => !x.unchanged).length;
      if (m.available.length || m.missing.length || m.unknown.length) {
        parts.push(`${updatable} Mods werden aktualisiert, ${m.missing.length} gibt es dafür nicht,`
          + ` ${m.unknown.length} unbekannt (bleiben wie sie sind).`);
      }
      if (res.modpack) parts.push(`Achtung: Modpack „${res.modpack}“ ist installiert, besser ein passendes Pack-Update nutzen.`);
      if (res.downgrade) parts.push("Achtung: ältere Version, die Welt kann beschädigt werden.");
      setText($("#ver-summary"), parts.join(" "));
      const list = $("#ver-mods");
      list.textContent = "";
      for (const x of m.missing) list.appendChild(verRow(`✗ ${x.filename}: keine passende Version`, "warn"));
      for (const x of m.available.filter((y) => !y.unchanged)) {
        list.appendChild(verRow(`↑ ${x.filename}: ${x.from || "?"} → ${x.to}`));
      }
      for (const x of m.unknown) list.appendChild(verRow(`? ${x.filename}: unbekannt, bleibt unverändert`));
      show($("#ver-downgrade-wrap"), !!res.downgrade);
      $("#ver-allow-downgrade").checked = false;
      show($("#ver-preview"), true);
    } catch (e) {
      verSetError(`Prüfung fehlgeschlagen: ${e.message}`);
    } finally {
      btn.disabled = false;
      btn.textContent = "Mods prüfen";
    }
  });

  $("#ver-apply").addEventListener("click", async () => {
    if (!state.detailId) return;
    const target = verTarget();
    const ok = await confirmDialog({
      title: "Version wechseln?",
      message: `Der Server wird auf ${target.loader} ${target.game_version} umgestellt. `
        + "Vorher entsteht ein Backup inklusive Welt.",
      ok: "Wechseln",
    });
    if (!ok) return;
    const btn = $("#ver-apply");
    btn.disabled = true;
    verSetError("");
    try {
      const job = await api(`/api/instances/${state.detailId}/version`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          ...target,
          update_mods: $("#ver-update-mods").checked,
          disable_missing: $("#ver-disable-missing").checked,
          allow_downgrade: $("#ver-allow-downgrade").checked,
        }),
      });
      const done = await pollProgress(job, {
        box: $("#ver-progress"), bar: $("#ver-bar"), phase: $("#ver-phase"), pct: $("#ver-pct"),
      });
      const sum = done?.summary || {};
      const msg = [`Server steht jetzt auf ${sum.target || `${target.loader} ${target.game_version}`}.`];
      if (sum.updated) msg.push(`${sum.updated} Mods aktualisiert.`);
      if (sum.disabled?.length) msg.push(`${sum.disabled.length} deaktiviert.`);
      toast(msg.join(" "), "success");
      if (sum.failed) verSetError(`Fehler bei: ${sum.errors.join(" · ")}`);
      show($("#ver-preview"), false);
      await loadDetail();
      loadVersionForm();
    } catch (e) {
      verSetError(`Wechsel fehlgeschlagen: ${e.message}`);
    } finally {
      btn.disabled = false;
    }
  });
  function closeDetail() {
    state.detailId = null;
    state.detailData = null;
    updateInstSelection(); // Hervorhebung aufheben
    closeDetailLogStream();
    showSoft($("#inst-detail"), false);
    show($("#inst-list-wrap"), true); // Liste wieder einblenden
    syncHotbar();
    renderInstTabs(state.ovInstances?.instances || []);
  }
  $("#detail-close").addEventListener("click", closeDetail);
  $("#inst-delete-btn").addEventListener("click", () => {
    const inst = currentDetailInst() || state.detailData;
    if (inst) deleteInstance(inst);
  });

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
      setText($("#detail-state-text"), OV_STATE_LABELS[instDisplayKey(detail)]);
      state.detailData = detail;
      // Eingebettete Mod-Suche: Ziel steht erst jetzt sicher fest
      if (state.searchEmbedded && state.wsTab === "mods" && state.modsView === "add") {
        mountSearch(true);
      }
      renderDetailInfo(detail);
      renderDetailMods(detail.mods || [], detail);
      if (!$("#detail-icon").src.startsWith("blob:")) $("#detail-icon").src = serverBlockIcon(detail.name);
      updateHotbarCounts();
      updateWsOverview();
      state.detailRunning = !!detail.container?.running;
      setText($("#pack-target"),
        `Ziel: ${detail.loader} ${detail.game_version}${detail.loader_version ? ` (${detail.loader_version})` : ""}`);
      if (detail.container?.running) openDetailLogStream();
      else if (!state.detailLogStream) startDetailLogPolling(); // gestoppt: Polling halten
      if (detail.disk_pending) loadDetailDisk(id);
      return detail;
    } catch (e) {
      if (e.status === 404) { closeDetail(); return null; }
      setText($("#detail-error"), `Details nicht abrufbar: ${e.message}`);
      show($("#detail-error"), true);
      return null;
    }
  }

  /* Speicher nachladen: Die Detail-Antwort wartet nicht auf die Zählung
     großer Welten ("disk_pending"), der Wert kommt hier nach. */
  async function loadDetailDisk(id) {
    try {
      const disk = await api(`/api/instances/${id}/disk`);
      if (state.detailId !== id || !state.detailData) return;
      state.detailData.disk = disk;
      state.detailData.disk_pending = false;
      renderDetailInfo(state.detailData);
    } catch (e) {
      /* Speicher bleibt „wird berechnet…“; nächstes Neuladen versucht es erneut */
    }
  }

  /* ---------- Absturz-Diagnose ---------- */
  function renderCrash(detail) {
    const crash = detail?.last_crash;
    const box = $("#crash-box");
    show(box, !!crash || detail?.status === "error");
    show($("#crash-dismiss"), !!crash);
    if (!crash) {
      // Fehlerstatus ohne Diagnose (z. B. aus älterer Version): anbieten
      setText($("#crash-title"), "Der Server meldet einen Fehler");
      setText($("#crash-when"), "");
      $("#crash-findings").textContent = "";
      const li = document.createElement("li");
      li.textContent = "„Neu analysieren“ wertet Log und Crash-Report aus und nennt die Ursache.";
      $("#crash-findings").appendChild(li);
      show($("#crash-suspects"), false);
      $("#crash-log").textContent = "–";
      return;
    }
    setText($("#crash-title"), `Letzter Absturz: ${crash.summary || "Ursache nicht erkannt"}`);
    setText($("#crash-when"), [crash.at ? fmtDate(crash.at) : "",
      crash.exit_code != null ? `Exit-Code ${crash.exit_code}` : "",
      crash.crash_report ? `Bericht: crash-reports/${crash.crash_report}` : ""]
      .filter(Boolean).join(" · "));
    const list = $("#crash-findings");
    list.textContent = "";
    const findings = crash.findings || [];
    if (!findings.length) {
      const li = document.createElement("li");
      li.textContent = "Keine bekannte Ursache gefunden. Die letzten Logzeilen unten zeigen meist, was schiefging.";
      list.appendChild(li);
    }
    for (const f of findings) {
      const li = document.createElement("li");
      const title = document.createElement("strong");
      title.textContent = f.title;
      const hint = document.createElement("div");
      hint.textContent = f.hint;
      li.append(title, hint);
      if (f.evidence?.length) {
        const pre = document.createElement("pre");
        pre.textContent = f.evidence.join("\n");
        li.appendChild(pre);
      }
      list.appendChild(li);
    }
    const suspects = crash.suspects || [];
    setText($("#crash-suspects"), suspects.length ? `Verdächtige Mods: ${suspects.join(", ")}` : "");
    show($("#crash-suspects"), suspects.length > 0);
    $("#crash-log").textContent = (crash.log_tail || []).join("\n") || "–";
    $("#crash-log-wrap").open = !findings.length;
  }

  $("#crash-dismiss").addEventListener("click", async () => {
    if (!state.detailId) return;
    try {
      await api(`/api/instances/${state.detailId}/crash`, { method: "DELETE" });
      if (state.detailData) delete state.detailData.last_crash;
      show($("#crash-box"), false);
    } catch (e) {
      toast(`Ausblenden fehlgeschlagen: ${e.message}`, "error");
    }
  });

  async function analyzeCrash() {
    if (!state.detailId) return;
    const id = state.detailId;
    try {
      const crash = await api(`/api/instances/${id}/crash/analyze`, { method: "POST" });
      if (state.detailId !== id || !state.detailData) return;
      state.detailData.last_crash = crash;
      renderCrash(state.detailData);
    } catch (e) {
      toast(`Analyse fehlgeschlagen: ${e.message}`, "error");
    }
  }
  $("#crash-analyze").addEventListener("click", analyzeCrash);

  function renderDetailInfo(detail) {
    renderCrash(detail);
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
      : (detail.disk_pending ? "wird berechnet…" : null);
    const items = [
      ["Status", OV_STATE_LABELS[instDisplayKey(detail)]],
      ["Docker", detail.container?.state || "–"],
      ["Uptime", up || "–"],
      ["Loader", `${detail.loader}${detail.loader_version ? ` ${detail.loader_version}` : ""}`],
      ["Minecraft", detail.game_version],
      ["Port", String(detail.port)],
      ["Adresse", hostFor(detail)],
      ["RAM", detail.memory || "2G"],
      ["Speicher", diskLine || "–"],
      ["Erstellt", fmtDate(detail.created_at)],
      ["Erreichbar", detail.container?.paused ? "schläft (wacht beim Verbinden auf)"
        : ping.online ? "ja" : "nein"],
      ["Spieler", ping.online && ping.players
        ? `${ping.players.online}/${ping.players.max}` : "–"],
      ["Server-Version", ping.version || "–"],
      ["Verzeichnis", `${detail.id}/`],
    ];
    // server.properties-Anreicherung (wenn Config schon geladen wurde)
    const props = new Map(state.cfgProps.map((p) => [p.key, p.value]));
    if (props.get("difficulty")) {
      items.splice(5, 0, ["Schwierigkeit", String(props.get("difficulty"))]);
    }
    if (props.get("gamemode")) {
      items.splice(6, 0, ["Spielmodus", String(props.get("gamemode"))]);
    }
    const motd = String(props.get("motd") || ping.motd || "")
      .replace(/\s+/g, " ").trim();
    if (motd) items.push(["MOTD", motd]);
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
      if (k === "Adresse") {
        dd.title = "Klicken zum Kopieren";
        dd.style.cursor = "copy";
        dd.addEventListener("click", () => copyAddress(detail));
      }
      dl.append(dt, dd);
    }
    box.appendChild(dl);
  }

  function renderDetailMods(mods, detail) {
    state.detailMods = mods || [];
    renderModNotes(detail);
    applyModFilter();
    // Papierkorb nur nachladen, wenn der Mods-Bereich schon geladen ist
    if (state.wsLoaded?.has("mods")) loadModTrash();
  }

  // Hinweise über der Mod-Liste: erkannte Probleme + „Neustart nötig“
  function renderModNotes(detail) {
    const problems = Array.isArray(detail?.mod_problems) ? detail.mod_problems : [];
    const list = $("#mods-problems-list");
    list.textContent = "";
    for (const p of problems.slice(0, 8)) {
      const li = document.createElement("li");
      li.textContent = p.message;
      list.appendChild(li);
    }
    if (problems.length > 8) {
      const li = document.createElement("li");
      li.textContent = `… und ${problems.length - 8} weitere (in der Liste markiert)`;
      list.appendChild(li);
    }
    setText($("#mods-problems-title"), problems.length === 1
      ? "1 Problem erkannt:" : `${problems.length} Probleme erkannt:`);
    show($("#mods-problems"), problems.length > 0);

    // Läuft der Server und wurde der mods-Ordner nach dem Start geändert
    // (Installieren, Löschen, An/Aus), wirkt das erst nach einem Neustart
    const started = Date.parse(detail?.container?.started_at || "");
    const changed = Number(detail?.mods_changed_at) * 1000;
    const pending = !!detail?.container?.running && Number.isFinite(started)
      && Number.isFinite(changed) && changed > started + 30000;
    show($("#mods-restart-note"), pending);
  }

  // Lesbarer Anzeigename: Version-/Loader-Suffixe aus dem Dateinamen entfernen
  // ("jei-1.21.1-forge-19.21.1.310.jar" → "jei"), voller Name bleibt im Tooltip
  function modDisplayName(filename) {
    const base = filename.replace(/\.jar(\.disabled)?$/i, "");
    const parts = base.split(/[-_+]+/).filter(Boolean);
    if (!parts.length) return base.slice(0, 32);
    const isNoise = (p) => /^v?\d/.test(p)
      || /^(mc|forge|fabric|neoforge|quilt|universal|client|server|api|all|snapshot)$/i.test(p);
    const out = [parts[0]];
    if (parts.length > 1 && !isNoise(parts[1])
        && parts[0].length + parts[1].length <= 30) {
      out.push(parts[1]);
    }
    const name = out.join("-");
    return name.length >= 2 ? name : base.slice(0, 32);
  }

  function modLetterColor(name) {
    let h = 0;
    for (const c of name) h = (h * 31 + c.charCodeAt(0)) >>> 0;
    return `hsl(${h % 360} 30% 32%)`;
  }

  // Kompakte, filterbare Mod-Liste: Zusammenfassung + Zeilen
  function applyModFilter() {
    const list = $("#detail-mods");
    list.textContent = "";
    const all = state.detailMods;
    const q = ($("#mods-filter-input").value || "").trim().toLowerCase();
    const mods = q ? all.filter((m) => [m.filename, m.meta?.name, m.meta?.mod_id]
      .some((v) => (v || "").toLowerCase().includes(q))) : all;

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
      const meta = mod.meta || null;
      const display = meta?.name || modDisplayName(mod.filename);
      const nameEl = node.querySelector(".mod-name");
      nameEl.textContent = display;
      // Tooltip: Beschreibung, Mod-ID und voller Dateiname
      nameEl.title = [meta?.description, meta?.mod_id ? `Mod-ID: ${meta.mod_id}` : "",
        mod.filename].filter(Boolean).join("\n");
      const tags = node.querySelector(".mod-tags");
      const problems = Array.isArray(mod.problems) ? mod.problems : [];
      const tagLabels = {
        missing_dependency: "Abhängigkeit fehlt",
        disabled_dependency: "Abhängigkeit aus",
        wrong_loader: "falscher Loader",
        client_only: "nur Client",
        duplicate: "doppelt",
      };
      for (const type of new Set(problems.map((p) => p.type))) {
        const tag = document.createElement("span");
        tag.className = "mod-tag is-err";
        tag.textContent = tagLabels[type] || "Problem";
        tag.title = problems.filter((p) => p.type === type).map((p) => p.message).join("\n");
        tags.appendChild(tag);
      }
      if (problems.length) node.querySelector(".mod-row").classList.add("has-problem");
      // Icon: Logo aus der .jar (Backend-Endpoint); ohne Logo Platzhalter-
      // kachel mit den Anfangsbuchstaben in stabiler Zufallsfarbe
      const img = node.querySelector(".mod-icon");
      const letter = node.querySelector(".mod-letter");
      letter.textContent = display.replace(/[^A-Za-z0-9]/g, "").slice(0, 2)
        .toUpperCase() || "?";
      letter.style.background = modLetterColor(mod.filename);
      img.addEventListener("load", () => {
        img.classList.add("loaded");
        letter.classList.add("hidden");
      });
      img.addEventListener("error", () => {
        img.classList.remove("loaded");
        letter.classList.remove("hidden");
      });
      img.src = `/api/instances/${state.detailId}/mods/${encodeURIComponent(mod.filename)}/icon`;
      const updateItem = state.detailUpdates?.[mod.filename];
      const updateAvailable = updateItem?.status === "update_available";
      setText(node.querySelector(".mod-meta"),
        `${meta?.version ? `${meta.version} · ` : ""}${fmtBytes(mod.size_bytes)}`
        + `${mod.enabled ? "" : " · deaktiviert"}`
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
        if (!(await confirmDialog({
          title: `„${display}“ löschen?`,
          message: "Die Datei kommt in den Papierkorb und lässt sich dort "
            + `${state.trashDays || 7} Tage lang wiederherstellen.`,
          list: [mod.filename],
          ok: "In den Papierkorb",
          danger: true,
        }))) return;
        try {
          await api(`/api/instances/${state.detailId}/mods/${encodeURIComponent(mod.filename)}`,
            { method: "DELETE" });
          toast(`"${display}" in den Papierkorb verschoben.`, "success");
          loadDetail();
        } catch (e) {
          toast(`Löschen fehlgeschlagen: ${e.message}`, "error");
        }
      });
      list.appendChild(node);
    }
  }

  /* ---------- Mod-Papierkorb ---------- */
  async function loadModTrash() {
    const id = state.detailId;
    if (!id) return;
    let data;
    try {
      data = await api(`/api/instances/${id}/mod-trash`);
    } catch (e) {
      show($("#mods-trash"), false);
      return;
    }
    if (state.detailId !== id) return;
    const entries = data.entries || [];
    setText($("#mods-trash-count"), `(${entries.length})`);
    state.trashDays = data.keep_days ?? 7;
    setText($("#mods-trash-days"), String(state.trashDays));
    show($("#mods-trash"), entries.length > 0);
    const list = $("#mods-trash-list");
    list.textContent = "";
    for (const entry of entries) {
      const li = document.createElement("li");
      li.className = "mod-row";
      const info = document.createElement("div");
      info.className = "mod-info";
      const name = document.createElement("span");
      name.className = "mod-name";
      name.textContent = entry.filename;
      name.title = entry.filename;
      const meta = document.createElement("span");
      meta.className = "mod-meta muted small";
      const when = new Date(entry.trashed_at * 1000).toLocaleString("de-DE",
        { dateStyle: "short", timeStyle: "short" });
      meta.textContent = `${entry.reason === "update" ? "vor Update gesichert" : "gelöscht"}`
        + ` · ${when} · ${fmtBytes(entry.size_bytes)}`;
      info.append(name, meta);
      const btn = document.createElement("button");
      btn.className = "btn small-btn admin-only";
      btn.textContent = "Wiederherstellen";
      btn.addEventListener("click", () => restoreModFromTrash(entry, btn));
      const row = document.createElement("div");
      row.className = "row";
      row.appendChild(btn);
      li.append(info, row);
      list.appendChild(li);
    }
  }

  async function restoreModFromTrash(entry, btn) {
    const id = state.detailId;
    if (!id) return;
    btn.disabled = true;
    try {
      await api(`/api/instances/${id}/mod-trash/${encodeURIComponent(entry.id)}/restore`,
        { method: "POST" });
      toast(`"${entry.filename}" wiederhergestellt.`, "success");
      loadDetail();
    } catch (e) {
      btn.disabled = false;
      const hint = e.status === 409
        ? " Eine Datei mit diesem Namen liegt schon im mods-Ordner." : "";
      toast(`Wiederherstellen fehlgeschlagen: ${e.message}${hint}`, "error");
    }
  }

  $("#mods-trash-empty").addEventListener("click", async () => {
    const id = state.detailId;
    if (!id || !(await confirmDialog({
      title: "Papierkorb leeren?",
      message: "Die Dateien werden endgültig gelöscht und lassen sich nicht wiederherstellen.",
      ok: "Endgültig löschen",
      danger: true,
    }))) return;
    try {
      const data = await api(`/api/instances/${id}/mod-trash`, { method: "DELETE" });
      toast(`${data.removed} Datei(en) endgültig gelöscht.`, "success");
      loadModTrash();
    } catch (e) {
      toast(`Leeren fehlgeschlagen: ${e.message}`, "error");
    }
  });

  $("#mods-restart-btn").addEventListener("click", async () => {
    const detail = state.detailData;
    if (!detail) return;
    await instAction({ id: detail.id, name: detail.name }, "restart");
    loadDetail();
  });

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
      updateHotbarCounts();
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
      const done = await pollProgress(job, {
        box: $("#mods-update-progress"),
        bar: $("#mods-update-bar"),
        phase: $("#mods-update-phase"),
        pct: $("#mods-update-pct"),
      });
      Object.assign(job, done || {});
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
      const atBottom = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 8;
      const fresh = data.logs.length ? data.logs.join("\n") : "(keine Logs)";
      if (pre.textContent !== fresh) {
        pre.textContent = fresh;
        rebuildActivity(data.logs);
      }
      if (atBottom) pre.scrollTop = pre.scrollHeight;
    } catch (e) {
      pre.textContent = `Logs nicht abrufbar: ${e.message}`;
    }
  }
  $("#detail-logs-reload").addEventListener("click", loadDetailLogs);

  /* ---------- Statistik-Verlauf (persistente Charts) ---------- */
  const CHART_COLORS = ["#4ade80", "#60a5fa", "#2dd4bf", "#fbbf24",
    "#a855f7", "#fda4af", "#ef4444", "#94a3b8"];

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
    renderChart($("#chart-cpu"), [{ values: points.map((p) => p.cpu), color: "#4ade80" }]);
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

  /* ---------- Modpack-Upload (eigener Tab: Ablegen → Vorschau → Installieren) ---------- */
  // Andere Stellen (Server-Detail, Modpack-Tab) springen hierher und wählen
  // das Ziel vor; der Upload läuft nur noch an dieser einen Stelle.
  function openPackUpload(instanceId) {
    state.upPresetTarget = instanceId || "";
    activateTab("upload");
  }
  $("#pack-upload-goto").addEventListener("click", () => openPackUpload(state.detailId));

  const upEls = {
    drop: $("#up-drop"),
    file: $("#up-file"),
    btn: $("#up-install-btn"),
    reset: $("#up-reset-btn"),
    analyze: { box: $("#up-analyze"), bar: $("#up-analyze-bar"),
               phase: $("#up-analyze-phase"), pct: $("#up-analyze-pct") },
    preview: $("#up-preview"),
    pvTitle: $("#up-pv-title"),
    pvFacts: $("#up-pv-facts"),
    pvWarnings: $("#up-pv-warnings"),
    pvErrors: $("#up-pv-errors"),
    target: $("#up-target-select"),
    existingOpts: $("#up-existing-opts"),
    newOpts: $("#up-new-opts"),
    autoVersion: $("#up-auto-version"),
    force: $("#up-force"),
    name: $("#up-name"),
    memory: $("#up-memory"),
    memoryHint: $("#up-memory-hint"),
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
    retry: $("#up-retry-btn"),
  };
  state.upAnalysis = null;   // Antwort von /api/modpacks/analyze (mit token)
  state.upBusy = false;
  state.upRetryInstance = null;

  const PACK_FORMATS = {
    serverpack: "CurseForge „Server Files“",
    curseforge: "CurseForge-Export (manifest.json)",
    modrinth: "Modrinth (.mrpack)",
  };

  async function loadUploadTargets() {
    try {
      const data = await api("/api/instances");
      const options = data.instances.map((i) => ({
        value: i.id, label: `${i.name} (${i.loader} ${i.game_version})`,
      }));
      const current = state.upPresetTarget ?? upEls.target.value;
      state.upPresetTarget = undefined;
      fillSelect(upEls.target, options, "Neuen Server aus dem Pack erstellen");
      upEls.target.value = options.some((o) => o.value === current) ? current : "";
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

  function resetUpload() {
    state.upAnalysis = null;
    upEls.file.value = "";
    show(upEls.preview, false);
    show(upEls.analyze.box, false);
    show(upEls.drop, true);
    upEls.btn.disabled = true;
  }
  upEls.reset.addEventListener("click", () => {
    resetUpload();
    show(upEls.error, false);
    show(upEls.summary, false);
  });

  function showPreview(a) {
    setText(upEls.pvTitle, a.version ? `${a.title} ${a.version}` : a.title);
    const facts = [
      ["Datei", `${a.filename} · ${fmtBytes(a.size)}`],
      ["Typ", PACK_FORMATS[a.format] || a.format],
      ["Minecraft", a.game_version || "nicht erkannt"],
      ["Loader", a.loader
        ? `${a.loader}${a.loader_version ? ` ${a.loader_version}` : ""}` : "nicht erkannt"],
      ["Java", a.java ? `Java ${a.java} (automatisch)` : "–"],
      ["Mods", String(a.mods)],
      ["Empfohlener RAM", a.recommended_memory || `Standard (${a.default_memory})`],
    ];
    if (a.detected_by) facts.push(["Erkannt über", a.detected_by]);
    upEls.pvFacts.textContent = "";
    for (const [k, v] of facts) {
      const dt = document.createElement("dt");
      dt.textContent = k;
      const dd = document.createElement("dd");
      dd.textContent = v; // textContent schützt vor XSS (Werte aus dem Archiv)
      upEls.pvFacts.append(dt, dd);
    }
    upEls.pvWarnings.textContent = "";
    for (const w of a.warnings || []) {
      const li = document.createElement("li");
      li.textContent = w;
      upEls.pvWarnings.appendChild(li);
    }
    show(upEls.pvWarnings, (a.warnings || []).length > 0);
    setText(upEls.pvErrors, (a.errors || []).join(" "));
    show(upEls.pvErrors, (a.errors || []).length > 0);
    upEls.name.placeholder = a.title || "wird aus dem Pack abgeleitet";
    setText(upEls.memory.options[0], `Standard (${a.default_memory})`);
    upEls.memory.value = a.recommended_memory || "";
    if (a.recommended_memory) {
      setText(upEls.memoryHint,
        `Vorgeschlagen für ${a.mods} Mods: ${a.recommended_memory} — bei Bedarf ändern.`);
    }
    upEls.btn.disabled = (a.errors || []).length > 0;
    show(upEls.drop, false);
    show(upEls.preview, true);
  }

  async function analyzePackFile(file) {
    if (state.upBusy) return;
    show(upEls.error, false);
    show(upEls.summary, false);
    show(upEls.preview, false);
    const problem = await packFileProblem(file);
    if (problem) {
      setText(upEls.error, problem);
      show(upEls.error, true);
      upEls.file.value = "";
      return;
    }
    const { box, bar, phase, pct } = upEls.analyze;
    state.upBusy = true;
    show(box, true);
    bar.style.width = "0%";
    setText(phase, `Lade ${file.name} hoch…`);
    setText(pct, "");
    try {
      const form = new FormData();
      form.append("file", file);
      const data = await apiUpload("/api/modpacks/analyze", form, (p) => {
        bar.style.width = p + "%";
        setText(pct, `${p} %`);
        if (p >= 100) setText(phase, "Pack wird analysiert…");
      });
      state.upAnalysis = data;
      showPreview(data);
    } catch (e) {
      setText(upEls.error, `Pack konnte nicht gelesen werden: ${e.message}`);
      show(upEls.error, true);
      resetUpload();
    } finally {
      state.upBusy = false;
      show(box, false);
    }
  }

  upEls.file.addEventListener("change", () => {
    const file = upEls.file.files[0];
    if (file) analyzePackFile(file);
  });
  // Drag & Drop auf die Ablagefläche
  for (const ev of ["dragenter", "dragover"]) {
    upEls.drop.addEventListener(ev, (e) => {
      e.preventDefault();
      upEls.drop.classList.add("dragover");
    });
  }
  for (const ev of ["dragleave", "drop"]) {
    upEls.drop.addEventListener(ev, () => upEls.drop.classList.remove("dragover"));
  }
  upEls.drop.addEventListener("drop", (e) => {
    e.preventDefault();
    const file = e.dataTransfer?.files?.[0];
    if (file) analyzePackFile(file);
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

  function showUploadSummary(text, skipped, failed, retryInstance = null) {
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
    state.upRetryInstance = retryInstance;
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
      setText(upEls.failedText, retryInstance
        ? `${failed.length} Mod(s) konnten nicht geladen werden — der Rest ist installiert:`
        : "Nicht installiert:");
      show(upEls.failedBox, true);
    } else {
      show(upEls.failedBox, false);
    }
    show(upEls.retry, !!retryInstance);
    show(upEls.summary, true);
  }

  async function runUploadJob(job, label, instanceId) {
    const result = await pollUploadJob(job, upEls.progress);
    const summary = result?.summary || {};
    const failed = summary.failed || [];
    showUploadSummary(
      `${label} · ${summary.files ?? "?"} Dateien installiert` +
        (failed.length ? `, ${failed.length} fehlen.` : "."),
      summary.skipped || [], failed, failed.length ? instanceId : null);
    return result;
  }

  upEls.btn.addEventListener("click", async () => {
    const a = state.upAnalysis;
    if (!a || state.upBusy) return;
    show(upEls.error, false);
    show(upEls.summary, false);
    const instanceId = upEls.target.value;
    if (!instanceId && !upEls.eula.checked) {
      setText(upEls.error, "Bitte die Minecraft-EULA akzeptieren.");
      show(upEls.error, true);
      return;
    }
    state.upBusy = true;
    upEls.btn.disabled = true;
    upEls.btn.textContent = "Starte…";
    const sendExisting = (force) => {
      const form = new FormData();
      form.append("staged", a.token);
      if (force) form.append("force", "true");
      if (!upEls.autoVersion.checked) form.append("auto_version", "false");
      return apiUpload(`/api/instances/${instanceId}/modpacks/upload`, form);
    };
    try {
      let job;
      let label;
      let targetId = instanceId;
      if (instanceId) {
        try {
          job = await sendExisting(upEls.force.checked);
        } catch (e) {
          if (e.status !== 409) throw e;
          if (/läuft bereits/i.test(e.message || "")) throw e;
          const ok = await confirmDialog({
            title: "Modpack ersetzen?", message: e.message,
            ok: "Ersetzen & neu installieren", danger: true });
          if (!ok) return;
          job = await sendExisting(true);
        }
        label = `Instanz aktualisiert mit „${a.title}“`;
      } else {
        const form = new FormData();
        form.append("staged", a.token);
        const name = upEls.name.value.trim();
        if (name) form.append("name", name);
        const memory = upEls.memory.value;
        if (memory) form.append("memory", memory);
        form.append("accept_eula", "true");
        const created = await apiUpload("/api/instances/from-pack-upload", form);
        job = { job_id: created.job.id };
        targetId = created.instance.id;
        label = `Neuer Server „${created.instance.name}“ (Port ${created.instance.port}, `
          + `${created.instance.memory || a.default_memory} RAM)`;
      }
      resetUpload();
      show(upEls.drop, false);
      await runUploadJob(job, label, targetId);
      toast(`Modpack „${a.title}“ installiert.`, "success");
    } catch (e) {
      setText(upEls.error, `Installation fehlgeschlagen: ${e.message}`);
      show(upEls.error, true);
      const skipped = e.summary?.skipped || [];
      if (e.failed?.length || skipped.length) {
        showUploadSummary(
          `Fehlgeschlagen — installiert: ${e.summary?.files ?? 0} Dateien` +
          (skipped.length ? ` · übersprungen: ${skipped.length}` : "") + ".",
          skipped, e.failed || []);
      }
      // Hochgeladene Datei ist nach einem Fehler weg → neu ablegen
      if (e.status === 404) resetUpload();
    } finally {
      state.upBusy = false;
      upEls.btn.textContent = "Installieren";
      upEls.btn.disabled = !state.upAnalysis || (state.upAnalysis.errors || []).length > 0;
      if (!state.upAnalysis) show(upEls.drop, true);
    }
  });

  upEls.retry.addEventListener("click", async () => {
    const instanceId = state.upRetryInstance;
    if (!instanceId || state.upBusy) return;
    state.upBusy = true;
    upEls.retry.disabled = true;
    show(upEls.error, false);
    try {
      const job = await api(`/api/instances/${instanceId}/modpacks/retry-failed`,
        { method: "POST" });
      show(upEls.summary, false);
      const result = await runUploadJob(job, "Erneuter Versuch", instanceId);
      if (!(result?.summary?.failed || []).length) {
        toast("Alle fehlenden Mods nachgeladen.", "success");
      }
    } catch (e) {
      setText(upEls.error, `Erneuter Versuch fehlgeschlagen: ${e.message}`);
      show(upEls.error, true);
    } finally {
      state.upBusy = false;
      upEls.retry.disabled = false;
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
    // cfg: {mode: "pack", hit} (Uploads laufen über den Upload-Tab)
    activateTab("modpacks");
    state.mpCreate = cfg;
    show($("#mp-create-error"), false);
    show($("#mp-create"), true);
    const info = $("#mp-create-info");
    info.textContent = "";
    const vSel = $("#mp-create-version");
    fillSelect(vSel, [], "Neueste (Standard)");
    vSel.disabled = true;
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
    const name = $("#mp-create-name").value.trim() || null;
    const memory = $("#mp-create-memory").value || null;
    btn.disabled = true;
    btn.textContent = "Erstelle…";
    let created = null;
    let jobId = null;
    try {
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

  $("#mp-upload-goto").addEventListener("click", () => {
    openPackUpload(mpTarget()?.instanceId || "");
  });
  /* ---------- Backups je Instanz ---------- */
  async function loadBackups() {
    if (!state.detailId) return;
    show($("#backup-error"), false);
    const list = $("#backup-list");
    try {
      const data = await api(`/api/instances/${state.detailId}/backups`);
      list.textContent = "";
      const loc = $("#backup-location");
      if (data.location) {
        setText(loc, data.external
          ? `Ablage: ${data.location} (eigene Platte)`
          : `Ablage: ${data.location}, auf derselben Platte wie die Server. Fällt sie aus, `
            + "sind auch die Backups weg. Abhilfe: DASHBOARD_BACKUPS_DIR in der .env auf "
            + "eine zweite Platte setzen oder Backups herunterladen.");
      }
      show(loc, !!data.location);
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
      renderBackupMini(data.backups);
    } catch (e) {
      show($("#backup-empty"), false);
      setText($("#backup-error"), `Backups nicht abrufbar: ${e.message}`);
      show($("#backup-error"), true);
    }
  }

  /* ---------- Backup-Kompaktpanel im Workspace-Kopf ---------- */
  function renderBackupMini(backups) {
    const list = $("#bk-mini-list");
    if (!list) return;
    list.textContent = "";
    const all = backups || [];
    const total = all.reduce((s, b) => s + (b.size_bytes || 0), 0);
    setText($("#bk-mini-summary"), all.length
      ? `${all.length} Backup${all.length === 1 ? "" : "s"} · ${fmtBytes(total)} gesamt`
      : "Noch keine Backups vorhanden.");
    for (const b of all.slice(0, 4)) {
      const li = document.createElement("li");
      li.className = "mod-row";
      const info = document.createElement("div");
      info.className = "mod-info";
      const nm = document.createElement("span");
      nm.className = "mod-name";
      nm.textContent = b.name;
      const meta = document.createElement("span");
      meta.className = "mod-meta muted small";
      meta.textContent = `${fmtBytes(b.size_bytes)} · ${fmtDate(b.created)}`;
      info.append(nm, meta);
      const rb = document.createElement("button");
      rb.className = "btn";
      rb.textContent = "Wiederherstellen";
      rb.addEventListener("click", () => restoreBackup(b.name));
      li.append(info, rb);
      list.appendChild(li);
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
  /* ---------- Welten je Instanz: Liste, neu, wechseln, kopieren, … ---------- */
  function worldError(msg) {
    setText($("#world-error"), msg || "");
    show($("#world-error"), !!msg);
  }

  async function worldAction(path, body, okText) {
    worldError("");
    try {
      const res = await api(`/api/instances/${state.detailId}/worlds${path}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (okText) toast(typeof okText === "function" ? okText(res) : okText, "success");
      await loadWorlds();
      return res;
    } catch (e) {
      worldError(e.message);
      return null;
    }
  }

  async function downloadNamedWorld(name, btn) {
    if (btn) btn.disabled = true;
    try {
      const res = await fetch(
        `/api/instances/${state.detailId}/worlds/download?name=${encodeURIComponent(name)}`,
        { headers: state.apiKey ? { "X-API-Key": state.apiKey } : {} });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const blob = await res.blob();
      const cd = res.headers.get("content-disposition") || "";
      const match = cd.match(/filename="?([^";]+)"?/);
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = match ? match[1] : `${name}.zip`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      toast(`Welt-Download fehlgeschlagen: ${e.message}`, "error");
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  function askWorldName(title, current) {
    const name = window.prompt(title, current || "");
    return name === null ? null : name.trim() || null;
  }

  async function loadWorlds() {
    if (!state.detailId) return;
    const id = state.detailId;
    let data;
    try {
      data = await api(`/api/instances/${id}/worlds`);
    } catch (e) {
      worldError(`Welten nicht abrufbar: ${e.message}`);
      return;
    }
    if (state.detailId !== id) return;
    const running = !!state.detailRunning;
    const list = $("#worlds-list");
    list.textContent = "";
    const worlds = data.worlds || [];
    const real = worlds.filter((w) => !w.pending).length;
    setText($("#worlds-count"), real ? `(${real})` : "");
    setText($("#world-info"), running
      ? "Server läuft — Wechseln, Anlegen und Umbenennen der aktiven Welt gehen nur bei gestopptem Server."
      : "Aktiv ist die markierte Welt. Ein Wechsel wirkt beim nächsten Start.");
    for (const w of worlds) {
      const li = document.createElement("li");
      li.className = "world-row" + (w.active ? " active" : "");
      const img = document.createElement("img");
      img.src = SLOT_ICONS.globe;
      img.alt = "";
      const info = document.createElement("div");
      info.className = "world-info";
      const head = document.createElement("div");
      head.className = "world-head";
      const nm = document.createElement("span");
      nm.className = "world-name";
      nm.textContent = w.name;
      nm.title = w.name;
      head.appendChild(nm);
      if (w.active) {
        const b = document.createElement("span");
        b.className = `badge ${w.pending ? "starting" : "on"}`;
        b.textContent = w.pending ? "neu · entsteht beim Start" : "aktiv";
        head.appendChild(b);
      }
      const meta = document.createElement("span");
      meta.className = "muted small";
      const parts = [];
      if (!w.pending) parts.push(fmtBytes(w.size_bytes));
      if (w.modified) parts.push(`zuletzt gespielt ${fmtDate(w.modified)}`);
      if (w.dimensions?.length) parts.push("inkl. Nether/End");
      meta.textContent = parts.join(" · ");
      info.append(head, meta);

      const actions = document.createElement("div");
      actions.className = "world-actions";
      const mk = (label, title, fn, opts = {}) => {
        const b = document.createElement("button");
        b.className = `btn small-btn${opts.cls ? ` ${opts.cls}` : ""}${opts.admin === false ? "" : " admin-only"}`;
        b.textContent = label;
        b.title = title;
        b.disabled = !!opts.disabled;
        b.addEventListener("click", () => fn(b));
        actions.appendChild(b);
        return b;
      };
      if (!w.active) {
        mk("Aktivieren", running ? "Erst den Server stoppen" : "Diese Welt beim nächsten Start laden",
          () => worldAction("/activate", { name: w.name }, `Welt „${w.name}“ ist aktiv — wirkt beim nächsten Start.`),
          { cls: "primary", disabled: running });
      }
      if (!w.pending) {
        mk("Download", "Als .zip herunterladen", (b) => downloadNamedWorld(w.name, b), { admin: false });
        mk("Kopieren", w.active && running ? "Aktive Welt nur bei gestopptem Server kopieren" : "Kopie unter neuem Namen anlegen",
          () => {
            const nn = askWorldName(`Name der Kopie von „${w.name}“:`, `${w.name}-kopie`);
            if (nn) worldAction("/copy", { name: w.name, new_name: nn }, `Kopie „${nn}“ angelegt.`);
          }, { disabled: w.active && running });
        mk("Umbenennen", w.active && running ? "Aktive Welt nur bei gestopptem Server umbenennen" : "Welt umbenennen",
          () => {
            const nn = askWorldName(`Neuer Name für „${w.name}“:`, w.name);
            if (nn && nn !== w.name) worldAction("/rename", { name: w.name, new_name: nn }, `Umbenannt in „${nn}“.`);
          }, { disabled: w.active && running });
      }
      if (!w.active) {
        mk("Löschen", "Welt endgültig löschen", () => {
          if (!window.confirm(`Welt „${w.name}“ endgültig löschen?\n\nDas kann nicht rückgängig gemacht werden — vorher ggf. herunterladen oder ein Backup machen.`)) return;
          worldAction("/delete", { name: w.name }, `Welt „${w.name}“ gelöscht.`);
        }, { cls: "danger" });
      }
      li.append(img, info, actions);
      list.appendChild(li);
    }
    $("#world-new-btn").disabled = running;
    updateWorldUploadBtn();
  }

  // Wird beim Öffnen einer Instanz und beim Wechsel in den Welt-Slot aufgerufen
  function loadWorldInfo() {
    worldError("");
    loadWorlds();
    loadMapStatus();
  }
  $("#world-reload").addEventListener("click", loadWorldInfo);

  $("#world-new-btn").addEventListener("click", async () => {
    const name = $("#world-new-name").value.trim();
    if (!name) { worldError("Bitte einen Namen für die neue Welt eingeben."); return; }
    const res = await worldAction("", {
      name, seed: $("#world-new-seed").value.trim(), level_type: $("#world-new-type").value,
    }, `Welt „${name}“ angelegt — sie entsteht beim nächsten Start.`);
    if (res) {
      $("#world-new-name").value = "";
      $("#world-new-seed").value = "";
      $("#world-new-box").open = false;
    }
  });

  function updateWorldUploadBtn() {
    const activate = $("#world-import-activate").checked;
    $("#world-upload-btn").disabled = !$("#world-upload-file").files.length
      || (activate && state.detailRunning);
  }
  $("#world-upload-file").addEventListener("change", () => { updateWorldUploadBtn(); worldError(""); });
  $("#world-import-activate").addEventListener("change", updateWorldUploadBtn);
  $("#world-upload-btn").addEventListener("click", async () => {
    const file = $("#world-upload-file").files[0];
    if (!file || !state.detailId) return;
    const form = new FormData();
    form.append("file", file);
    form.append("name", $("#world-import-name").value.trim());
    form.append("activate", $("#world-import-activate").checked ? "true" : "false");
    const btn = $("#world-upload-btn");
    btn.disabled = true;
    worldError("");
    try {
      const result = await apiUpload(`/api/instances/${state.detailId}/worlds/import`, form,
        (pct) => { btn.textContent = `Lade hoch… ${pct} %`; });
      toast(result.active
        ? `Welt „${result.name}“ importiert und aktiviert.`
        : `Welt „${result.name}“ importiert.`, "success");
      $("#world-upload-file").value = "";
      $("#world-import-name").value = "";
      $("#world-import-activate").checked = false;
      loadWorlds();
    } catch (e) {
      worldError(`Import fehlgeschlagen: ${e.message}`);
    } finally {
      btn.textContent = "Importieren";
      updateWorldUploadBtn();
    }
  });

  /* ---------- Live-Karte (BlueMap) ---------- */
  function mapError(msg) {
    setText($("#map-error"), msg || "");
    show($("#map-error"), !!msg);
  }

  // Karte kommt über den Proxy des Dashboards (nur mit Login erreichbar)
  function mapUrl(id) {
    return `/api/instances/${encodeURIComponent(id)}/map/view/`;
  }

  function resetMapFrame() {
    const frame = $("#map-frame");
    if (frame.getAttribute("src")) frame.removeAttribute("src");
    show($("#map-frame-wrap"), false);
  }

  async function loadMapStatus() {
    if (!state.detailId) return;
    const id = state.detailId;
    let st;
    try {
      st = await api(`/api/instances/${id}/map`);
    } catch (e) {
      mapError(`Karten-Status nicht abrufbar: ${e.message}`);
      return;
    }
    if (state.detailId !== id) return;
    state.mapStatus = st;
    mapError("");
    const info = $("#map-info");
    show($("#map-setup"), st.supported && !st.enabled);
    show($("#map-disable"), st.enabled);
    show($("#map-restart"), false);
    const open = $("#map-open");
    show(open, !!(st.enabled && st.reachable));
    open.href = mapUrl(id);
    if (!st.supported) {
      setText(info, "Für diesen Server-Typ nicht verfügbar (braucht Fabric, Quilt, Forge, NeoForge, Paper, Spigot oder Bukkit).");
    } else if (!st.enabled) {
      setText(info, "Zeigt die Welt als 3D-Karte im Browser — mit Spielern in Echtzeit. BlueMap wird als "
        + (st.kind === "plugin" ? "Plugin" : "Mod") + " installiert und rendert im Hintergrund (kostet etwas CPU).");
    } else if (!st.server_running) {
      setText(info, "Aktiv — startet mit dem Server.");
    } else if (!st.reachable) {
      setText(info, "Server läuft, aber die Karte antwortet noch nicht. "
        + "Nach dem Aktivieren einmal neu starten — beim ersten Start lädt BlueMap außerdem die Texturen, das dauert kurz.");
      show($("#map-restart"), true);
    } else {
      setText(info, "Läuft. Das erste Rendern dauert je nach Weltgröße eine Weile — die Karte füllt sich nach und nach.");
    }
    // Einbetten nur, wenn erreichbar und der Welt-Slot sichtbar ist
    const embed = st.enabled && st.reachable && state.wsTab === "welt";
    if (embed) {
      const frame = $("#map-frame");
      const url = mapUrl(id);
      if (frame.getAttribute("src") !== url) frame.setAttribute("src", url);
      show($("#map-frame-wrap"), true);
    } else {
      resetMapFrame();
    }
  }
  $("#map-reload").addEventListener("click", loadMapStatus);
  $("#map-accept").addEventListener("change", () => {
    $("#map-enable").disabled = !$("#map-accept").checked;
  });
  $("#map-enable").addEventListener("click", async () => {
    const btn = $("#map-enable");
    btn.disabled = true;
    btn.textContent = "Installiere BlueMap…";
    mapError("");
    try {
      await api(`/api/instances/${state.detailId}/map`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: true, accept_download: $("#map-accept").checked }),
      });
      toast(state.detailRunning
        ? "Live-Karte installiert — wirkt nach einem Neustart des Servers."
        : "Live-Karte installiert — sie startet mit dem Server.", "success");
      $("#map-accept").checked = false;
      await loadMapStatus();
      if (state.detailRunning) show($("#map-restart"), true);
    } catch (e) {
      mapError(`Aktivieren fehlgeschlagen: ${e.message}`);
    } finally {
      btn.textContent = "Karte aktivieren";
      btn.disabled = !$("#map-accept").checked;
    }
  });
  $("#map-disable").addEventListener("click", async () => {
    if (!window.confirm("Live-Karte deaktivieren?\n\nBlueMap wird entfernt (wirkt nach Neustart). Bereits gerenderte Kartendaten bleiben erhalten.")) return;
    mapError("");
    try {
      await api(`/api/instances/${state.detailId}/map`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: false }),
      });
      toast("Live-Karte deaktiviert — wirkt nach dem nächsten Neustart.", "success");
      resetMapFrame();
      loadMapStatus();
    } catch (e) {
      mapError(`Deaktivieren fehlgeschlagen: ${e.message}`);
    }
  });
  $("#map-restart").addEventListener("click", async () => {
    const inst = currentDetailInst() || state.detailData;
    if (!inst) return;
    await instAction(inst, "restart");
    setTimeout(loadMapStatus, 20000); // BlueMap braucht nach dem Start etwas
  });

  /* ---------- Server-Icon je Instanz (server-icon.png) ---------- */
  function iconSetError(msg) {
    setText($("#icon-error"), msg);
    show($("#icon-error"), !!msg);
  }

  async function loadIconPreview() {
    if (!state.detailId) return;
    const img = $("#icon-preview");
    const del = $("#icon-delete-btn");
    URL.revokeObjectURL(img.src || "");
    img.removeAttribute("src");
    img.classList.add("hidden");
    del.classList.add("hidden");
    setText($("#icon-info"), "–");
    iconSetError("");
    try {
      const headers = {};
      if (state.apiKey) headers["X-API-Key"] = state.apiKey;
      const id = state.detailId;
      const res = await fetch(`/api/instances/${id}/icon`, { headers });
      if (state.detailId !== id) return; // inzwischen andere Instanz geöffnet
      if (res.status === 401) showAuthOverlay();
      if (res.status === 404) {
        setText($("#icon-info"), "Kein Icon vorhanden (64×64-PNG hochladen).");
        const name = state.detailData?.name || currentDetailInst()?.name || state.detailId;
        $("#detail-icon").src = serverBlockIcon(name);
        return;
      }
      if (!res.ok) throw Object.assign(new Error(`HTTP-Fehler ${res.status}`),
                                       { status: res.status });
      const blob = await res.blob();
      if (state.detailId !== id) return;
      const url = URL.createObjectURL(blob);
      img.src = url;
      $("#detail-icon").src = url; // echtes server-icon.png auch im Kopf zeigen
      img.classList.remove("hidden");
      del.classList.remove("hidden");
      setText($("#icon-info"), `64×64 PNG · ${fmtBytes(blob.size)}`);
    } catch (e) {
      setText($("#icon-info"), "–");
      iconSetError(`Icon nicht abrufbar: ${e.message}`);
    }
  }

  $("#icon-reload").addEventListener("click", loadIconPreview);

  $("#icon-upload-file").addEventListener("change", () => {
    $("#icon-upload-btn").disabled = !$("#icon-upload-file").files.length;
    iconSetError("");
  });

  $("#icon-upload-btn").addEventListener("click", async () => {
    const file = $("#icon-upload-file").files[0];
    if (!file || !state.detailId) return;
    if (file.size > 1024 * 1024) {
      iconSetError("Icon zu groß (max 1 MiB).");
      return;
    }
    const form = new FormData();
    form.append("file", file);
    const btn = $("#icon-upload-btn");
    btn.disabled = true;
    btn.textContent = "Lade hoch…";
    iconSetError("");
    try {
      await apiUpload(`/api/instances/${state.detailId}/icon`, form, null, "PUT");
      toast("Server-Icon gesetzt.", "success");
      forgetCardIcon(state.detailId);
      $("#icon-upload-file").value = "";
      $("#icon-upload-btn").disabled = true;
      loadIconPreview();
    } catch (e) {
      iconSetError(`Icon-Upload fehlgeschlagen: ${e.message}`);
    } finally {
      btn.disabled = false;
      btn.textContent = "Icon hochladen";
    }
  });

  $("#icon-delete-btn").addEventListener("click", async () => {
    if (!state.detailId) return;
    if (!window.confirm("Server-Icon entfernen?")) return;
    try {
      await api(`/api/instances/${state.detailId}/icon`, { method: "DELETE" });
      toast("Server-Icon entfernt.", "success");
      forgetCardIcon(state.detailId);
      loadIconPreview();
    } catch (e) {
      iconSetError(`Löschen fehlgeschlagen: ${e.message}`);
    }
  });

  /* ---------- Datei-Browser je Instanz ---------- */
  state.fb = { path: "", entries: [] };

  function fbSetError(msg) { authError("#fb-error", msg); }

  function fbJoin(dir, name) {
    return dir ? `${dir}/${name}` : name;
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
      java: $("#jvm-java").value || "auto",
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

  /* ---------- Schlafmodus je Instanz ---------- */
  function loadHibernate(inst) {
    const hib = inst.hibernate || {};
    $("#hib-mode").value = hib.mode || "off";
    $("#hib-minutes").value = hib.minutes || 30;
    show($("#hib-error"), false);
  }

  $("#hib-save").addEventListener("click", async () => {
    const btn = $("#hib-save");
    btn.disabled = true;
    show($("#hib-error"), false);
    const minutes = Math.min(1440, Math.max(5, parseInt($("#hib-minutes").value, 10) || 30));
    const hibernate = { mode: $("#hib-mode").value, minutes };
    try {
      const inst = await api(`/api/instances/${state.detailId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ hibernate }),
      });
      loadHibernate(inst);
      const cached = (state.instList || []).find((i) => i.id === state.detailId);
      if (cached) cached.hibernate = inst.hibernate;
      toast(state.detailRunning
        ? "Schlafmodus gespeichert, wirksam nach dem nächsten Neustart."
        : "Schlafmodus gespeichert.", "success");
    } catch (e) {
      setText($("#hib-error"), `Speichern fehlgeschlagen: ${e.message}`);
      show($("#hib-error"), true);
    } finally {
      btn.disabled = false;
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
    $("#ws-autostart").checked = !!sched.auto_start;
    const rs = sched.restart || {};
    $("#sched-restart-enabled").checked = !!rs.enabled;
    $("#sched-restart-time").value = rs.time || "04:00";
    $("#sched-restart-warn").value = rs.warn_minutes ?? 5;
    const st = sched.stop || {};
    $("#sched-stop-enabled").checked = !!st.enabled;
    $("#sched-stop-time").value = st.time || "23:00";
    $("#sched-stop-warn").value = st.warn_minutes ?? 5;
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
      stop: {
        enabled: $("#sched-stop-enabled").checked,
        time: $("#sched-stop-time").value || "23:00",
        warn_minutes: schedInt($("#sched-stop-warn").value, 0, 30, 5),
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
    const list = $("#rcon-players");
    try {
      const id = state.detailId;
      // Spielzeit parallel laden — fehlt sie, bleiben die Karten trotzdem nutzbar
      const [data, pt] = await Promise.all([
        api(`/api/instances/${id}/players`),
        api(`/api/players/playtime?hours=all&instance=${encodeURIComponent(id)}`).catch(() => null),
      ]);
      if (state.detailId !== id) return;
      const playtime = new Map((pt?.players || []).map((p) => [p.player, p.seconds]));
      list.textContent = "";
      const names = data.names || [];
      setText(hint, data.online != null
        ? `${data.online} von ${data.max} Slots belegt` : `Antwort: ${data.raw || "?"}`);
      show($("#rcon-players-empty"), names.length === 0);
      for (const name of names) {
        const node = document.createElement("li");
        node.className = "player-card";
        const info = document.createElement("div");
        info.className = "player-card-head";
        const face = document.createElement("img");
        face.src = playerFace(name);
        face.alt = "";
        const nm = document.createElement("span");
        nm.className = "player-card-name";
        nm.textContent = name;
        nm.title = name;
        const txt = document.createElement("div");
        txt.className = "player-card-text";
        const time = document.createElement("span");
        time.className = "player-card-time muted small";
        time.textContent = playtime.has(name)
          ? `Spielzeit ${fmtDuration(playtime.get(name))}` : "Spielzeit –";
        time.title = "Gesamte Spielzeit auf diesem Server (seit Aufzeichnung)";
        txt.append(nm, time);
        info.append(face, txt);
        const btns = document.createElement("div");
        btns.className = "player-card-actions";
        const mkBtn = (label, action, cls = "", title = "") => {
          const b = document.createElement("button");
          b.className = `btn${cls ? ` ${cls}` : ""}`.trim();
          b.textContent = label;
          if (title) b.title = title;
          b.addEventListener("click", (e) => rconAction(action, name, e.target));
          btns.appendChild(b);
        };
        const invBtn = document.createElement("button");
        invBtn.className = "btn small-btn";
        invBtn.textContent = "Inventar";
        invBtn.title = "Inventar ansehen und bearbeiten";
        invBtn.addEventListener("click", () => openInventory(name));
        btns.appendChild(invBtn);
        mkBtn("OP", "op", "small-btn admin-only", "Operator-Rechte geben");
        mkBtn("Kick", "kick", "small-btn admin-only", "Spieler kicken");
        mkBtn("Ban", "ban", "small-btn danger admin-only", "Spieler bannen");
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

  /* ---------- Spieler-Inventar (RCON) ---------- */
  const INV_SLOT_LABEL = {
    "armor.head": "Helm", "armor.chest": "Brustpanzer", "armor.legs": "Hose",
    "armor.feet": "Stiefel", "weapon.offhand": "Offhand",
  };
  const INV_GAMEMODE = { survival: "Überleben", creative: "Kreativ", adventure: "Abenteuer", spectator: "Zuschauer" };
  const inv = { instance: null, player: null, snap: null, tab: "main", selected: null,
    names: new Map(), iconVer: "", iconTimer: null, giveTimer: null, giveId: null, busy: false };

  function invSlotLabel(slot) {
    if (INV_SLOT_LABEL[slot]) return INV_SLOT_LABEL[slot];
    const [kind, num] = slot.split(".");
    const n = Number(num);
    if (kind === "enderchest") return `Endertruhe ${n + 1}`;
    return n < 9 ? `Hotbar ${n + 1}` : `Inventar ${n - 8}`;
  }

  function invPrettyId(id) {
    const path = String(id).split(":").pop().split("/").pop();
    return path.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
  }

  function invName(item) {
    return item.custom_name || inv.names.get(item.id) || invPrettyId(item.id);
  }

  function invIcon(id, size = "") {
    const img = document.createElement("img");
    img.alt = "";
    img.loading = "lazy";
    img.src = `/api/instances/${encodeURIComponent(inv.instance)}/items/icon?id=${encodeURIComponent(id)}&v=${inv.iconVer}`;
    img.addEventListener("error", () => {
      const fb = document.createElement("span");
      fb.className = "inv-fb";
      fb.textContent = invPrettyId(id).replace(/[^A-Za-z0-9 ]/g, "").split(" ")
        .filter(Boolean).slice(0, 2).map((w) => w[0]).join("").toUpperCase() || "?";
      if (size) fb.style.cssText = `width:${size};height:${size};font-size:10px`;
      img.replaceWith(fb);
    }, { once: true });
    return img;
  }

  function invCanEdit() {
    return state.role !== "viewer" && !!inv.snap?.editable;
  }

  function invSlotEl(slot, item, { movable = true, cls = "" } = {}) {
    const el = document.createElement("div");
    el.className = `inv-slot${cls ? ` ${cls}` : ""}`;
    el.dataset.slot = slot || "";
    el.title = item ? `${invName(item)} (${item.id})` : (slot ? invSlotLabel(slot) : "");
    if (item) {
      el.appendChild(invIcon(item.id));
      if (item.count > 1) {
        const n = document.createElement("span");
        n.className = "inv-n";
        n.textContent = item.count;
        el.appendChild(n);
      }
      if (item.damage && item.max_damage) {
        const bar = document.createElement("span");
        bar.className = "inv-dur";
        const fill = document.createElement("i");
        const left = Math.max(0, 1 - item.damage / item.max_damage);
        fill.style.width = `${Math.round(left * 100)}%`;
        fill.style.background = `hsl(${Math.round(left * 120)} 90% 45%)`;
        bar.appendChild(fill);
        el.appendChild(bar);
      }
    } else if (slot && slot.startsWith("armor.")) {
      el.classList.add("ghost-empty");
    }
    if (inv.selected && inv.selected === slot) el.classList.add("sel");
    if (slot && movable && invCanEdit()) {
      if (item && item.movable) {
        el.draggable = true;
        el.addEventListener("dragstart", (e) => {
          e.dataTransfer.setData("text/plain", slot);
          e.dataTransfer.effectAllowed = "move";
        });
      }
      el.addEventListener("dragover", (e) => { e.preventDefault(); el.classList.add("drop"); });
      el.addEventListener("dragleave", () => el.classList.remove("drop"));
      el.addEventListener("drop", (e) => {
        e.preventDefault();
        el.classList.remove("drop");
        const from = e.dataTransfer.getData("text/plain");
        if (from && from !== slot) invMove(from, slot);
      });
      el.addEventListener("contextmenu", (e) => {
        if (!item) return;
        e.preventDefault();
        invClear(slot);
      });
    }
    if (slot) {
      el.addEventListener("click", () => {
        inv.selected = slot;
        invRender();
      });
    }
    return el;
  }

  function invFill(container, slots, opts) {
    container.textContent = "";
    for (const slot of slots) {
      container.appendChild(invSlotEl(slot, inv.snap.slots[slot] || null, opts));
    }
  }

  function invRender() {
    const snap = inv.snap;
    if (!snap) return;
    const range = (prefix, from, to) => Array.from({ length: to - from + 1 }, (_, i) => `${prefix}.${from + i}`);
    invFill($("#inv-armor"), ["armor.head", "armor.chest", "armor.legs", "armor.feet"]);
    invFill($("#inv-offhand"), ["weapon.offhand"]);
    invFill($("#inv-main"), range("container", 9, 35));
    invFill($("#inv-hotbar"), range("container", 0, 8));
    const hand = $(`#inv-hotbar .inv-slot[data-slot="container.${snap.selected_slot}"]`);
    if (hand) hand.classList.add("hand");
    invFill($("#inv-ender"), range("enderchest", 0, 26));

    const stats = $("#inv-stats");
    stats.textContent = "";
    const st = snap.stats || {};
    for (const line of [
      `❤ Leben ${st.health ?? "–"} / 20`,
      `🍗 Hunger ${st.food ?? "–"} / 20`,
      `✦ Level ${st.level ?? "–"}`,
      INV_GAMEMODE[st.gamemode] ? `Modus: ${INV_GAMEMODE[st.gamemode]}` : "",
    ].filter(Boolean)) {
      const d = document.createElement("div");
      d.textContent = line;
      stats.appendChild(d);
    }

    const mods = $("#inv-mods");
    mods.textContent = "";
    const groups = new Map();
    for (const item of snap.mod_items || []) {
      if (!groups.has(item.group)) groups.set(item.group, []);
      groups.get(item.group).push(item);
    }
    for (const [group, items] of groups) {
      const box = document.createElement("div");
      box.className = "inv-mod-group";
      const lbl = document.createElement("div");
      lbl.className = "inv-lbl";
      lbl.textContent = invPrettyId(group);
      const grid = document.createElement("div");
      grid.className = "inv-grid";
      items.forEach((item, i) => {
        const key = `mod:${group}:${i}`;
        const el = invSlotEl(null, item, { movable: false });
        el.addEventListener("click", () => { inv.selected = key; invRender(); });
        if (inv.selected === key) el.classList.add("sel");
        grid.appendChild(el);
      });
      box.append(lbl, grid);
      mods.appendChild(box);
    }
    show($("#inv-mods-empty"), groups.size === 0);
    invRenderDetail();
  }

  function invSelectedItem() {
    const key = inv.selected;
    if (!key || !inv.snap) return null;
    if (key.startsWith("mod:")) {
      const [, group, idx] = key.split(":");
      const items = (inv.snap.mod_items || []).filter((m) => m.group === group);
      return items[Number(idx)] || null;
    }
    return inv.snap.slots[key] || null;
  }

  function invRenderDetail() {
    const item = invSelectedItem();
    show($("#inv-detail-empty"), !item);
    show($("#inv-detail-body"), !!item);
    if (!item) {
      setText($("#inv-detail-empty"), inv.selected && !inv.selected.startsWith("mod:")
        ? `${invSlotLabel(inv.selected)} ist leer.` : "Slot anklicken, um Details zu sehen.");
      return;
    }
    const isMod = inv.selected.startsWith("mod:");
    const det = $("#inv-det-slot");
    det.textContent = "";
    det.appendChild(invIcon(item.id));
    setText($("#inv-det-name"), invName(item));
    setText($("#inv-det-id"), `${item.id} · ${isMod ? `Mod-Slot ${invPrettyId(item.group)}` : invSlotLabel(inv.selected)}`);
    const kv = $("#inv-det-kv");
    kv.textContent = "";
    const add = (k, v, cls = "") => {
      const dt = document.createElement("dt");
      dt.textContent = k;
      const dd = document.createElement("dd");
      if (v instanceof Node) dd.appendChild(v); else dd.textContent = v;
      if (cls) dd.className = cls;
      kv.append(dt, dd);
    };
    add("Menge", String(item.count));
    if (item.damage != null) {
      add("Haltbarkeit", item.max_damage
        ? `${(item.max_damage - item.damage).toLocaleString("de-DE")} / ${item.max_damage.toLocaleString("de-DE")}`
        : `${item.damage} Schaden`);
    }
    if (item.enchantments?.length) {
      add("Verzauberungen", item.enchantments.map((e) => `${inv.names.get(e.id) || invPrettyId(e.id)} ${e.level}`).join(", "), "inv-ench");
    }
    if (item.data) {
      const pre = document.createElement("pre");
      pre.textContent = item.data;
      add("Daten", pre);
    }
    const editable = invCanEdit() && !isMod;
    show($("#inv-det-acts"), editable);
    const note = $("#inv-det-note");
    const reason = isMod ? "Mod-Slots lassen sich über RCON nicht bearbeiten."
      : (!item.movable && editable ? "Dieses Item hat zu viele Daten, um es über RCON zu verschieben oder die Menge zu ändern. Löschen geht." : "");
    setText(note, reason);
    show(note, !!reason);
    if (editable) {
      const count = $("#inv-det-count");
      count.value = item.count;
      $("#inv-det-apply").disabled = !item.movable;
      $("#inv-det-move").disabled = !item.movable;
      const target = $("#inv-det-target");
      target.textContent = "";
      const all = [
        ...Array.from({ length: 9 }, (_, i) => `container.${i}`),
        ...Array.from({ length: 27 }, (_, i) => `container.${i + 9}`),
        "armor.head", "armor.chest", "armor.legs", "armor.feet", "weapon.offhand",
        ...Array.from({ length: 27 }, (_, i) => `enderchest.${i}`),
      ];
      for (const slot of all) {
        if (slot === inv.selected) continue;
        const opt = document.createElement("option");
        opt.value = slot;
        const other = inv.snap.slots[slot];
        opt.textContent = `${invSlotLabel(slot)}${other ? ` (${invName(other)})` : ""}`;
        target.appendChild(opt);
      }
      const free = all.find((s) => s.startsWith("container.") && s !== inv.selected && !inv.snap.slots[s]);
      if (free) target.value = free;
    }
  }

  async function invLookupNames() {
    const ids = new Set();
    for (const item of [...Object.values(inv.snap?.slots || {}), ...(inv.snap?.mod_items || [])]) {
      ids.add(item.id);
      for (const e of item.enchantments || []) ids.add(e.id);
    }
    const missing = [...ids].filter((id) => !inv.names.has(id));
    if (!missing.length) return;
    try {
      const inst = inv.instance;
      const data = await api(`/api/instances/${encodeURIComponent(inst)}/items?ids=${encodeURIComponent(missing.join(","))}`);
      if (inv.instance !== inst) return;
      for (const [id, info] of Object.entries(data.items || {})) {
        if (info.name) inv.names.set(id, info.name);
      }
      invRender();
    } catch (e) { /* Namen sind optional */ }
  }

  function invApplySnapshot(snap) {
    inv.snap = snap;
    inv.iconVer = snap.icons || "";
    setText($("#inv-title"), `Inventar · ${snap.player}`);
    const meta = $("#inv-meta");
    meta.textContent = "";
    const parts = [
      ["Version", `${snap.loader ? `${snap.loader} ` : ""}${snap.mc_version || "?"}`],
      ["Welt", snap.stats?.dimension ? invPrettyId(snap.stats.dimension) : "–"],
      ["Position", snap.stats?.pos ? snap.stats.pos.join(" / ") : "–"],
    ];
    for (const [k, v] of parts) {
      const span = document.createElement("span");
      const b = document.createElement("b");
      b.textContent = v;
      span.append(`${k} `, b);
      meta.appendChild(span);
    }
    setText($("#inv-stamp"), `Stand ${new Date((snap.fetched_at || Date.now() / 1000) * 1000).toLocaleTimeString("de-DE")}`);
    const ro = $("#inv-readonly");
    const roText = state.role === "viewer" ? "Als Viewer kannst du das Inventar nur ansehen."
      : (!snap.editable ? "Bearbeiten braucht Minecraft 1.17 oder neuer — hier nur ansehen." : "");
    setText(ro, roText);
    show(ro, !!roText);
    if (snap.icons === "failed") {
      setText($("#inv-stamp"), `${$("#inv-stamp").textContent} · Vanilla-Icons nicht ladbar (Mojang nicht erreichbar)`);
    }
    if (snap.icons === "loading") {
      setText($("#inv-stamp"), `${$("#inv-stamp").textContent} · Vanilla-Icons werden geladen …`);
      clearTimeout(inv.iconTimer);
      inv.iconTimer = setTimeout(() => { if ($("#inv-dialog").open) invLoad(true); }, 8000);
    }
    invRender();
    invLookupNames();
  }

  async function invLoad(quiet = false) {
    show($("#inv-error"), false);
    const btn = $("#inv-reload");
    btn.disabled = true;
    try {
      const inst = inv.instance;
      const snap = await api(`/api/instances/${encodeURIComponent(inst)}/players/${encodeURIComponent(inv.player)}/inventory`);
      if (inv.instance !== inst) return;
      invApplySnapshot(snap);
    } catch (e) {
      if (!quiet || !inv.snap) {
        setText($("#inv-error"), e.message);
        show($("#inv-error"), true);
      }
    } finally {
      btn.disabled = false;
    }
  }

  async function invAction(body, okText) {
    if (inv.busy) return;
    inv.busy = true;
    show($("#inv-error"), false);
    const expect = {};
    for (const slot of [body.slot, body.to]) {
      if (slot) expect[slot] = inv.snap.slots[slot]?.sig || "";
    }
    try {
      const snap = await api(`/api/instances/${encodeURIComponent(inv.instance)}/players/${encodeURIComponent(inv.player)}/inventory`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...body, expect }),
      });
      invApplySnapshot(snap);
      toast(okText, "success");
    } catch (e) {
      setText($("#inv-error"), e.message);
      show($("#inv-error"), true);
      if (e.status === 409 || e.status === 400) invLoad(true);
    } finally {
      inv.busy = false;
    }
  }

  function invMove(from, to) {
    const item = inv.snap.slots[from];
    if (!item) return;
    inv.selected = to;
    invAction({ action: "move", slot: from, to },
      `${invName(item)} nach ${invSlotLabel(to)} verschoben`);
  }

  async function invClear(slot) {
    const item = inv.snap.slots[slot];
    if (!item) return;
    const ok = await confirmDialog({
      title: "Item löschen?",
      message: `${item.count}× ${invName(item)} aus ${invSlotLabel(slot)} von ${inv.player} entfernen. Das lässt sich nicht rückgängig machen.`,
      ok: "Löschen", danger: true,
    });
    if (ok) invAction({ action: "clear", slot }, `${invName(item)} gelöscht`);
  }

  function invRenderResults(results) {
    const ul = $("#inv-give-results");
    ul.textContent = "";
    for (const r of results) {
      const li = document.createElement("li");
      li.dataset.id = r.id;
      if (r.id === inv.giveId) li.classList.add("active");
      const ico = document.createElement("span");
      ico.className = "inv-ico";
      ico.appendChild(invIcon(r.id, "22px"));
      const nm = document.createElement("span");
      nm.textContent = r.name || invPrettyId(r.id);
      const id = document.createElement("span");
      id.className = "inv-mono";
      id.textContent = r.id;
      li.append(ico, nm, id);
      if (r.name) inv.names.set(r.id, r.name);
      li.addEventListener("click", () => {
        inv.giveId = r.id;
        $("#inv-give-q").value = r.id;
        ul.querySelectorAll("li").forEach((x) => x.classList.toggle("active", x === li));
      });
      ul.appendChild(li);
    }
  }

  async function invSearch() {
    const q = $("#inv-give-q").value.trim();
    if (q.length < 2) { invRenderResults([]); return; }
    try {
      const inst = inv.instance;
      const data = await api(`/api/instances/${encodeURIComponent(inst)}/items?q=${encodeURIComponent(q)}`);
      if (inv.instance !== inst || $("#inv-give-q").value.trim() !== q) return;
      const results = data.results || [];
      if (!inv.giveId || !results.some((r) => r.id === inv.giveId)) inv.giveId = results[0]?.id || null;
      invRenderResults(results);
    } catch (e) { invRenderResults([]); }
  }

  function openInventory(player) {
    inv.instance = state.detailId;
    inv.player = player;
    inv.snap = null;
    inv.selected = null;
    inv.tab = "main";
    inv.giveId = null;
    $("#inv-face").src = playerFace(player);
    setText($("#inv-title"), `Inventar · ${player}`);
    setText($("#inv-meta"), "");
    setText($("#inv-stamp"), "lädt …");
    ["#inv-armor", "#inv-offhand", "#inv-main", "#inv-hotbar", "#inv-ender", "#inv-mods", "#inv-stats", "#inv-give-results"]
      .forEach((sel) => { $(sel).textContent = ""; });
    $("#inv-give-q").value = "";
    show($("#inv-detail-body"), false);
    show($("#inv-detail-empty"), true);
    invSwitchTab("main");
    $("#inv-dialog").showModal();
    invLoad();
  }

  function invSwitchTab(tab) {
    inv.tab = tab;
    document.querySelectorAll(".inv-tab").forEach((b) => b.classList.toggle("active", b.dataset.invTab === tab));
    show($("#inv-pane-main"), tab === "main");
    show($("#inv-pane-ender"), tab === "ender");
    show($("#inv-pane-mods"), tab === "mods");
  }

  document.querySelectorAll(".inv-tab").forEach((b) => b.addEventListener("click", () => invSwitchTab(b.dataset.invTab)));
  $("#inv-reload").addEventListener("click", () => invLoad());
  $("#inv-close").addEventListener("click", () => closeDialog($("#inv-dialog")));
  $("#inv-dialog").addEventListener("cancel", (e) => { e.preventDefault(); closeDialog($("#inv-dialog")); });
  $("#inv-dialog").addEventListener("close", () => clearTimeout(inv.iconTimer));
  $("#inv-det-apply").addEventListener("click", () => {
    const item = invSelectedItem();
    const count = Number($("#inv-det-count").value);
    if (!item || !Number.isInteger(count) || count < 1 || count > 99) {
      toast("Menge muss zwischen 1 und 99 liegen", "error");
      return;
    }
    invAction({ action: "set_count", slot: inv.selected, count }, `Menge auf ${count} gesetzt`);
  });
  $("#inv-det-move").addEventListener("click", () => invMove(inv.selected, $("#inv-det-target").value));
  $("#inv-det-clear").addEventListener("click", () => invClear(inv.selected));
  $("#inv-give-q").addEventListener("input", () => {
    inv.giveId = null;
    clearTimeout(inv.giveTimer);
    inv.giveTimer = setTimeout(invSearch, 250);
  });
  $("#inv-give-btn").addEventListener("click", () => {
    const typed = $("#inv-give-q").value.trim();
    const id = inv.giveId || (/^[a-z0-9_.-]+:[a-z0-9_./-]+$/.test(typed) ? typed : "");
    const count = Number($("#inv-give-count").value);
    if (!id) { toast("Erst ein Item aus der Liste wählen", "error"); return; }
    if (!Number.isInteger(count) || count < 1 || count > 6400) {
      toast("Menge muss zwischen 1 und 6400 liegen", "error");
      return;
    }
    invAction({ action: "give", item_id: id, count }, `${count}× ${inv.names.get(id) || invPrettyId(id)} gegeben`);
  });

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

  /* ---------- Welt-Einstellungen (Spielregeln + Allgemein) ---------- */
  // Kuratierte Auswahl mit verständlichen Erklärungen; alle übrigen Regeln
  // bleiben im Gamerule-Editor, alle Properties im server.properties-Editor.
  const WSET_RULES = [
    { name: "playersSleepingPercentage", label: "Schlaf-Schwelle", slider: true,
      desc: "Wie viel Prozent der Spieler schlafen müssen, damit die Nacht übersprungen wird. Bei 0 reicht ein einzelner schlafender Spieler." },
    { name: "doDaylightCycle", label: "Tageszyklus",
      desc: "Wenn aus, bleiben Sonne und Mond stehen. Die Uhrzeit setzt du oben mit den Knöpfen." },
    { name: "doWeatherCycle", label: "Wetterwechsel",
      desc: "Wenn aus, bleibt das aktuelle Wetter dauerhaft. Das Wetter setzt du oben mit den Knöpfen." },
    { name: "locatorBar", label: "Spieler-Ortungsleiste",
      desc: "Zeigt jedem Spieler eine Leiste mit der Richtung zu den anderen. Wer seine Basis geheim halten will, schaltet sie aus. Schleichen oder ein Mob-Kopf verstecken einen Spieler kurzzeitig." },
    { name: "keepInventory", label: "Inventar behalten",
      desc: "Spieler behalten beim Tod ihre Items und ihre Erfahrung." },
    { name: "mobGriefing", label: "Mobs verändern Blöcke",
      desc: "Creeper sprengen Löcher, Endermen tragen Blöcke weg. Aus schützt Bauten, stoppt aber auch Dorfbewohner beim Ernten." },
    { name: "doInsomnia", label: "Phantome",
      desc: "Phantome greifen Spieler an, die drei Nächte nicht geschlafen haben." },
    { name: "doFireTick", label: "Feuer breitet sich aus",
      desc: "Feuer springt auf brennbare Blöcke über. Aus verhindert Waldbrände und abgebrannte Holzhäuser." },
    // Ab 1.21.11 ersetzt der Radius den Schalter oben (der Server meldet nur eins von beiden)
    { name: "fireSpreadRadius", label: "Feuer breitet sich aus", number: true,
      desc: "Radius in Blöcken um Spieler, in dem sich Feuer ausbreitet. 0 schaltet die Ausbreitung ab, -1 erlaubt sie überall." },
  ];

  const WSET_GENERAL = [
    { key: "level-name", label: "Weltname", readonly: true, def: "world",
      desc: "Ordner der aktiven Welt. Welten wechseln und neu anlegen geht weiter unten unter „Welten“." },
    { key: "level-seed", label: "Welt-Seed", readonly: true, def: "", empty: "zufällig",
      desc: "Bestimmt, wie die Welt erzeugt wird. Gilt nur beim Erzeugen; eine neue Welt mit eigenem Seed legst du unter „Welten“ an." },
    { key: "gamemode", label: "Spielmodus", def: "survival",
      choices: [["survival", "Überleben"], ["creative", "Kreativ"], ["adventure", "Abenteuer"], ["spectator", "Zuschauer"]],
      desc: "Modus für neue Spieler. Überleben ist das klassische Spiel, Kreativ gibt unbegrenzt Blöcke und Fliegen, Abenteuer ist für eigene Karten, Zuschauer nur zum Zusehen." },
    { key: "difficulty", label: "Schwierigkeit", def: "easy",
      choices: [["peaceful", "Friedlich"], ["easy", "Einfach"], ["normal", "Normal"], ["hard", "Schwer"]],
      desc: "Friedlich entfernt alle Monster, Einfach macht sie schwächer, Normal ist das Standard-Spiel, bei Schwer kann man verhungern." },
    { key: "hardcore", label: "Hardcore", bool: true, def: "false",
      desc: "Nur ein Leben: Wer stirbt, kann nur noch zuschauen. Die Schwierigkeit ist dann fest auf Schwer." },
    { key: "pvp", label: "PvP", bool: true, def: "true",
      desc: "Spieler können sich gegenseitig Schaden zufügen." },
    { key: "max-players", label: "Maximale Spieler", int: [1, 1000], def: "20",
      desc: "So viele Spieler können gleichzeitig verbunden sein." },
    { key: "allow-flight", label: "Fliegen erlauben", bool: true, def: "false", adv: true,
      desc: "Verhindert Kicks wegen Fliegens. Nötig für manche Mods und Plugins mit Flug." },
    { key: "allow-nether", label: "Nether erlauben", bool: true, def: "true", adv: true,
      desc: "Wenn aus, funktionieren Netherportale nicht." },
    { key: "force-gamemode", label: "Spielmodus erzwingen", bool: true, def: "false", adv: true,
      desc: "Setzt jeden Spieler beim Betreten auf den Standard-Spielmodus zurück." },
    { key: "spawn-protection", label: "Spawn-Schutz", int: [0, 256], def: "16", adv: true,
      desc: "Radius in Blöcken um den Spawn, in dem nur Operatoren bauen dürfen. 0 schaltet den Schutz ab." },
    { key: "view-distance", label: "Sichtweite", int: [3, 32], def: "10", adv: true,
      desc: "Wie viele Chunks Spieler sehen. Der größte Hebel für RAM und Leistung." },
    { key: "simulation-distance", label: "Simulationsweite", int: [3, 32], def: "10", adv: true,
      desc: "In wie vielen Chunks um Spieler Pflanzen wachsen und Mobs sich bewegen." },
  ];

  state.wset = { props: [], rules: [], edits: {} };

  function wsetSetError(msg) { authError("#wset-error", msg); }

  function wsetRunning() {
    return instStateKey(currentDetailInst() || state.detailData || {}) === "running";
  }

  // Minecraft-Ticks → Uhrzeit (Tick 0 = 6:00 Uhr)
  function wsetClock(ticks) {
    if (ticks == null) return "–";
    const hour = (Math.floor(ticks / 1000) + 6) % 24;
    const minute = Math.floor((ticks % 1000) * 60 / 1000);
    return `${String(hour).padStart(2, "0")}:${String(minute).padStart(2, "0")}`;
  }

  function wsetRow(label, desc, def, control) {
    const row = document.createElement("div");
    row.className = "wset-row";
    const text = document.createElement("div");
    text.className = "wset-text";
    const title = document.createElement("div");
    title.className = "wset-label";
    title.textContent = label;
    const info = document.createElement("div");
    info.className = "wset-desc muted small";
    info.textContent = desc;
    text.append(title, info);
    if (def !== null) {
      const d = document.createElement("div");
      d.className = "wset-default muted small";
      const code = document.createElement("code");
      code.textContent = def;
      d.append("Standard: ", code);
      text.append(d);
    }
    const ctl = document.createElement("div");
    ctl.className = "wset-control";
    ctl.append(control);
    row.append(text, ctl);
    return row;
  }

  function wsetSwitch(checked, onChange) {
    const label = document.createElement("label");
    label.className = "wset-switch";
    const input = document.createElement("input");
    input.type = "checkbox";
    input.checked = checked;
    input.disabled = state.role === "viewer";
    input.addEventListener("change", () => onChange(input));
    const knob = document.createElement("span");
    knob.className = "wset-knob";
    label.append(input, knob);
    return label;
  }

  function renderWsetRules() {
    const list = $("#wset-rules");
    list.textContent = "";
    const byName = new Map(state.wset.rules.map((g) => [g.name, g]));
    for (const def of WSET_RULES) {
      const rule = byName.get(def.name);
      if (!rule) continue; // in dieser Version nicht vorhanden
      const value = rule.value == null ? rule.default : rule.value;
      let control;
      if (def.slider) {
        control = document.createElement("div");
        control.className = "wset-slider";
        const range = document.createElement("input");
        range.type = "range";
        range.min = String(rule.min ?? 0);
        range.max = String(rule.max ?? 100);
        range.value = String(value);
        range.disabled = state.role === "viewer";
        range.setAttribute("aria-label", def.label);
        const out = document.createElement("span");
        out.className = "wset-slider-val";
        out.textContent = `${value} %`;
        range.addEventListener("input", () => { out.textContent = `${range.value} %`; });
        range.addEventListener("change", () => wsetSetRule(def, Number(range.value), () => {
          range.value = String(value);
          out.textContent = `${value} %`;
        }));
        control.append(range, out);
      } else if (def.number) {
        control = document.createElement("input");
        control.type = "number";
        control.className = "cfg-input wset-num";
        control.min = String(rule.min ?? 0);
        control.value = String(value);
        control.disabled = state.role === "viewer";
        control.setAttribute("aria-label", def.label);
        const input = control;
        input.addEventListener("change", () => wsetSetRule(def, Number(input.value), () => {
          input.value = String(value);
        }));
      } else {
        control = wsetSwitch(value === true, (input) =>
          wsetSetRule(def, input.checked, () => { input.checked = !input.checked; }));
      }
      const shownDefault = def.slider ? `${rule.default} %`
        : def.number ? String(rule.default) : (rule.default ? "an" : "aus");
      list.appendChild(wsetRow(def.label, def.desc, shownDefault, control));
    }
  }

  async function wsetSetRule(def, value, revert) {
    try {
      const data = await api(`/api/instances/${state.detailId}/gamerules`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: def.name, value }),
      });
      const rule = state.wset.rules.find((g) => g.name === def.name);
      if (rule) rule.value = data.value;
      const grRule = state.grRules.find((g) => g.name === def.name);
      if (grRule) { grRule.value = data.value; renderGamerules(); }
      const shown = typeof data.value === "boolean" ? (data.value ? "an" : "aus")
        : def.slider ? `${data.value} %` : String(data.value);
      toast(`${def.label}: ${shown}`, "success");
    } catch (e) {
      revert();
      toast(`${def.label} nicht geändert: ${e.message}`, "error");
    }
  }

  async function wsetTimeWeather(body) {
    try {
      const data = await api(`/api/instances/${state.detailId}/world/time-weather`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      setText($("#wset-time"), wsetClock(data.daytime));
      toast(body.time ? `Uhrzeit: ${wsetClock(data.daytime)}` : "Wetter geändert", "success");
    } catch (e) {
      toast(`Nicht geändert: ${e.message}`, "error");
    }
  }

  function wsetPropValue(key) {
    if (key in state.wset.edits) return state.wset.edits[key];
    return state.wset.props.find((p) => p.key === key)?.value;
  }

  function wsetEdit(key, value) {
    state.wset.edits[key] = value;
    $("#wset-save").disabled = false;
  }

  function renderWsetGeneral() {
    const list = $("#wset-general");
    list.textContent = "";
    const advanced = $("#wset-advanced").checked;
    const viewer = state.role === "viewer";
    for (const def of WSET_GENERAL) {
      if (def.adv && !advanced) continue;
      const raw = wsetPropValue(def.key);
      const value = raw == null ? def.def : String(raw);
      let control;
      if (def.readonly) {
        control = document.createElement("code");
        control.className = "wset-readonly";
        control.textContent = value || def.empty || "–";
      } else if (def.bool) {
        control = wsetSwitch(value.trim().toLowerCase() === "true",
          (input) => wsetEdit(def.key, input.checked ? "true" : "false"));
      } else if (def.choices) {
        control = document.createElement("select");
        control.className = "cfg-input";
        for (const [v, text] of def.choices) {
          const opt = document.createElement("option");
          opt.value = v;
          opt.textContent = text;
          control.appendChild(opt);
        }
        control.value = value.trim().toLowerCase();
        control.disabled = viewer;
        control.addEventListener("change", (e) => wsetEdit(def.key, e.target.value));
      } else {
        control = document.createElement("input");
        control.type = "number";
        control.className = "cfg-input wset-num";
        control.min = String(def.int[0]);
        control.max = String(def.int[1]);
        control.value = value;
        control.disabled = viewer;
        control.addEventListener("input", (e) => wsetEdit(def.key, e.target.value));
      }
      if (!def.readonly && !def.bool) control.setAttribute("aria-label", def.label);
      const shownDefault = def.readonly ? null
        : def.choices ? def.choices.find(([v]) => v === def.def)?.[1]
          : def.bool ? (def.def === "true" ? "an" : "aus") : def.def;
      list.appendChild(wsetRow(def.label, def.desc, shownDefault, control));
    }
  }

  async function wsetLoad() {
    if (!state.detailId) return;
    const id = state.detailId;
    wsetSetError("");
    state.wset.edits = {};
    $("#wset-save").disabled = true;
    show($("#wset-restart-row"), false);
    try {
      const cfg = await api(`/api/instances/${id}/config`);
      if (state.detailId !== id) return;
      state.wset.props = cfg.properties || [];
    } catch (e) {
      state.wset.props = [];
      wsetSetError(`Einstellungen nicht ladbar: ${e.message}`);
    }
    renderWsetGeneral();
    const running = wsetRunning();
    state.wset.rules = [];
    show($("#wset-rules-off"), !running);
    show($("#wset-clock"), false);
    if (running) {
      try {
        const data = await api(`/api/instances/${id}/gamerules`);
        if (state.detailId !== id) return;
        state.wset.rules = data.gamerules || [];
        setText($("#wset-time"), wsetClock(data.daytime));
        show($("#wset-clock"), true);
      } catch (e) {
        show($("#wset-rules-off"), e.status === 409);
        if (e.status !== 409) wsetSetError(`Spielregeln nicht ladbar: ${e.message}`);
      }
    }
    renderWsetRules();
  }

  async function wsetSave() {
    const btn = $("#wset-save");
    btn.disabled = true;
    wsetSetError("");
    try {
      const edits = { ...state.wset.edits };
      const properties = state.wset.props.map((p) =>
        ({ key: p.key, value: p.key in edits ? edits[p.key] : p.value }));
      for (const [key, value] of Object.entries(edits)) {
        if (!state.wset.props.some((p) => p.key === key)) properties.push({ key, value });
      }
      const data = await api(`/api/instances/${state.detailId}/config`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ properties }),
      });
      state.wset.props = properties;
      state.wset.edits = {};
      toast("Welt-Einstellungen gespeichert.", "success");
      show($("#wset-restart-row"), !!data.restart_required);
      if (state.cfgOpen) loadCfg();
    } catch (e) {
      btn.disabled = false;
      wsetSetError(`Speichern fehlgeschlagen: ${e.message}`);
    }
  }

  $("#wset-reload").addEventListener("click", wsetLoad);
  $("#wset-save").addEventListener("click", wsetSave);
  $("#wset-advanced").addEventListener("change", renderWsetGeneral);
  $("#wset-restart").addEventListener("click", () => {
    if (!state.detailId) return;
    show($("#wset-restart-row"), false);
    instAction({ id: state.detailId }, "restart");
  });
  document.querySelectorAll("[data-wset-time]").forEach((btn) =>
    btn.addEventListener("click", () => wsetTimeWeather({ time: btn.dataset.wsetTime })));
  document.querySelectorAll("[data-wset-weather]").forEach((btn) =>
    btn.addEventListener("click", () => wsetTimeWeather({ weather: btn.dataset.wsetWeather })));

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
      const data = await api(`/api/instances/${state.detailId}/modpacks/update-check`
        + (quiet ? "" : "?refresh=true"));
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

  /* =====================================================================
     Instanz-Workspace (SpawnBox-Look)
     - Instanz-Tabs oben: Schnellwechsel zwischen Servern + "Alle"-Tab
     - Kopf-Grid: Server-Info · Steuerung (Start/Stop/Chat/Auto-Start) ·
       Backup-Kompaktpanel
     - Bereichs-Tabs: bestehende Detail-Boxen werden bei Init in Panels
       einsortiert (DOM bleibt sonst unverändert — keine Backend-Änderung)
     ===================================================================== */
  /* Hotbar-Slots: [Schlüssel, Name, Pixel-Icon]. Reihenfolge = Taste 1–9. */
  const WS_TABS = [
    ["uebersicht", "Übersicht", "compass"],
    ["konsole", "Konsole", "cmd"],
    ["spieler", "Spieler", "head"],
    ["mods", "Mods & Packs", "book"],
    ["welt", "Welt", "globe"],
    ["dateien", "Dateien", "folder"],
    ["backups", "Backups", "chest"],
    ["zeitplan", "Zeitplan", "clock"],
    ["einstellungen", "Einstellungen", "anvil"],
  ];

  const SLOT_ICONS = {
    compass: pixelIcon(["..ssss..", ".sYYYYs.", "sYyrryYs", "sYyrRyYs", "sYybbyYs", "sYybbyYs", ".sYYYYs.", "..ssss.."]),
    cmd: pixelIcon(["oooooooo", "oSSSSSSo", "oSwSSSSo", "oSSwSSSo", "oSwSSSSo", "oSSSwwSo", "oSSSSSSo", "oooooooo"]),
    head: pixelIcon(["kkkkkkkk", "kkkkkkkk", "kttttttk", "twkttkwt", "tttttttt", "tttttttt", "ttRRRRtt", "tttttttt"]),
    book: pixelIcon([".RRRRRR.", ".RwwwwR.", ".RwkkwR.", ".RwwwwR.", ".RwkkwR.", ".RwwwwR.", ".RRRRRR.", "..YYYY.."]),
    globe: pixelIcon(["..bbbb..", ".bgbbgb.", "bggbbbgb", "bbggbbbb", "bbbgggbb", "bgbbggbb", ".bbbbgb.", "..bbbb.."]),
    folder: pixelIcon(["........", "YYY.....", "YyyYYYYY", "YyyyyyyY", "YyyyyyyY", "YyyyyyyY", "YYYYYYYY", "........"]),
    chest: pixelIcon(["DddddddD", "dYYYYYYd", "dYddddYd", "kkkyykkk", "dYdyydYd", "dYddddYd", "dYYYYYYd", "DddddddD"]),
    clock: pixelIcon(["..yyyy..", ".yYYYYy.", "yYwwwwYy", "yYwkwwYy", "yYwkkwYy", "yYwwwwYy", ".yYYYYy.", "..yyyy.."]),
    anvil: pixelIcon(["........", "SSSSSSSs", ".SSSSSs.", "...Ss...", "...Ss...", "..SSSs..", ".SSSSSs.", "........"]),
  };

  /* Server-Icon-Ersatz: Pixel-Block in einer stabilen Farbe je Name */
  function serverBlockIcon(name) {
    let h = 0;
    for (const c of String(name || "")) h = (h * 31 + c.charCodeAt(0)) >>> 0;
    const base = `hsl(${h % 360} 45% 42%)`;
    return "data:image/svg+xml;utf8," + encodeURIComponent(
      `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 8 8" shape-rendering="crispEdges">` +
      `<rect width="8" height="8" fill="${base}"/><rect y="3" width="8" height="5" fill="#00000040"/>` +
      `<rect x="1" y="1" width="2" height="1" fill="#ffffff55"/><rect x="5" y="0" width="1" height="2" fill="#ffffff33"/>` +
      `<rect x="2" y="5" width="1" height="1" fill="#ffffff22"/><rect x="5" y="6" width="2" height="1" fill="#00000033"/></svg>`);
  }

  /* Pixel-Kopf je Spielername (stabil, ohne externe Skin-Dienste) */
  function playerFace(name) {
    let h = 0;
    for (const c of String(name || "")) h = (h * 33 + c.charCodeAt(0)) >>> 0;
    const skin = ["#c58c6a", "#e2b28c", "#8d5a3b", "#f0caa6"][h % 4];
    const hair = ["#2d1e12", "#141414", "#b5651d", "#dcc27a", "#5b3a8c", "#3c6e3a"][(h >> 3) % 6];
    const eye = ["#3b6fd4", "#2e8b57", "#5a3c24"][(h >> 6) % 3];
    const r = (x, y, w, hh, f) => `<rect x="${x}" y="${y}" width="${w}" height="${hh}" fill="${f}"/>`;
    return "data:image/svg+xml;utf8," + encodeURIComponent(
      `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 8 8" shape-rendering="crispEdges">` +
      r(0, 0, 8, 8, skin) + r(0, 0, 8, 2, hair) + r(0, 2, 1, 1, hair) + r(7, 2, 1, 1, hair) +
      r(1, 4, 2, 1, "#fff") + r(5, 4, 2, 1, "#fff") + r(2, 4, 1, 1, eye) + r(5, 4, 1, 1, eye) +
      r(2, 6, 4, 1, "#7a4a3a") + "</svg>");
  }

  /* Übersicht-Slot: Spieler-Leiste, Version, Adresse, Online-Liste */
  function updateWsOverview() {
    if (!state.detailId) return;
    const inst = currentDetailInst() || state.detailData;
    if (!inst) return;
    const running = instStateKey(inst) === "running";
    const ping = (running && (liveEntry(inst)?.ping || state.detailData?.ping)) || null;
    const online = ping?.online ? ping.players?.online || 0 : 0;
    const max = ping?.online ? ping.players?.max || 0 : 0;
    setText($("#ws-players-val"), ping?.online ? `${online} / ${max}` : "–");
    $("#ws-players-bar").style.width = max ? `${Math.min(100, (online / max) * 100)}%` : "0%";
    setText($("#ws-version"), ping?.online
      ? (ping.version || `Minecraft ${inst.game_version}`)
      : (running ? "Server antwortet noch nicht" : "Server ist offline"));
    setText($("#ws-addr"), hostFor(inst));

    const list = $("#ws-online-list");
    list.textContent = "";
    setText($("#ws-online-count"), ping?.online ? `${online}/${max}` : "");
    const names = (ping?.players?.sample || []).map((p) => p.name).filter(Boolean);
    if (!ping?.online || !online) {
      const li = document.createElement("li");
      li.className = "ws-online-empty muted small";
      li.textContent = !running ? "Server ist offline."
        : !ping?.online ? "Warte auf Antwort vom Server …"
          : "Gerade ist niemand online.";
      list.appendChild(li);
      return;
    }
    for (const name of names) {
      const li = document.createElement("li");
      const img = document.createElement("img");
      img.src = playerFace(name);
      img.alt = "";
      const span = document.createElement("span");
      span.textContent = name;
      li.append(img, span);
      list.appendChild(li);
    }
    if (online > names.length) {
      const li = document.createElement("li");
      li.className = "ws-online-empty muted small";
      li.textContent = names.length
        ? `+ ${online - names.length} weitere`
        : `${online} Spieler online (Namen verbirgt der Server)`;
      list.appendChild(li);
    }
  }

  let slotNameTimer = null;
  function activateWsTab(key, announce = false) {
    state.wsTab = key;
    try { localStorage.setItem("md_ws_tab", key); } catch (e) { /* optional */ }
    document.querySelectorAll("#ws-tabs .slot").forEach((b) => {
      const on = b.dataset.wsTab === key;
      b.classList.toggle("active", on);
      b.setAttribute("aria-current", on ? "page" : "false");
    });
    document.querySelectorAll("#ws-panels .ws-panel").forEach((p) =>
      show(p, p.dataset.wsPanel === key));
    // Das Live-Log gibt es nur einmal: in der Konsole groß über den
    // Befehlen, sonst in der Übersicht unter Aktivität & Chat
    const logs = document.querySelector(".ws-logcol");
    if (logs) {
      if (key === "konsole") document.querySelector('.ws-panel[data-ws-panel="konsole"]')?.prepend(logs);
      else if (logs.parentElement?.id !== "ws-ov-main") $("#ws-ov-main")?.append(logs);
    }
    // Mods › Hinzufügen: Suche in den Server-Bereich holen (Ziel = dieser Server)
    if (key === "mods" && state.modsView === "add" && state.detailId) mountSearch(true);
    // Welt-Slot: Welten + Karte laden; beim Verlassen die Karte entladen
    if (key === "welt" && state.detailId && state.wsLoaded?.has("welt")) loadWorldInfo();
    else if (key !== "welt" && typeof resetMapFrame === "function") resetMapFrame();
    loadWsTabData(key);
    // Spieler-Slot: Liste direkt laden, wenn der Server läuft
    if (key === "spieler" && state.detailId
        && instStateKey(currentDetailInst() || state.detailData || {}) === "running") {
      loadRconPlayers();
    }
    if (announce) {
      // Name des Slots kurz über der Hotbar einblenden (wie im Spiel)
      const label = WS_TABS.find(([k]) => k === key)?.[1] || "";
      const el = $("#slot-name");
      el.textContent = label;
      el.classList.add("show");
      clearTimeout(slotNameTimer);
      slotNameTimer = setTimeout(() => el.classList.remove("show"), 1300);
    }
  }

  /* Hotbar nur zeigen, wenn im Server-Tab eine Instanz geöffnet ist */
  function syncHotbar() {
    const visible = state.tab === "servers" && !!state.detailId;
    show($("#hotbar-wrap"), visible);
    document.body.classList.toggle("has-hotbar", visible);
  }

  /* Zähler in den Slots: Spieler online, installierte Mods */
  function updateHotbarCounts() {
    const set = (key, n) => {
      const el = document.querySelector(`#ws-tabs .slot[data-ws-tab="${key}"] .slot-count`);
      if (el) el.textContent = n > 0 ? String(n) : "";
    };
    const inst = state.detailId ? currentDetailInst() : null;
    set("spieler", inst ? liveEntry(inst)?.ping?.players?.online || 0 : 0);
    set("mods", state.detailData?.mods?.length || 0);
    const updates = Object.values(state.detailUpdates || {})
      .filter((u) => u?.status === "update_available").length;
    const modsSlot = document.querySelector('#ws-tabs .slot[data-ws-tab="mods"]');
    if (modsSlot) {
      modsSlot.classList.toggle("has-update", updates > 0);
      modsSlot.title = `Mods & Packs (Taste 4)${updates ? ` · ${updates} Update(s) verfügbar` : ""}`;
    }
  }

  /* Mods-Bereich: drei Ansichten (Installiert · Hinzufügen · Modpack) */
  const MODS_VIEWS = [["installed", "Installiert"], ["add", "Hinzufügen"], ["packs", "Modpack"]];
  state.modsView = "installed";
  try {
    const saved = localStorage.getItem("md_mods_view");
    if (MODS_VIEWS.some(([k]) => k === saved)) state.modsView = saved;
  } catch (e) { /* optional */ }

  function buildModsViews() {
    const panel = document.querySelector('.ws-panel[data-ws-panel="mods"]');
    if (!panel || $("#ws-mods-seg")) return;
    const seg = document.createElement("div");
    seg.id = "ws-mods-seg";
    seg.className = "seg ws-mods-seg";
    seg.setAttribute("role", "tablist");
    seg.setAttribute("aria-label", "Mods-Bereich");
    for (const [key, label] of MODS_VIEWS) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "seg-btn";
      b.dataset.modsView = key;
      b.setAttribute("role", "tab");
      b.textContent = label;
      b.addEventListener("click", () => activateModsView(key));
      seg.appendChild(b);
      const view = document.createElement("div");
      view.id = `ws-mods-${key}`;
      view.className = "ws-mods-view";
      view.dataset.modsView = key;
      panel.appendChild(view);
    }
    panel.prepend(seg);
    applyModsView();
  }

  function applyModsView() {
    document.querySelectorAll("#ws-mods-seg .seg-btn").forEach((b) => {
      const on = b.dataset.modsView === state.modsView;
      b.classList.toggle("active", on);
      b.setAttribute("aria-selected", on ? "true" : "false");
    });
    document.querySelectorAll(".ws-mods-view").forEach((v) =>
      show(v, v.dataset.modsView === state.modsView));
  }

  function activateModsView(key) {
    state.modsView = key;
    try { localStorage.setItem("md_mods_view", key); } catch (e) { /* optional */ }
    applyModsView();
    if (key === "add" && state.wsTab === "mods" && state.detailId) mountSearch(true);
  }

  /* Sortiert die Detail-Boxen einmalig in die Workspace-Panels um.
     Anker = stabile IDs innerhalb der jeweiligen Box. */
  function organizeWorkspace() {
    buildModsViews();
    const mapping = [
      ["#mp-update-check", "mods-packs"],   // Installiertes Modpack
      ["#pack-search-input", "mods-packs"], // Modpack installieren (Suche)
      ["#pack-upload-goto", "mods-packs"],  // Modpack hochladen (→ Upload-Tab)
      ["#wset-reload", "welt"],           // Welt-Einstellungen
      ["#world-reload", "welt"],          // Welten
      ["#map-reload", "welt"],            // Live-Karte
      ["#icon-reload", "welt"],           // Server-Icon
      ["#dp-reload", "welt"],             // Datapacks
      ["#fb-reload", "dateien"],          // Datei-Browser
      ["#rcon-reload", "spieler"],        // Spieler verwalten (RCON)
      ["#wl-reload-file", "spieler"],     // Whitelist
      ["#gr-reload", "welt"],             // Alle Gamerules
      ["#cfg-toggle", "einstellungen"],   // server.properties
      ["#jvm-save", "einstellungen"],     // JVM & RAM
      ["#hib-save", "einstellungen"],     // Schlafmodus
      ["#ver-check", "einstellungen"],    // Version wechseln
      ["#tags-save", "einstellungen"],    // Tags & Port
      ["#inst-delete-btn", "einstellungen"], // Server löschen
      ["#sched-autostart", "zeitplan"],   // Zeitplan
      ["#console-send", "konsole"],       // RCON-Konsole
      ["#backup-create-btn", "backups"],  // Backup-Verwaltung
    ];
    for (const [anchorSel, target] of mapping) {
      const anchor = document.querySelector(anchorSel);
      const panel = target === "mods-packs" ? $("#ws-mods-packs")
        : document.querySelector(`.ws-panel[data-ws-panel="${target}"]`);
      if (!anchor || !panel) continue;
      const box = anchor.closest(".upload-box");
      if (box) panel.appendChild(box);
    }
    // Mods-/Logs-Spalte aus dem alten Detail-Grid einsortieren
    const modsBox = document.querySelector("#detail-mods")?.closest("div");
    if (modsBox) {
      modsBox.classList.add("ws-modsbox");
      $("#ws-mods-installed").prepend(modsBox);
    }
    const logsBox = document.querySelector(".ws-logcol");
    const logsTarget = $("#ws-ov-main"); // Live-Log unter Aktivität & Chat
    if (logsBox && logsTarget) logsTarget.append(logsBox);
    document.querySelector("#inst-detail .detail-grid")?.remove();

    // Hotbar-Slots aufbauen
    const nav = $("#ws-tabs");
    nav.textContent = "";
    WS_TABS.forEach(([key, label, icon], i) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "slot";
      b.dataset.wsTab = key;
      b.title = `${label} (Taste ${i + 1})`;
      b.setAttribute("aria-label", label);
      const num = document.createElement("span");
      num.className = "slot-num";
      num.textContent = String(i + 1);
      const img = document.createElement("img");
      img.src = SLOT_ICONS[icon];
      img.alt = "";
      const count = document.createElement("span");
      count.className = "slot-count";
      b.append(num, img, count);
      b.addEventListener("click", () => activateWsTab(key, true));
      nav.appendChild(b);
    });
    // Tasten 1–9 wechseln den Slot (nicht beim Tippen oder in Dialogen)
    document.addEventListener("keydown", (e) => {
      if (!/^[1-9]$/.test(e.key) || e.ctrlKey || e.metaKey || e.altKey) return;
      if ($("#hotbar-wrap").classList.contains("hidden")) return;
      const t = e.target;
      if (t.closest?.("input, textarea, select, [contenteditable], dialog")) return;
      if (document.querySelector("dialog[open]")) return;
      activateWsTab(WS_TABS[+e.key - 1][0], true);
    });
    // Mausrad über der Hotbar blättert durch die Slots
    nav.addEventListener("wheel", (e) => {
      if (Math.abs(e.deltaY) < Math.abs(e.deltaX)) return;
      e.preventDefault();
      const i = WS_TABS.findIndex(([k]) => k === state.wsTab);
      const next = (i + (e.deltaY > 0 ? 1 : WS_TABS.length - 1)) % WS_TABS.length;
      activateWsTab(WS_TABS[next][0], true);
    }, { passive: false });
    let saved = null;
    try { saved = localStorage.getItem("md_ws_tab"); } catch (e) { /* optional */ }
    activateWsTab(saved && WS_TABS.some(([k]) => k === saved) ? saved : "uebersicht");
  }

  /* ---------- Instanz-Tab-Leiste oben ---------- */
  function renderInstTabs(list) {
    const nav = $("#inst-tabs");
    if (!nav) return;
    nav.textContent = "";
    const all = document.createElement("button");
    all.type = "button";
    const onServers = state.tab === "servers";
    all.className = "inst-tab" + (state.tab === "overview" ? " active" : "");
    all.textContent = "Alle Server";
    all.title = "Kartenansicht aller Server";
    all.addEventListener("click", () => activateTab("overview"));
    nav.appendChild(all);
    const instances = (list || []).slice()
      .sort((a, b) => a.name.localeCompare(b.name, "de"));
    for (const inst of instances) {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "inst-tab" + (onServers && inst.id === state.detailId ? " active" : "");
      const icon = document.createElement("img");
      icon.className = "inst-tab-icon";
      icon.src = serverBlockIcon(inst.name);
      icon.alt = "";
      const dot = document.createElement("span");
      dot.className = `inst-tab-dot ${instStateKey(inst)}`;
      dot.title = OV_STATE_LABELS[instDisplayKey(inst)];
      const count = document.createElement("span");
      count.className = "inst-tab-count hidden";
      b.dataset.id = inst.id;
      b.append(icon, document.createTextNode(inst.name), count, dot);
      b.title = `${inst.loader} · MC ${inst.game_version} · Port ${inst.port}`;
      b.addEventListener("click", () => {
        activateTab("servers");
        if (inst.id !== state.detailId) openDetail(inst.id);
      });
      nav.appendChild(b);
    }
    const add = document.createElement("button");
    add.type = "button";
    add.className = "inst-tab add admin-only";
    add.textContent = "+ Server";
    add.title = "Neuen Server erstellen";
    add.addEventListener("click", () => {
      activateTab("servers");
      openCreate();
    });
    nav.appendChild(add);
    updateHotbarCounts();
    updateInstTabCounts();
  }

  /* ---------- Workspace-Steuerung ---------- */
  function currentDetailInst() {
    return (state.ovInstances?.instances || [])
      .find((i) => i.id === state.detailId) || null;
  }

  function updateWsControls() {
    if (!state.detailId) return;
    const inst = currentDetailInst() || state.detailData;
    if (!inst) return;
    const key = instStateKey(inst);
    const running = key === "running";
    const starting = key === "starting";
    $("#ws-start").disabled = running || starting;
    $("#ws-stop").disabled = !running;
    $("#ws-restart").disabled = !running;
    const badge = $("#detail-state");
    badge.className = "badge inst-badge";
    badge.classList.add(stateBadgeClass(inst));
    setText($("#detail-state-text"), OV_STATE_LABELS[instDisplayKey(inst)]);
    const up = running && inst.container?.started_at
      ? fmtUptime(inst.container.started_at) : null;
    setText($("#ws-uptime"), up ? `Läuft seit ${up}` : ""); // Status steht schon im Badge
  }

  function updateWsPerf() {
    if (!state.detailId) return;
    const cpuVal = $("#ws-cpu-val");
    if (!cpuVal) return;
    const res = containerStatsById()[`mc-inst-${state.detailId}`];
    if (res && currentDetailInst()?.container?.running) {
      setText(cpuVal, `${res.cpu_percent} %`);
      $("#ws-cpu-bar").style.width =
        `${Math.max(2, Math.min(100, res.cpu_percent))}%`;
      setText($("#ws-ram-val"), res.ram_limit_mb
        ? `${fmtMb(res.ram_mb)} / ${fmtMb(res.ram_limit_mb)}` : fmtMb(res.ram_mb));
      $("#ws-ram-bar").style.width = res.ram_limit_mb
        ? `${Math.max(2, Math.min(100, (res.ram_mb / res.ram_limit_mb) * 100))}%`
        : `${Math.max(2, Math.min(100, res.ram_mb))}%`;
    } else {
      setText(cpuVal, "–");
      $("#ws-cpu-bar").style.width = "0%";
      setText($("#ws-ram-val"), "–");
      $("#ws-ram-bar").style.width = "0%";
    }
    const hist = state.ovHistory.cont[`mc-inst-${state.detailId}`];
    setSpark("#ws-cpu-spark", hist?.cpu || [], "", 100);
    setSpark("#ws-ram-spark", hist?.ram || [], "", res?.ram_limit_mb || null);
  }

  for (const [sel, action] of [
    ["#ws-start", "start"],
    ["#ws-stop", "stop"],
    ["#ws-restart", "restart"],
  ]) {
    $(sel).addEventListener("click", () => {
      const inst = currentDetailInst()
        || (state.detailData
          ? { id: state.detailId, name: state.detailData.name }
          : null);
      if (inst) instAction(inst, action);
    });
  }

  /* Chat-Kurzsprung: Nachricht an alle Spieler (say) */
  async function sendWsChat() {
    if (!state.detailId) return;
    const input = $("#ws-chat-input");
    const text = input.value.trim();
    if (!text) return;
    show($("#ws-controls-error"), false);
    input.value = "";
    const cmd = `say ${text}`;
    try {
      const data = await api(`/api/instances/${state.detailId}/console`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ command: cmd }),
      });
      appendConsoleLine(`> ${cmd}`);
      appendConsoleLine(data.output || "(gesendet)");
    } catch (e) {
      setText($("#ws-controls-error"), `Senden fehlgeschlagen: ${e.message}`);
      show($("#ws-controls-error"), true);
    }
  }
  $("#ws-chat-send").addEventListener("click", sendWsChat);
  $("#ws-addr").addEventListener("click", () => {
    const inst = currentDetailInst() || state.detailData;
    if (inst) copyAddress(inst);
  });
  $("#ws-chat-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      sendWsChat();
    }
  });

  /* Auto-Start-Schnellschalter (spiegelt den Zeitplan-Eintrag) */
  $("#ws-autostart").addEventListener("change", async (e) => {
    if (!state.detailId) return;
    const checked = e.target.checked;
    const sched = JSON.parse(JSON.stringify(state.detailData?.schedule || {}));
    sched.auto_start = checked;
    show($("#ws-controls-error"), false);
    try {
      await api(`/api/instances/${state.detailId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ schedule: sched }),
      });
      $("#sched-autostart").checked = checked;
      toast(checked
        ? "Auto-Start aktiviert — Server startet mit dem Dashboard."
        : "Auto-Start deaktiviert.", "success");
    } catch (err) {
      e.target.checked = !checked;
      setText($("#ws-controls-error"), `Auto-Start nicht gespeichert: ${err.message}`);
      show($("#ws-controls-error"), true);
    }
  });

  /* Backup-Kompaktpanel: Schalter in die Backup-Verwaltung */
  $("#ws-backup-now").addEventListener("click", () =>
    $("#backup-create-btn").click());
  $("#ws-backups-open").addEventListener("click", () => activateWsTab("backups"));

  organizeWorkspace();

  /* =====================================================================
     Aktivität & Chat: Beitritte, Chat, Tode und Erfolge aus dem Live-Log
     ===================================================================== */
  state.activity = [];
  const ACT_MAX = 80;
  const MC_NAME = "([A-Za-z0-9_]{2,16})";
  const DEATH_RE = new RegExp(`^${MC_NAME} (was (?:slain|shot|killed|blown up|fireballed|pummeled|stung|impaled|squashed|squished|struck by lightning|pricked|poked|skewered|obliterated|frozen|doomed|knocked|burnt|roasted)|drowned|died|fell |hit the ground|burned to death|went up in flames|went off with a bang|blew up|tried to swim in lava|starved|suffocated|froze to death|withered away|walked into|discovered the floor was lava|experienced kinetic energy|left the confines|didn't want to live)`);

  /* Eine Logzeile → Ereignis {type, name, text, time} oder null */
  function parseActivity(line) {
    const raw = String(line || "");
    // Vanilla/Fabric: "[12:34:56] [Server thread/INFO]: …", Paper: "[12:34:56 INFO]: …"
    const m = /^\[(\d{1,2}:\d{2})(?::\d{2})?[^\]]*\](?:\s*\[[^\]]*\])?:\s?(.*)$/.exec(raw);
    const time = m ? m[1] : "";
    const msg = (m ? m[2] : raw).trim();
    let r;
    if ((r = new RegExp(`^${MC_NAME} joined the game$`).exec(msg))) {
      return { type: "join", name: r[1], text: "ist beigetreten", time };
    }
    if ((r = new RegExp(`^${MC_NAME} left the game$`).exec(msg))) {
      return { type: "leave", name: r[1], text: "hat den Server verlassen", time };
    }
    if ((r = new RegExp(`^(?:\\[Not Secure\\] )?<${MC_NAME}> (.+)$`).exec(msg))) {
      return { type: "chat", name: r[1], text: r[2], time };
    }
    if ((r = /^(?:\[Not Secure\] )?\[(Server|Rcon)\] (.+)$/.exec(msg))) {
      return { type: "chat server", name: "Server", text: r[2], time };
    }
    if ((r = new RegExp(`^${MC_NAME} has (made the advancement|completed the challenge|reached the goal) \\[(.+)\\]$`).exec(msg))) {
      const kind = r[2] === "made the advancement" ? "Fortschritt"
        : r[2] === "completed the challenge" ? "Aufgabe" : "Ziel";
      return { type: "adv", name: r[1], text: `${kind} erzielt: ${r[3]}`, time };
    }
    if ((r = DEATH_RE.exec(msg))) {
      return { type: "death", name: r[1], text: msg.slice(r[1].length + 1), time };
    }
    if (/^Done \([\d.,]+s\)!/.test(msg)) {
      return { type: "server", name: "Server", text: "ist bereit — Spieler können beitreten", time };
    }
    if (/^Stopping (the )?server/.test(msg)) {
      return { type: "server", name: "Server", text: "wird gestoppt", time };
    }
    return null;
  }

  let activityFrame = 0;
  function feedActivity(line) {
    const ev = parseActivity(line);
    if (!ev) return;
    // Neu verbundener Stream schickt die letzten Zeilen erneut → doppelte überspringen
    if (ev.time && state.activity.some((x) => x.time === ev.time && x.type === ev.type
        && x.name === ev.name && x.text === ev.text)) return;
    state.activity.push(ev);
    if (state.activity.length > ACT_MAX) state.activity.splice(0, state.activity.length - ACT_MAX);
    if (!activityFrame) {
      activityFrame = requestAnimationFrame(() => { activityFrame = 0; renderActivity(); });
    }
  }
  function rebuildActivity(lines) {
    state.activity = [];
    for (const line of lines) {
      const ev = parseActivity(line);
      if (ev) state.activity.push(ev);
    }
    state.activity = state.activity.slice(-ACT_MAX);
    renderActivity();
  }

  const ACT_SERVER_ICON = SLOT_ICONS.cmd;
  function renderActivity() {
    const ul = $("#ws-activity");
    if (!ul) return;
    const atBottom = ul.scrollTop + ul.clientHeight >= ul.scrollHeight - 8;
    ul.textContent = "";
    for (const ev of state.activity) {
      const li = document.createElement("li");
      li.className = `act act-${ev.type.split(" ")[0]}${ev.type.includes("server") ? " act-from-server" : ""}`;
      const img = document.createElement("img");
      img.alt = "";
      img.src = ev.name === "Server" ? ACT_SERVER_ICON : playerFace(ev.name);
      const body = document.createElement("div");
      body.className = "act-body";
      const who = document.createElement("b");
      who.textContent = ev.name;
      const text = document.createElement("span");
      text.className = "act-text";
      text.textContent = ev.type.startsWith("chat") ? `: ${ev.text}` : ` ${ev.text}`;
      body.append(who, text);
      li.append(img, body);
      if (ev.time) {
        const t = document.createElement("time");
        t.className = "act-time muted small";
        t.textContent = ev.time;
        li.appendChild(t);
      }
      ul.appendChild(li);
    }
    show($("#ws-activity-empty"), state.activity.length === 0);
    show(ul, state.activity.length > 0);
    if (atBottom) ul.scrollTop = ul.scrollHeight;
  }
  renderActivity();

  /* =====================================================================
     Benachrichtigungen: Server bereit, unerwartet gestoppt, Spieler-Beitritte
     — im sichtbaren Tab als Hinweis, im Hintergrund als Desktop-Meldung
     ===================================================================== */
  state.userActionAt = state.userActionAt || {};
  state.notifySnap = null; // id -> {key, ready, online, names: Set|null}
  try { state.notify = localStorage.getItem("md_notify") === "1"; } catch (e) { state.notify = false; }

  function desktopNotifyOk() {
    return "Notification" in window && window.isSecureContext
      && Notification.permission === "granted";
  }
  function renderNotifyMenu() {
    const btn = $("#menu-notify");
    const label = !state.notify ? "aus" : desktopNotifyOk() ? "an" : "an (nur im Dashboard)";
    setText(btn, `Benachrichtigungen: ${label}`);
    btn.setAttribute("aria-checked", String(!!state.notify));
    btn.title = "Hinweise bei Server bereit, unerwartetem Stopp und Spieler-Beitritten";
  }
  $("#menu-notify").addEventListener("click", async () => {
    state.notify = !state.notify;
    try { localStorage.setItem("md_notify", state.notify ? "1" : "0"); } catch (e) { /* optional */ }
    if (state.notify) {
      if (!("Notification" in window) || !window.isSecureContext) {
        toast("Benachrichtigungen an. Desktop-Meldungen braucht der Browser per HTTPS oder localhost — bis dahin erscheinen sie nur hier im Dashboard.", "info");
      } else if (Notification.permission === "default") {
        try { await Notification.requestPermission(); } catch (e) { /* alte Browser */ }
      }
      if (desktopNotifyOk()) toast("Benachrichtigungen an — auch wenn der Tab im Hintergrund liegt.", "success");
      else if (window.isSecureContext && "Notification" in window && Notification.permission === "denied") {
        toast("Benachrichtigungen an, Desktop-Meldungen sind im Browser blockiert — sie erscheinen nur hier im Dashboard.", "info");
      }
    } else {
      toast("Benachrichtigungen aus.", "info");
    }
    renderNotifyMenu();
  });
  renderNotifyMenu();

  function notifyUser(inst, text, type) {
    if (!state.notify) return;
    if (document.hidden && desktopNotifyOk()) {
      try {
        const n = new Notification(`${inst.name} · Minedocker`, { body: text, tag: `md-${inst.id}-${type}` });
        n.onclick = () => { window.focus(); openServer(inst.id); n.close(); };
        return;
      } catch (e) { /* z. B. Android ohne Service Worker → Hinweis im Dashboard */ }
    }
    toast(`${inst.name}: ${text}`, type);
  }

  function checkNotifications(list) {
    if (state.ovInstances === null) return; // Fehler beim Laden ≠ alle Server weg
    const snap = {};
    for (const inst of list) {
      const key = instStateKey(inst);
      const ping = inst.container?.running ? liveEntry(inst)?.ping : null;
      const ready = !!ping?.online;
      const online = ready ? (ping.players?.online || 0) : 0;
      const sample = ready ? (ping.players?.sample || []).map((p) => p.name).filter(Boolean) : [];
      // Namen nur verwenden, wenn der Server alle liefert (sonst nur Zähler)
      snap[inst.id] = { key, ready, online, names: sample.length === online ? new Set(sample) : null };
    }
    const prev = state.notifySnap;
    state.notifySnap = snap;
    if (!prev) return; // erste Messung: nur merken
    for (const inst of list) {
      const was = prev[inst.id];
      const now = snap[inst.id];
      if (!was) continue;
      const byUser = Date.now() - (state.userActionAt[inst.id] || 0) < 3 * 60 * 1000;
      if (was.key === "running" && now.key !== "running" && now.key !== "starting" && !byUser) {
        notifyUser(inst, now.key === "error"
          ? "ist abgestürzt" : "wurde unerwartet gestoppt (nicht über das Dashboard)", "error");
      }
      if (!was.ready && now.ready) {
        notifyUser(inst, "ist bereit — Spieler können beitreten", "success");
      }
      if (was.ready && now.ready && now.online > was.online) {
        const joined = was.names && now.names
          ? [...now.names].filter((n) => !was.names.has(n)) : [];
        notifyUser(inst, joined.length
          ? `${joined.join(", ")} ${joined.length === 1 ? "ist" : "sind"} beigetreten (${now.online} online)`
          : `${now.online - was.online === 1 ? "Ein Spieler ist" : `${now.online - was.online} Spieler sind`} beigetreten (${now.online} online)`,
        "info");
      }
    }
  }

  /* =====================================================================
     Befehlspalette (Strg+K): Server, Bereiche, Aktionen und /Befehle
     ===================================================================== */
  const cmdk = { items: [], shown: [], active: 0 };
  const canAdmin = () => state.role !== "viewer";
  const allInstances = () => (state.ovInstances?.instances || []).slice()
    .sort((a, b) => a.name.localeCompare(b.name, "de"));
  const NAV_ICON = pixelIcon(["...ss...", "..sYYs..", ".sYwwYs.", "sYwkkwYs", "sYwkkwYs", ".sYwwYs.", "..sYYs..", "...ss..."]);
  const ACT_ICONS = {
    start: pixelIcon(["........", ".ee.....", ".eeee...", ".eeeeee.", ".eeeeee.", ".eeee...", ".ee.....", "........"]),
    stop: pixelIcon(["........", ".rrrrrr.", ".rRRRRr.", ".rRRRRr.", ".rRRRRr.", ".rRRRRr.", ".rrrrrr.", "........"]),
    restart: pixelIcon(["..yyyy..", ".y....y.", "y.......", "y....yyy", "y.....yy", ".y....y.", "..yyyy..", "........"]),
    add: pixelIcon(["........", "...ee...", "...ee...", ".eeeeee.", ".eeeeee.", "...ee...", "...ee...", "........"]),
  };

  // Ziel für /Befehle: geöffneter Server, sonst der einzige laufende
  function cmdkConsoleTargets() {
    const running = allInstances().filter((i) => instStateKey(i) === "running");
    const cur = running.find((i) => i.id === state.detailId);
    return cur ? [cur, ...running.filter((i) => i !== cur)] : running;
  }

  async function cmdkSendCommand(inst, cmd) {
    try {
      const data = await api(`/api/instances/${inst.id}/console`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ command: cmd }),
      });
      if (inst.id === state.detailId) {
        appendConsoleLine(`> ${cmd}`);
        appendConsoleLine(data.output || "(keine Antwort)");
      }
      const out = (data.output || "").trim();
      toast(`${inst.name}: /${cmd}${out ? ` → ${out.length > 160 ? `${out.slice(0, 160)}…` : out}` : ""}`, "success");
    } catch (e) {
      toast(`${inst.name}: Befehl fehlgeschlagen — ${e.message}`, "error");
    }
  }

  function buildCmdkItems() {
    const items = [];
    const add = (group, label, opts) => items.push({ group, label, ...opts });
    const insts = allInstances();
    const cur = currentDetailInst();

    for (const inst of insts) {
      const key = instStateKey(inst);
      const n = onlinePlayers(inst);
      add("Server", inst.name, {
        icon: cardIcon(inst),
        hint: OV_STATE_LABELS[key] + (n !== null ? ` · ${n} online` : ""),
        keywords: `${inst.loader} ${inst.game_version} ${(inst.tags || []).join(" ")} öffnen`,
        dot: key,
        run: () => openServer(inst.id),
      });
    }

    // Bereiche: für den geöffneten Server immer, für andere nur bei Suche
    for (const inst of insts) {
      const isCur = cur && inst.id === cur.id && state.tab === "servers";
      WS_TABS.forEach(([slot, label, icon], i) => {
        add("Bereiche", isCur ? label : `${inst.name} › ${label}`, {
          icon: SLOT_ICONS[icon],
          hint: isCur ? `Taste ${i + 1}` : "",
          keywords: inst.name,
          deep: !isCur,
          run: () => openServer(inst.id, slot),
        });
      });
    }

    if (canAdmin()) {
      for (const inst of insts) {
        const key = instStateKey(inst);
        const isCur = cur && inst.id === cur.id;
        const acts = key === "running"
          ? [["restart", "neu starten"], ["stop", "stoppen"]]
          : key === "starting" ? [["stop", "stoppen"]] : [["start", "starten"]];
        for (const [action, verb] of acts) {
          add("Aktionen", `${inst.name} ${verb}`, {
            icon: ACT_ICONS[action],
            keywords: action === "restart" ? "neustart restart" : action,
            deep: !isCur,
            run: () => instAction(inst, action),
          });
        }
        add("Aktionen", `Backup von ${inst.name} erstellen`, {
          icon: SLOT_ICONS.chest,
          keywords: "sichern sicherung backup",
          deep: !isCur,
          run: () => { openServer(inst.id, "backups"); $("#backup-create-btn").click(); },
        });
      }
      add("Aktionen", "Neuen Server erstellen", {
        icon: ACT_ICONS.add, keywords: "anlegen new server instanz",
        run: () => { activateTab("servers"); openCreate(); },
      });
      if (insts.some((i) => instStateKey(i) === "stopped")) {
        add("Aktionen", "Alle gestoppten Server starten", {
          icon: ACT_ICONS.start, keywords: "alle start", run: () => $("#ov-start-all").click(),
        });
      }
      if (insts.some((i) => instStateKey(i) === "running")) {
        add("Aktionen", "Alle laufenden Server stoppen", {
          icon: ACT_ICONS.stop, keywords: "alle stop", run: () => $("#ov-stop-all").click(),
        });
      }
    }

    // Spieler (aus dem Live-Ping): springt in den Spieler-Slot ihres Servers
    for (const inst of insts) {
      const ping = inst.container?.running ? liveEntry(inst)?.ping : null;
      for (const p of (ping?.online && ping.players?.sample) || []) {
        if (!p.name) continue;
        add("Spieler", p.name, {
          icon: playerFace(p.name), hint: `auf ${inst.name}`, keywords: inst.name, deep: true,
          run: () => openServer(inst.id, "spieler"),
        });
      }
    }

    document.querySelectorAll(".tabs .tab").forEach((tab) => {
      const label = tab.querySelector(".tab-label")?.textContent || tab.dataset.tab;
      add("Navigation", `Gehe zu ${label}`, {
        icon: NAV_ICON, keywords: tab.dataset.tab, run: () => activateTab(tab.dataset.tab),
      });
    });
    return items;
  }

  function filterCmdk(query) {
    const q = query.trim().toLowerCase();
    // „/befehl“ → an einen laufenden Server per RCON senden
    if (q.startsWith("/")) {
      const cmd = query.trim().replace(/^\/+/, "");
      const targets = canAdmin() ? cmdkConsoleTargets() : [];
      if (!targets.length) {
        return [{ group: "Befehl", label: canAdmin()
          ? "Kein laufender Server für Befehle" : "Befehle sind nur für Admins",
          disabled: true }];
      }
      return targets.map((inst) => ({
        group: "Befehl",
        label: cmd ? `/${cmd}` : "Befehl eingeben …",
        hint: `an ${inst.name}`,
        icon: SLOT_ICONS.cmd,
        disabled: !cmd,
        run: () => cmdkSendCommand(inst, cmd),
      }));
    }
    const tokens = q.split(/\s+/).filter(Boolean);
    const groups = ["Server", "Bereiche", "Aktionen", "Spieler", "Navigation"];
    const out = [];
    for (const g of groups) {
      const hits = [];
      cmdk.items.forEach((it, idx) => {
        if (it.group !== g) return;
        if (!tokens.length) { if (!it.deep) hits.push([0, idx, it]); return; }
        const label = it.label.toLowerCase();
        const hay = `${label} ${(it.keywords || "").toLowerCase()} ${g.toLowerCase()}`;
        if (!tokens.every((t) => hay.includes(t))) return;
        const score = label.startsWith(q) ? 0 : label.includes(q) ? 1
          : tokens.every((t) => label.includes(t)) ? 2 : 3;
        hits.push([score, idx, it]);
      });
      hits.sort((a, b) => a[0] - b[0] || a[1] - b[1]);
      out.push(...hits.slice(0, tokens.length ? 8 : 30).map((h) => h[2]));
    }
    return out;
  }

  function renderCmdk() {
    const list = $("#cmdk-list");
    list.textContent = "";
    cmdk.shown = filterCmdk($("#cmdk-input").value);
    if (cmdk.active >= cmdk.shown.length) cmdk.active = 0;
    let lastGroup = null;
    cmdk.shown.forEach((it, i) => {
      if (it.group !== lastGroup) {
        lastGroup = it.group;
        const h = document.createElement("div");
        h.className = "cmdk-group";
        h.textContent = it.group;
        list.appendChild(h);
      }
      const row = document.createElement("div");
      row.className = "cmdk-item" + (i === cmdk.active ? " active" : "")
        + (it.disabled ? " disabled" : "");
      row.id = `cmdk-opt-${i}`;
      row.setAttribute("role", "option");
      row.setAttribute("aria-selected", String(i === cmdk.active));
      if (it.icon) {
        const img = document.createElement("img");
        img.src = it.icon;
        img.alt = "";
        row.appendChild(img);
      }
      const label = document.createElement("span");
      label.className = "cmdk-label";
      label.textContent = it.label;
      row.appendChild(label);
      if (it.dot) {
        const dot = document.createElement("span");
        dot.className = `inst-tab-dot ${it.dot}`;
        row.appendChild(dot);
      }
      if (it.hint) {
        const hint = document.createElement("span");
        hint.className = "cmdk-hint-text";
        hint.textContent = it.hint;
        row.appendChild(hint);
      }
      row.addEventListener("mousemove", () => {
        if (cmdk.active === i) return;
        cmdk.active = i;
        list.querySelectorAll(".cmdk-item").forEach((el) => {
          const on = el.id === row.id;
          el.classList.toggle("active", on);
          el.setAttribute("aria-selected", String(on));
        });
        $("#cmdk-input").setAttribute("aria-activedescendant", row.id);
      });
      row.addEventListener("click", () => runCmdk(i));
      list.appendChild(row);
    });
    if (!cmdk.shown.length) {
      const empty = document.createElement("div");
      empty.className = "cmdk-empty muted";
      empty.textContent = "Nichts gefunden — mit „/“ beginnt ein Server-Befehl.";
      list.appendChild(empty);
    }
    $("#cmdk-input").setAttribute("aria-activedescendant", cmdk.shown.length ? `cmdk-opt-${cmdk.active}` : "");
    list.querySelector(".cmdk-item.active")?.scrollIntoView({ block: "nearest" });
    const target = cmdkConsoleTargets()[0];
    setText($("#cmdk-target"), target ? target.name : "laufenden Server");
  }

  function runCmdk(i) {
    const it = cmdk.shown[i];
    if (!it || it.disabled || !it.run) return;
    closeDialog($("#cmdk"));
    it.run();
  }

  function openCmdk() {
    const dlg = $("#cmdk");
    if (dlg.open) return;
    if (document.querySelector("dialog[open]")) return; // andere Dialoge nicht überlagern
    cmdk.items = buildCmdkItems();
    cmdk.active = 0;
    $("#cmdk-input").value = "";
    renderCmdk();
    dlg.showModal();
    $("#cmdk-input").focus();
  }

  $("#cmdk-input").addEventListener("input", () => { cmdk.active = 0; renderCmdk(); });
  $("#cmdk-input").addEventListener("keydown", (e) => {
    const n = cmdk.shown.length;
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (!n) return;
      cmdk.active = (cmdk.active + (e.key === "ArrowDown" ? 1 : n - 1)) % n;
      renderCmdk();
    } else if (e.key === "Enter") {
      e.preventDefault();
      runCmdk(cmdk.active);
    }
  });
  $("#cmdk").addEventListener("cancel", (e) => { e.preventDefault(); closeDialog($("#cmdk")); });
  // Klick auf den Hintergrund schließt die Palette
  $("#cmdk").addEventListener("click", (e) => {
    if (e.target === e.currentTarget) closeDialog($("#cmdk"));
  });
  $("#cmdk-open").addEventListener("click", openCmdk);
  $("#ov-cmdk").addEventListener("click", openCmdk);
  document.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && !e.altKey && !e.shiftKey && e.key.toLowerCase() === "k") {
      if (!$("#auth-overlay").classList.contains("hidden")) return; // nicht über dem Login
      e.preventDefault();
      if ($("#cmdk").open) closeDialog($("#cmdk"));
      else openCmdk();
    }
  });

})();