// Yvara AI – lokale Chat-Oberfläche
const $ = (s) => document.querySelector(s);
const api = async (url, opts = {}) => {
  // Gibt immer ein Objekt zurück; bei Fehlern {ok:false, error:"…"} statt einer Exception
  try {
    const r = await fetch(url, { headers: { "Content-Type": "application/json" }, ...opts,
      body: opts.body && typeof opts.body !== "string" && !(opts.body instanceof FormData) ? JSON.stringify(opts.body) : opts.body });
    const text = await r.text();
    let data; try { data = JSON.parse(text); } catch { data = { error: text }; }
    if (!r.ok) return { ...(typeof data === "object" && data ? data : {}), ok: false, error: (data && data.error) || text || `Fehler ${r.status}` };
    return data;
  } catch (e) { return { ok: false, error: "Yvara AI ist nicht erreichbar – läuft das Programm noch?" }; }
};
const failed = (r) => !r || r.ok === false;
// Grüne/rote Rückmeldung unten rechts (+ optional Button kurz aufleuchten/wackeln lassen)
function toast(text, ok = true, el = null) {
  const t = document.createElement("div");
  t.className = "toast " + (ok ? "ok" : "err");
  t.innerHTML = `<span class="ic">${ok ? "✓" : "✕"}</span><span></span>`;
  t.lastChild.textContent = text;
  $("#toasts").appendChild(t);
  setTimeout(() => t.remove(), 3100);
  if (el) { const c = ok ? "flash-ok" : "flash-err"; el.classList.remove(c); void el.offsetWidth; el.classList.add(c); }
}
async function act(btn, promise, okText, errText) {
  if (btn) btn.disabled = true;
  const r = await promise;
  if (btn) btn.disabled = false;
  if (failed(r)) toast(`${errText}${r && r.error ? ": " + r.error : ""}`, false, btn);
  else toast(okText, true, btn);
  return r;
}
let state = {}, sessionId = null, audioEl = new Audio();

// --- Mini-Markdown (offline, HTML wird escaped) ---------------------------
function esc(s) { return s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }
function md(src) {
  const lines = esc(src || "").split("\n"); let html = "", inList = false;
  const inline = (t) => t.replace(/`([^`]+)`/g, "<code>$1</code>").replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
    .replace(/\*([^*]+)\*/g, "<i>$1</i>").replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  for (const l of lines) {
    const li = l.match(/^\s*[-*•]\s+(.*)/) || l.match(/^\s*\d+[.)]\s+(.*)/);
    if (li) { if (!inList) { html += "<ul>"; inList = true; } html += `<li>${inline(li[1])}</li>`; continue; }
    if (inList) { html += "</ul>"; inList = false; }
    const h = l.match(/^(#{1,3})\s+(.*)/);
    if (h) html += `<h${h[1].length}>${inline(h[2])}</h${h[1].length}>`;
    else if (l.trim()) html += `<p>${inline(l)}</p>`;
  }
  return html + (inList ? "</ul>" : "");
}

// --- Nachrichten ----------------------------------------------------------
function addMsg(role, text, extra = "") {
  $("#emptyState")?.classList.add("hidden");
  const d = document.createElement("div");
  d.className = `msg ${role} ${extra}`;
  d.innerHTML = `<div class="who">${role === "user" ? "Du" : esc(state.name || "Yvara AI")}</div><div class="md">${md(text)}</div>`;
  $("#messages").appendChild(d);
  $("#messages").scrollTop = 1e9;
  return d;
}
function play(url) { if (url && $("#speakToggle").checked) { audioEl.src = url; audioEl.play().catch(() => {}); } }

async function ensureSession() {
  if (!sessionId) { sessionId = (await api("/api/sessions", { method: "POST" })).id; }
  return sessionId;
}

async function send(text) {
  if (!text.trim()) return;
  await ensureSession();
  addMsg("user", text);
  const wait = addMsg("assistant", "denkt nach …", "thinking");
  const res = await api("/api/chat", { method: "POST", body: { session_id: sessionId, text, web: $("#webToggle").checked, speak: $("#speakToggle").checked } });
  wait.remove();
  addMsg("assistant", res.reply || res.error || "Fehler");
  play(res.audio);
  loadSessions();
}

$("#composer").addEventListener("submit", (e) => { e.preventDefault(); const t = $("#input").value; $("#input").value = ""; send(t); });
$("#input").addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("#composer").requestSubmit(); } });
$("#input").addEventListener("input", (e) => { e.target.style.height = "auto"; e.target.style.height = e.target.scrollHeight + "px"; });

// --- Spracheingabe (gedrückt halten) --------------------------------------
let rec = null, chunks = [];
async function startRec() {
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    rec = new MediaRecorder(stream); chunks = [];
    rec.ondataavailable = (e) => chunks.push(e.data);
    rec.onstop = async () => {
      stream.getTracks().forEach((t) => t.stop());
      const blob = new Blob(chunks, { type: rec.mimeType });
      if (blob.size < 2000) return;
      await ensureSession();
      const wait = addMsg("assistant", "hört zu …", "thinking");
      const fd = new FormData();
      fd.append("audio", blob, "voice.webm"); fd.append("session_id", sessionId); fd.append("web", $("#webToggle").checked);
      const r = await fetch("/api/voice", { method: "POST", body: fd }).then((r) => r.json());
      wait.remove();
      if (r.error) { addMsg("assistant", r.error); return; }
      addMsg("user", "🎙️ " + r.text); addMsg("assistant", r.reply); play(r.audio); loadSessions();
    };
    rec.start(); $("#micBtn").classList.add("rec");
  } catch (e) { addMsg("assistant", "Mikrofon nicht verfügbar: " + e.message); }
}
function stopRec() { if (rec && rec.state === "recording") rec.stop(); $("#micBtn").classList.remove("rec"); }
["mousedown", "touchstart"].forEach((ev) => $("#micBtn").addEventListener(ev, (e) => { e.preventDefault(); startRec(); }));
["mouseup", "mouseleave", "touchend"].forEach((ev) => $("#micBtn").addEventListener(ev, stopRec));

// --- Chats ----------------------------------------------------------------
async function loadSessions() {
  const list = await api("/api/sessions");
  $("#sessionList").innerHTML = "";
  for (const s of list) {
    const li = document.createElement("li");
    li.className = s.id === sessionId ? "active" : "";
    li.innerHTML = `<span>${esc(s.title || "Chat")}</span><b class="x" title="Löschen">✕</b>`;
    li.onclick = () => openSession(s.id);
    li.querySelector(".x").onclick = async (e) => { e.stopPropagation(); await api(`/api/sessions/${s.id}`, { method: "DELETE" }); if (sessionId === s.id) newChat(); loadSessions(); };
    $("#sessionList").appendChild(li);
  }
}
async function openSession(id) {
  sessionId = id;
  $("#messages").querySelectorAll(".msg").forEach((m) => m.remove());
  const msgs = await api(`/api/sessions/${id}`);
  if (!msgs.length) $("#emptyState").classList.remove("hidden");
  msgs.forEach((m) => addMsg(m.role, m.text));
  loadSessions();
}
function newChat() { sessionId = null; $("#messages").querySelectorAll(".msg").forEach((m) => m.remove()); $("#emptyState").classList.remove("hidden"); loadSessions(); }
$("#newChatBtn").onclick = newChat;

// --- Teams ----------------------------------------------------------------
$("#teamSelect").onchange = async (e) => {
  const r = await api("/api/teams/activate", { method: "POST", body: { slug: e.target.value } });
  if (failed(r)) toast(r.error || "Teamwechsel fehlgeschlagen", false, $("#teamSelect"));
  else { toast(`Coache jetzt ${r.team ? r.team.name : "das Team"}`, true, $("#teamSelect")); newChat(); }
  await refresh(true);
};
// Neues Team: eigenes Eingabefeld statt Browser-Popup (Popups werden von manchen Browsern/Erweiterungen blockiert)
$("#newTeamBtn").onclick = () => { $("#newTeamRow").classList.remove("hidden"); $("#newTeamName").value = ""; $("#newTeamName").focus(); };
$("#newTeamCancel").onclick = () => $("#newTeamRow").classList.add("hidden");
$("#newTeamName").onkeydown = (e) => { if (e.key === "Enter") $("#newTeamOk").click(); if (e.key === "Escape") $("#newTeamCancel").click(); };
$("#newTeamOk").onclick = async () => {
  const name = $("#newTeamName").value.trim();
  if (!name) { toast("Bitte einen Teamnamen eingeben", false, $("#newTeamName")); return; }
  if ((state.teams || []).some((t) => t.name.toLowerCase() === name.toLowerCase())) { toast("Das Team gibt es schon", false, $("#newTeamName")); return; }
  const t = await api("/api/teams", { method: "POST", body: { name, create: true } });
  if (failed(t)) { toast("Team konnte nicht angelegt werden: " + (t.error || ""), false, $("#newTeamOk")); return; }
  const r = await api("/api/teams/activate", { method: "POST", body: { slug: t.slug } });
  $("#newTeamRow").classList.add("hidden");
  if (failed(r)) toast(`Team „${name}“ angelegt, aber ${r.error || "nicht aktiviert"}`, false);
  else toast(`Team „${name}“ angelegt`, true);
  newChat(); await refresh(true);
};
function playerRow(p = {}) {
  const d = document.createElement("div"); d.className = "player-row";
  d.innerHTML = `<input placeholder="In-Game-Name" value="${esc(p.ingame || "")}"><input placeholder="Rolle" value="${esc(p.role || "")}"><button title="Entfernen">✕</button>`;
  d.querySelector("button").onclick = () => d.remove();
  $("#players").appendChild(d);
}
$("#addPlayer").onclick = () => playerRow();
$("#saveTeam").onclick = async () => {
  const players = [...document.querySelectorAll(".player-row")].map((r) => { const [a, b] = r.querySelectorAll("input"); return { ingame: a.value.trim(), role: b.value.trim() }; });
  if (!$("#tName").value.trim()) { toast("Teamname darf nicht leer sein", false, $("#tName")); return; }
  const r = await act($("#saveTeam"), api("/api/teams", { method: "POST", body: {
    slug: state.team.slug, name: $("#tName").value.trim(), players, notes: $("#tNotes").value } }), "Team gespeichert", "Speichern fehlgeschlagen");
  if (!failed(r)) await refresh(true);
};
$("#profSelect").onchange = async (e) => {
  const [kind, ...n] = e.target.value.split(":");
  $("#profile").innerHTML = md((await api(`/api/profile?kind=${kind}&name=${encodeURIComponent(n.join(":"))}`)).text);
};

// --- Gedächtnis ----------------------------------------------------------
async function loadMemories() {
  const mems = await api("/api/memories");
  $("#memList").innerHTML = "";
  for (const m of mems) {
    const li = document.createElement("li");
    const src = { "gespräch": "🗣️ aus Gespräch", manual: "✍️ von dir", chat: "💬 Chat", autolearn: "🎓 gelernt", match: "🎮 Match" }[m.source] || "";
    li.innerHTML = `<span>${esc(m.text)}${src ? ` <small class="hint">${src}</small>` : ""}</span><b class="x">✕</b>`;
    li.querySelector(".x").onclick = async () => { await api(`/api/memories/${m.id}`, { method: "DELETE" }); loadMemories(); };
    $("#memList").appendChild(li);
  }
}
$("#memAdd").onclick = async () => {
  const t = $("#memInput").value.trim(); if (!t) { toast("Erst etwas eintippen", false, $("#memInput")); return; }
  const r = await act($("#memAdd"), api("/api/memories", { method: "POST", body: { text: t } }), "Gemerkt", "Konnte nicht speichern");
  if (!failed(r)) $("#memInput").value = ""; loadMemories();
};

// --- Match ----------------------------------------------------------------
const matchBody = () => ({ mode: $("#mMode").value, map: $("#mMap").value, opponent: $("#mOpp").value });
$("#startBtn").onclick = async () => { await act($("#startBtn"), api("/api/match/start", { method: "POST", body: matchBody() }), "Match gestartet", "Start fehlgeschlagen"); refresh(); };
$("#endBtn").onclick = async () => {
  addMsg("assistant", "Match beendet – ich schreibe das Review und lerne daraus …", "thinking");
  await api("/api/match/end", { method: "POST", body: { ours: $("#mOurs").value, enemy: $("#mEnemy").value } });
  document.querySelectorAll(".thinking").forEach((x) => x.remove());
  addMsg("assistant", "Review fertig. Es liegt im Team-Ordner unter reports/ und im Discord-Report-Kanal."); refresh();
};
$("#planBtn").onclick = async () => {
  const wait = addMsg("assistant", "erstellt Gameplan …", "thinking");
  const r = await api("/api/plan", { method: "POST", body: matchBody() });
  wait.remove(); addMsg("assistant", r.plan || "Plan fehlgeschlagen (API-Key?)");
};
async function loadCallouts() {
  const mp = $("#coMap").value.trim();
  const r = await api("/api/callouts" + (mp ? `?map=${encodeURIComponent(mp)}` : ""));
  if (!mp && r.map) $("#coMap").value = r.map;
  $("#coMaps").innerHTML = r.maps.map((m) => `<option value="${esc(m)}">`).join("");
  $("#coList").innerHTML = r.callouts.map((c) => `<li><span><b>${esc(c.name)}</b>${c.zone ? " · " + esc(c.zone) : ""}${c.aliases.length ? " · " + esc(c.aliases.join(", ")) : ""}
    <span class="hint">${c.source === "learned" ? "(gelernt)" : ""}</span></span>${c.id ? `<span class="x" data-co="${c.id}">✕</span>` : ""}</li>`).join("") || `<li class="hint">Noch keine Callouts für diese Map.</li>`;
  $("#coList").querySelectorAll("[data-co]").forEach((x) => x.onclick = async () => { await api(`/api/callouts/${x.dataset.co}`, { method: "DELETE" }); loadCallouts(); });
}
$("#coLoad").onclick = loadCallouts;
$("#coAdd").onclick = async () => {
  if (!$("#coMap").value.trim() || !$("#coName").value.trim()) { toast("Map und Callout-Name eintragen", false, $("#coAdd")); return; }
  const r = await act($("#coAdd"), api("/api/callouts", { method: "POST", body: { map: $("#coMap").value, name: $("#coName").value, zone: $("#coZone").value, aliases: $("#coAlias").value } }),
    "Callout gespeichert", "Callout nicht gespeichert");
  if (!failed(r)) $("#coName").value = $("#coZone").value = $("#coAlias").value = ""; loadCallouts();
};
$("#brainBadge").onclick = () => document.querySelector('.tabs button[data-tab="settings"]').click();
$("#cMode").onchange = async (e) => {
  const names = { talk: "Konversation", live: "Match / Live-Coaching", observe: "Nur zuschauen", teach: "Training" };
  await act(null, api("/api/mode", { method: "POST", body: { mode: e.target.value } }), `Modus: ${names[e.target.value]}`, "Modus nicht geändert"); refresh();
};
$("#lsStart").onclick = async () => {
  const topic = $("#lsTopic").value.trim(); if (!topic) return;
  $("#lsInfo").textContent = "Yvara AI bereitet das Training vor …";
  const r = await api("/api/lesson", { method: "POST", body: { topic } });
  if (!r || !r.ok) $("#lsInfo").textContent = "Konnte keine Lektion erstellen.";
  refresh();
};
$("#lsNext").onclick = async () => { await api("/api/lesson", { method: "POST", body: { action: "next" } }); refresh(); };
$("#lsRepeat").onclick = () => api("/api/lesson", { method: "POST", body: { action: "repeat" } });
$("#lsStop").onclick = async () => { await api("/api/lesson", { method: "POST", body: { action: "stop" } }); refresh(); };
$("#focusSelect").onchange = async (e) => { await api("/api/focus", { method: "POST", body: { player: e.target.value } }); refresh(); };

// --- Feeds -----------------------------------------------------------------
function feedRow(f = { player: "", kind: "monitor", ref: "" }) {
  const d = document.createElement("div"); d.className = "feed-row";
  const ref = Array.isArray(f.ref) ? f.ref.join(" ") : (f.ref ?? "");
  d.innerHTML = `<input placeholder="Spieler" value="${esc(f.player || "")}">
    <select>${["monitor", "window", "region", "twitch", "stream", "device"].map((k) => `<option ${k === f.kind ? "selected" : ""}>${k}</option>`).join("")}</select>
    <input placeholder="Monitor-Nr / Fenster / Twitch-Kanal / Port (stream)" value="${esc(String(ref))}"><span class="dot ${f.ok ? "ok" : ""}" title="${esc(f.error || "")}"></span><button>✕</button>`;
  d.querySelector("button").onclick = () => d.remove();
  $("#feedRows").appendChild(d);
}
async function loadFeeds() {
  const r = await api("/api/feeds");
  $("#feedRows").innerHTML = ""; r.feeds.forEach(feedRow); $("#autoFocus").checked = !!r.auto_focus;
}
$("#addFeed").onclick = () => feedRow();
$("#saveFeeds").onclick = async () => {
  const feeds = [...document.querySelectorAll(".feed-row")].map((r) => { const [p, , ref] = r.querySelectorAll("input"); return { player: p.value.trim(), kind: r.querySelector("select").value, ref: ref.value.trim() }; });
  await act($("#saveFeeds"), api("/api/feeds", { method: "POST", body: { feeds, auto_focus: $("#autoFocus").checked } }), "Bildquellen übernommen", "Bildquellen nicht übernommen");
  setTimeout(loadFeeds, 3000);
};
loadFeeds();

// --- Tools -----------------------------------------------------------------
$("#clipBtn").onclick = () => act($("#clipBtn"), api("/api/clip", { method: "POST", body: { note: "aus der App" } }), "Moment geclippt", "Clip fehlgeschlagen");
$("#timeoutBtn").onclick = async () => { await api("/api/timeout", { method: "POST", body: {} }); };
$("#trainingBtn").onclick = async () => {
  const w = addMsg("assistant", "erstellt Trainingsplan …", "thinking");
  const r = await api("/api/training", { method: "POST", body: {} }); w.remove(); addMsg("assistant", r.plan || "Fehlgeschlagen.");
};
$("#scoutBtn").onclick = async () => {
  const opp = $("#mOpp").value.trim();
  if (!opp) { toast("Erst bei „Gegner“ das Team eintragen", false, $("#mOpp")); return; }
  const w = addMsg("assistant", `scoutet ${opp} (mit Online-Recherche) …`, "thinking");
  const r = await api("/api/scout", { method: "POST", body: { opponent: opp } }); w.remove(); addMsg("assistant", r.report || "Fehlgeschlagen.");
};

// --- Lernen ----------------------------------------------------------------
const PB_TITLES = { principles: "Prinzipien", setups: "Setups", rotations: "Rotationen", mistakes_to_avoid: "Fehler vermeiden", meta_notes: "Meta", drills: "Drills" };
function fmtDur(sec) {
  sec = Math.round(sec || 0);
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return h ? `${h} h ${m} min` : m ? `${m} min` : `${s} s`;
}
const SRC_ICON = { url: "🎬", video: "📁", screen: "🖥️" };
const openHist = new Set();
function renderLearning(lib) {
  const t = lib.totals || {};
  const modes = Object.entries(t.modes || {}).slice(0, 4).map(([m, n]) => `${esc(m)} ${n}`).join(" · ");
  $("#lpTotals").innerHTML = [
    ["Geschaut", fmtDur(t.watched_s)], ["Quellen", `${t.done || 0} / ${t.sources || 0}`],
    ["Lektionen", t.lessons || 0], ["Eigene Taktiken", t.ideas || 0], ["Playbooks", t.playbooks || 0],
  ].map(([k, v]) => `<div><b>${v}</b><span>${k}</span></div>`).join("") + (modes ? `<p class="hint">Nach Modus: ${modes}</p>` : "");
  const st = lib.status || {};
  const now = $("#lpNow");
  now.classList.toggle("hidden", !st.running);
  if (st.running) {
    const pct = Math.round((st.progress || 0) * 100);
    const dur = st.duration ? ` von ${fmtDur(st.duration)}` : "";
    now.querySelector(".nowtxt").innerHTML = `▶ <b>${esc(st.title || "")}</b><br>${esc(st.message || "")} · ${fmtDur(st.watched_s)} geschaut${dur} · ${st.lessons || 0} Lektionen`;
    now.querySelector(".bar i").style.width = (st.kind === "screen" ? 100 : pct) + "%";
  }
  const ul = $("#lpHistory");
  ul.innerHTML = "";
  for (const h of lib.history || []) {
    const li = document.createElement("li");
    const status = h.status === "done" ? "✅ fertig" : h.status === "running" ? `▶ läuft ${Math.round((h.progress || 0) * 100)}%`
      : String(h.status || "").startsWith("error") ? "⚠️ Fehler" : esc(h.status || "");
    const when = new Date(h.ts * 1000).toLocaleString("de-DE", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
    const watched = h.duration ? `${fmtDur(h.watched_s)} von ${fmtDur(h.duration)}` : fmtDur(h.watched_s);
    li.innerHTML = `<div class="hh"><span>${SRC_ICON[h.kind] || "📺"} <b>${esc(h.title || h.ref || "")}</b></span><span class="muted">${when}</span></div>
      <div class="muted small">${status} · ${watched} geschaut · ${h.lessons || 0} Lektionen</div>
      <div class="bar"><i style="width:${Math.round((h.progress || 0) * 100)}%"></i></div>
      <ul class="top ${openHist.has(h.id) ? "" : "hidden"}">${(h.top || []).map((l) => `<li><b>${esc(l.mode || "?")}${l.map ? " · " + esc(l.map) : ""}</b> ${esc(l.lesson)}</li>`).join("") || "<li>Noch keine Lektionen.</li>"}</ul>`;
    li.onclick = () => { openHist.has(h.id) ? openHist.delete(h.id) : openHist.add(h.id); li.querySelector(".top").classList.toggle("hidden"); };
    ul.appendChild(li);
  }
  if (!ul.children.length) ul.innerHTML = '<li class="muted">Noch nichts geschaut – starte unten eine Fortbildung oder Lern-Session.</li>';
}

async function loadLibrary() {
  const lib = await api("/api/library");
  const st = lib.status || {};
  $("#stStatus").textContent = st.running ? `📚 ${st.title}: ${st.message} · ${Math.round((st.progress || 0) * 100)}% · ${st.lessons || 0} Lektionen`
    : (st.message ? `Letzte Session: ${st.message} (${st.lessons || 0} Lektionen)` : "");
  $("#lessonCount").textContent = lib.count || 0;
  renderLearning(lib);
  const al = lib.autolearn || {};
  $("#alStatus").textContent = al.running ? `🎓 läuft · ${al.message || ""} · ${al.videos || 0} Videos · ${al.ideas || 0} eigene Taktiken`
    : (al.message ? `Fortbildung: ${al.message} (${al.videos || 0} Videos)` : "");
  $("#alQueue").innerHTML = (lib.queue || []).slice(0, 15).map((q) => `<li><span>${q.status === "queued" ? "⏳" : q.status === "running" ? "▶" : q.status === "done" ? "✅" : "⚠️"} ${esc(q.title || q.url)}</span></li>`).join("");
  $("#alIdeas").innerHTML = (lib.ideas || []).map((i) => md(`**${i.name}** (${i.mode}, ${i.map})\n${i.data.concept || ""}\n- Setup: ${i.data.setup || ""}\n- Warum es zu uns passt: ${i.data.why_it_fits_us || ""}`)).join("") || "Noch keine.";
  $("#playbooks").innerHTML = Object.entries(lib.playbooks || {}).map(([mode, pb]) =>
    md(`### ${mode}\n${pb.summary || ""}\n` + Object.entries(PB_TITLES).map(([k, t]) => (pb[k] || []).slice(0, 4).map((x) => `- **${t}:** ${x}`).join("\n")).join("\n"))).join("") || "Noch keine – starte eine Lern-Session.";
  $("#lessons").innerHTML = "";
  for (const l of (lib.lessons || []).slice(0, 40)) {
    const li = document.createElement("li");
    li.innerHTML = `<span><b>${esc(l.mode || "?")}</b> ${esc(l.lesson)}</span><b class="x">✕</b>`;
    li.querySelector(".x").onclick = async () => { await api(`/api/library/${l.id}`, { method: "DELETE" }); loadLibrary(); };
    $("#lessons").appendChild(li);
  }
}
$("#stStart").onclick = async () => {
  const r = await api("/api/study", { method: "POST", body: { source: $("#stSource").value.trim() || "screen", title: $("#stTitle").value,
    mode: $("#stMode").value, own_team: $("#stOwn").checked } });
  if (failed(r) || r.error) toast("Lern-Session nicht gestartet: " + (r.error || ""), false, $("#stStart")); else toast("Lern-Session gestartet", true, $("#stStart"));
  loadLibrary();
};
$("#alStart").onclick = async () => {
  const r = await act($("#alStart"), api("/api/autolearn", { method: "POST", body: { topics: $("#alTopics").value, links: $("#alLinks").value.split(/\s+/).filter(Boolean) } }),
    "Fortbildung gestartet", "Fortbildung nicht gestartet");
  if (!failed(r)) $("#alLinks").value = ""; loadLibrary();
};
$("#alStop").onclick = async () => { await act($("#alStop"), api("/api/autolearn/stop", { method: "POST", body: {} }), "Fortbildung wird gestoppt", "Stoppen fehlgeschlagen"); loadLibrary(); };
$("#alGameSave").onclick = () => act($("#alGameSave"), api("/api/settings", { method: "POST", body: { "autolearn.game": $("#alGame").value || "Modern Warfare 4" } }), "Spiel übernommen", "Speichern fehlgeschlagen");
$("#alAuto").onchange = () => api("/api/settings", { method: "POST", body: { "autolearn.autostart": $("#alAuto").checked } });
$("#stStop").onclick = async () => { await api("/api/study/stop", { method: "POST", body: {} }); loadLibrary(); };
setInterval(() => { if (!$("#tab-study").classList.contains("hidden")) loadLibrary(); }, 4000);

// --- Einstellungen -------------------------------------------------------
$("#saveSettings").onclick = async () => {
  await act($("#saveSettings"), api("/api/settings", { method: "POST", body: {
    "coach.name": $("#sName").value, "coach.personality": $("#sPers").value, "coach.vibe": $("#sVibe").value, "coach.roast_level": Number($("#sRoast").value), "language": $("#sLang").value,
    "tts.engine": $("#sEngine").value, "audio.stt_engine": $("#sStt").value, "tts.supertonic_voice": $("#sSupVoice").value, "tts.piper_de": $("#sPiperDe").value, "tts.piper_en": $("#sPiperEn").value,
    "tts.piper_speed": $("#sSpeed").value || 1, "tts.xtts_voice": $("#sXttsVoice").value, "tts.xtts_voice_en": $("#sXttsVoiceEn").value, "tts.same_voice": $("#sSameVoice").checked, "capture.vision_interval_s": $("#sVision").value || 20,
    "audio.push_to_talk_key": $("#sPtt").value, "callouts.enabled": $("#sCallouts").checked, "web.research": $("#sWeb").checked,
    "llm.provider": $("#sProvider").value, "llm.local_model": $("#sLocalModel").value, "llm.base_url": $("#sBaseUrl").value,
    "llm.local_vision": $("#sLocalVision").checked, "performance.gaming_mode": $("#sGaming").value,
    "performance.game_model": $("#sGameModel").value, "discord.follow_user": $("#sFollow").value } }), "Einstellungen gespeichert", "Speichern fehlgeschlagen");
  await refresh(true);
};
// --- Stimmen -------------------------------------------------------------
let voiceWanted = "", voiceWantedEn = null;
function engineBoxes() {
  const e = $("#sEngine").value;
  $("#xttsBox").classList.toggle("hidden", e !== "xtts");
  $("#piperBox").classList.toggle("hidden", e !== "piper");
  $("#supBox").classList.toggle("hidden", e !== "supertonic");
}
$("#sEngine").onchange = engineBoxes;
$("#sXttsVoice").onchange = () => { voiceWanted = $("#sXttsVoice").value; };
$("#sXttsVoiceEn").onchange = () => { voiceWantedEn = $("#sXttsVoiceEn").value; };
$("#sSpeed").oninput = () => { $("#speedVal").textContent = Number($("#sSpeed").value).toFixed(2); };
async function loadVoices() {
  const v = await api("/api/voices");
  const cur = voiceWanted || $("#sXttsVoice").value;
  $("#sXttsVoice").innerHTML = [...(v.custom || []), ...(v.xtts_speakers || [])].map((o) => `<option value="${esc(o.id)}">${esc(o.label)}</option>`).join("");
  if (cur) $("#sXttsVoice").value = cur;
  const curEn = voiceWantedEn !== null ? voiceWantedEn : $("#sXttsVoiceEn").value;
  $("#sXttsVoiceEn").innerHTML = '<option value="">— gleiche Stimme wie Deutsch —</option>' + $("#sXttsVoice").innerHTML;
  $("#sXttsVoiceEn").value = curEn || "";
  $("#piperList").innerHTML = (v.piper || []).map((o) => `<option value="${esc(o.id)}">${esc(o.label)}</option>`).join("");
  const st = v.xtts_status || {};
  const bad = !v.xtts_ready || v.xtts_error;
  $("#xttsInfo").className = bad ? "warnbox" : "hint okbox";
  $("#xttsInfo").innerHTML = !bad ? "✅ XTTS ist eingerichtet – eigene Stimmen werden benutzt."
    : `⚠️ <b>Eigene Stimmen gehen gerade NICHT</b> – Yvara AI spricht deshalb mit der Standardstimme (Piper).<br>Grund: ${esc(v.xtts_error || st.problem || "unbekannt")}`;
  $("#cvList").innerHTML = "";
  if ($("#cvSlots")) $("#cvSlots").textContent = `Stimm-Plätze: ${(v.custom || []).length} von ${v.slots || 10} belegt`;
  for (const c of v.custom || []) {
    const li = document.createElement("li");
    li.innerHTML = `<span>🎙️ ${esc(c.id.slice(7))}${c.silent ? ' <b style="color:#ff4655">⚠️ Aufnahme ist stumm – bitte neu aufnehmen oder Datei hochladen</b>' : ""}</span><span><a class="use" data-l="de">für DE</a> · <a class="use" data-l="en">für EN</a> <a class="sty" title="Sprechstil">✎ Stil</a> <b class="x">✕</b></span>`
      + (c.style ? `<div class="hint">Stil: ${esc(c.style)}</div>` : "");
    li.querySelector(".sty").onclick = async () => {
      const st = prompt(`Wie redet ${c.id.slice(7)}? (z. B. „locker, sagt ab und zu Bro, viel Slang“) – leer = kein eigener Stil`, c.style || "");
      if (st === null) return;
      await api("/api/voices/style", { method: "POST", body: { name: c.id.slice(7), style: st } });
      toast("Sprechstil gespeichert"); loadVoices();
    };
    li.querySelectorAll(".use").forEach((a) => a.onclick = async () => {
      const body = a.dataset.l === "de" ? { "tts.xtts_voice": c.id } : { "tts.xtts_voice_en": c.id };
      body["tts.engine"] = "xtts";
      await api("/api/settings", { method: "POST", body }); toast(`${c.id.slice(7)} ist jetzt die ${a.dataset.l === "de" ? "deutsche" : "englische"} Stimme`);
      await refresh(true);
    });
    li.querySelector(".x").onclick = async () => { await api(`/api/voices/custom/${encodeURIComponent(c.id.slice(7))}`, { method: "DELETE" }); loadVoices(); };
    $("#cvList").appendChild(li);
  }
}
async function preview(lang, btn) {
  const old = btn.textContent; btn.textContent = "… erzeugt"; btn.disabled = true;
  try {
    const r = await api("/api/voices/preview", { method: "POST", body: {
      lang, engine: $("#sEngine").value, supertonic_voice: $("#sSupVoice").value, xtts_voice: $("#sXttsVoice").value, xtts_voice_en: $("#sXttsVoiceEn").value, piper_de: $("#sPiperDe").value,
      piper_en: $("#sPiperEn").value, piper_speed: $("#sSpeed").value, same_voice: $("#sSameVoice").checked } });
    if (r.error) toast("Vorhören fehlgeschlagen: " + r.error, false);
    else { if (r.wanted === "xtts" && r.engine !== "xtts") toast("XTTS nicht bereit – du hörst die Standardstimme. " + (r.engine_error || ""), false); play(r.audio); }
  } finally { btn.textContent = old; btn.disabled = false; }
}
$("#voiceTest").onclick = async () => {
  const b = $("#voiceTest"), out = $("#voiceTestOut"), old = b.textContent;
  b.textContent = "… testet (beim ersten Mal bis 1 Minute)"; b.disabled = true;
  out.classList.remove("hidden"); out.textContent = "";
  try {
    const r = await api("/api/voices/test", { method: "POST", body: {} });
    const names = { xtts: "XTTS (eigene Stimmen)", piper: "Piper (Standardstimme)", edge: "Edge", system: "System-Stimme" };
    const rows = Object.entries(r.langs || {}).map(([lang, x]) => {
      const ok = r.engine_setting !== "xtts" || x.engine === "xtts";
      const errs = Object.entries(x.errors || {}).map(([k, e]) => `${esc(k)}: ${esc(e)}`).join(" · ");
      return `${ok ? "✅" : "❌"} <b>${lang.toUpperCase()}</b>: spricht mit ${esc(names[x.engine] || x.engine || "nichts")}`
        + (x.engine === "xtts" ? ` – Stimme ${esc(String(x.voice).replace("custom:", "eigene: "))}` : "")
        + (x.voice_file === false ? " – ⚠️ Stimmdatei fehlt" : "") + (errs ? `<br><small>${errs}</small>` : "")
        + (x.audio ? ` <a href="#" data-a="${esc(x.audio)}">▶ anhören</a>` : "");
    });
    out.innerHTML = `Eingestellt: ${esc(names[r.engine_setting] || r.engine_setting)}<br>` + rows.join("<br>")
      + (r.xtts && r.xtts.problem ? `<div class="warnbox">${esc(r.xtts.problem)}</div>` : "");
    out.querySelectorAll("a[data-a]").forEach((a) => a.onclick = (e) => { e.preventDefault(); play(a.dataset.a); });
    loadVoices();
  } catch (e) { out.textContent = "Test fehlgeschlagen: " + e.message; }
  finally { b.textContent = old; b.disabled = false; }
};
$("#prevDe").onclick = () => preview("de", $("#prevDe"));
$("#prevEn").onclick = () => preview("en", $("#prevEn"));
let cvRec = null, cvChunks = [], cvBlob = null, cvTimer = null;
$("#cvRec").onclick = async () => {
  if (cvRec && cvRec.state === "recording") { cvRec.stop(); return; }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: false, noiseSuppression: true } });
    cvRec = new MediaRecorder(stream); cvChunks = [];
    // Pegel mitmessen: ein stummes Mikrofon ergibt sonst eine „gespeicherte“ Stimme ohne Ton
    let peak = 0, ac = null;
    try {
      ac = new AudioContext(); const an = ac.createAnalyser(); ac.createMediaStreamSource(stream).connect(an);
      const buf = new Float32Array(an.fftSize);
      cvRec._lvl = setInterval(() => { an.getFloatTimeDomainData(buf); for (const v of buf) peak = Math.max(peak, Math.abs(v)); }, 100);
    } catch (e) { peak = 1; }
    cvRec.ondataavailable = (e) => cvChunks.push(e.data);
    cvRec.onstop = () => {
      stream.getTracks().forEach((t) => t.stop()); clearInterval(cvTimer); clearInterval(cvRec._lvl); if (ac) ac.close();
      $("#cvRec").textContent = "⏺ Neu aufnehmen";
      if (peak < 0.01) {
        cvBlob = null;
        $("#cvInfo").textContent = "⚠️ Das Mikrofon hat nur Stille geliefert – falsches Mikrofon oder stummgeschaltet? In Windows das richtige Standard-Mikrofon wählen oder eine Sprachdatei hochladen.";
        toast("Aufnahme war stumm – bitte Mikrofon prüfen", false); return;
      }
      cvBlob = new Blob(cvChunks, { type: cvRec.mimeType });
      $("#cvInfo").textContent = "Aufnahme fertig – jetzt „Stimme speichern“.";
    };
    cvRec.start(); const t0 = Date.now();
    $("#cvRec").textContent = "■ Aufnahme stoppen";
    cvTimer = setInterval(() => { const s = Math.round((Date.now() - t0) / 1000); $("#cvTime").textContent = `${s} s ${s < 15 ? "(weiter, mind. 15 s)" : "✓"}`; if (s >= 30) cvRec.stop(); }, 500);
  } catch (e) { toast("Mikrofon nicht verfügbar: " + e.message, false); }
};
$("#cvSave").onclick = async () => {
  const files = $("#cvFile").files.length ? [...$("#cvFile").files] : (cvBlob ? [cvBlob] : []);
  if (!files.length) { toast("Erst aufnehmen oder eine Datei wählen.", false); return; }
  if (!$("#cvConsent").checked) { toast("Bitte bestätigen, dass es deine Stimme ist oder die Person zugestimmt hat.", false); return; }
  const fd = new FormData();
  files.forEach((f, i) => fd.append("audio", f, f.name || `stimme${i}.webm`)); fd.append("name", $("#cvName").value || "meine_stimme");
  fd.append("consent", "true"); fd.append("use", $("#cvUse").value);
  $("#cvInfo").textContent = "Wird gespeichert …";
  const r = await fetch("/api/voices/custom", { method: "POST", body: fd }).then((r) => r.json()).catch((e) => ({ error: e.message }));
  if (r.error) { toast(r.error, false); $("#cvInfo").textContent = r.error; return; }
  const forWhat = { both: "Deutsch und Englisch", de: "Deutsch", en: "Englisch" }[$("#cvUse").value];
  if (r.xtts_ready === false) {
    $("#cvInfo").textContent = `Gespeichert (${r.seconds} s) für ${forWhat} – aber XTTS ist nicht bereit, deshalb hörst du noch die Standardstimme: ${r.xtts_problem || ""}`;
    toast("Stimme gespeichert, XTTS aber nicht bereit – siehe roten Hinweis", false);
  } else {
    $("#cvInfo").textContent = `✅ Gespeichert (${r.seconds} s) und für ${forWhat} eingestellt. Zum Prüfen „Stimme testen“ drücken.`;
    toast("Stimme gespeichert und aktiv");
  }
  await refresh(true);
  cvBlob = null; $("#cvFile").value = "";
};

// Update-Suche mit sichtbarer Rückmeldung: Spinner → „Neue Version“ / „Neueste Version“ / Fehler
function setUpdState(state, title, info) {
  const box = $("#updStatus");
  box.dataset.state = state;
  $("#updTitle").textContent = title;
  $("#updInfo").textContent = info || "";
  box.classList.remove("upd-anim"); void box.offsetWidth; box.classList.add("upd-anim");
}
let updBusy = false;
async function checkUpdate(manual = false) {
  if (updBusy) return;
  updBusy = true;
  const btn = $("#updCheck");
  btn.disabled = true; btn.textContent = "Suche läuft …";
  $("#updApply").classList.add("hidden");
  setUpdState("checking", "Suche nach Updates …", "Frage GitHub nach der neuesten Version.");
  // Spinner kurz stehen lassen, damit man sieht, dass wirklich gesucht wurde
  const [r] = await Promise.all([api("/api/update"), new Promise((res) => setTimeout(res, 900))]);
  btn.disabled = false; btn.textContent = "Nach Updates suchen";
  updBusy = false;
  const short = (sha) => (sha ? String(sha).slice(0, 7) : "unbekannt");
  const cur = short(r.installed && r.installed.sha);
  const time = new Date().toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" });
  if (r.error || (!r.available && !r.latest)) {
    const why = r.error === true ? r.message : (r.error || r.message || "Unbekannter Fehler");
    setUpdState("error", "Update-Suche fehlgeschlagen", `${why} · Installiert: ${cur}`);
    if (manual) toast("Update-Suche fehlgeschlagen", false);
  } else if (r.available) {
    setUpdState("new", `Update gefunden: ${short(r.latest)}`,
      `${r.message ? "„" + r.message + "“ · " : ""}Installiert: ${cur} · geprüft ${time}`);
    $("#updApply").classList.remove("hidden");
    if (manual) toast("Neues Update gefunden!");
  } else {
    setUpdState("ok", "Du hast die neueste Version", `Version ${cur} · geprüft ${time}`);
    if (manual) toast("Du hast die neueste Version");
  }
}
$("#updCheck").onclick = () => checkUpdate(true);
$("#updApply").onclick = async () => {
  const btn = $("#updApply");
  btn.disabled = true;
  setUpdState("checking", "Update wird installiert …", "Bitte nicht schließen – Yvara AI startet danach neu.");
  const r = await api("/api/update", { method: "POST", body: {} });
  btn.disabled = false;
  if (r.error) {
    setUpdState("error", "Update fehlgeschlagen", String(r.error));
    toast("Update fehlgeschlagen", false);
  } else {
    btn.classList.add("hidden");
    setUpdState("ok", "Update installiert", "Yvara AI startet neu – die Seite lädt in ein paar Sekunden neu.");
    toast("Update installiert – Neustart …");
    setTimeout(() => location.reload(), 15000);
  }
};
document.querySelectorAll(".tabs button").forEach((b) => b.onclick = () => {
  document.querySelectorAll(".tabs button").forEach((x) => x.classList.toggle("active", x === b));
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("hidden", t.id !== "tab-" + b.dataset.tab));
  if (b.dataset.tab === "memory") loadMemories();
  if (b.dataset.tab === "study") loadLibrary();
  if (b.dataset.tab === "tactic") loadTactic();
  if (b.dataset.tab === "settings") checkUpdate(false);
  if (b.dataset.tab === "team") loadCallouts();
  if (b.dataset.tab === "console") loadLogs();
  if (b.dataset.tab === "home") loadHomeVoices();
});
function alertMsg(t) { toast(t, false); }

// --- Status-Polling -------------------------------------------------------
async function refresh(full = false) {
  state = await api("/api/state");
  document.title = `${state.name} Coach`;
  $("#coachName").textContent = state.name;
  $("#webToggleWrap").classList.toggle("hidden", !state.web_allowed); $("#sWebWrap").classList.toggle("hidden", !state.web_allowed);
  if (!state.web_allowed) $("#webToggle").checked = false;
  if (state.version) $("#appVersion").textContent = "v" + state.version + (String(state.version).startsWith("0.") ? " Alpha" : "");
  const g = state.gaming;
  if (g && $("#gamingInfo")) $("#gamingInfo").textContent = g.active ? `🎮 Gaming-Modus aktiv (${g.game}) – KI: ${g.model}, Bildanalyse und Lernen pausiert.` : "Gaming-Modus gerade nicht aktiv.";
  $("#appVersion").title = g && g.active ? "Gaming-Modus aktiv" : "";
  document.querySelectorAll(".nm").forEach((n) => n.textContent = state.name);
  $("#input").placeholder = `Nachricht an ${state.name} …`;
  const m = state.match;
  $("#matchBadge").textContent = m.active ? `LIVE · ${m.summary}` : "Kein Match";
  $("#matchBadge").classList.toggle("live", m.active);
  const br = state.brain || { ok: true };
  $("#brainBadge").classList.toggle("hidden", !!br.ok);
  $("#brainBadge").textContent = br.ok ? "" : `🧠 Kein Gehirn: ${br.message}`;
  $("#discordBadge").classList.toggle("hidden", !state.discord_error);
  $("#discordBadge").textContent = state.discord_error ? `🎧 ${state.discord_error}` : "";
  $("#discordBadge").title = state.discord_error || "";
  if (document.activeElement !== $("#cMode")) $("#cMode").value = state.coaching_mode || "talk";
  $("#teachBox").classList.toggle("hidden", state.coaching_mode !== "teach");
  const ls = state.lesson;
  if (ls) $("#lsInfo").innerHTML = `<b>${esc(ls.title || "")}</b> · Schritt ${Math.min(ls.idx + 1, ls.steps.length)}/${ls.steps.length}<br>${esc(ls.steps[ls.idx] || "")}`;
  else if ($("#lsInfo").textContent.startsWith("Yvara AI bereitet") === false && state.coaching_mode === "teach") $("#lsInfo").textContent = "Thema eingeben oder im Voice sagen: „Yvara AI, zeig uns …“";
  $("#focusBadge").classList.toggle("hidden", !state.focus);
  $("#focusBadge").textContent = state.focus ? `👁 Fokus: ${state.focus}` : "";
  $("#feed").innerHTML = state.feed.slice().reverse().map((f) => `<li><span>${esc(f.text)}</span><small>${new Date(f.ts * 1000).toLocaleTimeString()}</small></li>`).join("");
  $("#stats").innerHTML = md(state.stats);
  if (full) {
    $("#teamSelect").innerHTML = state.teams.map((t) => `<option value="${t.slug}" ${t.slug === state.team.slug ? "selected" : ""}>${esc(t.name)}</option>`).join("");
    $("#tName").value = state.team.name; $("#tNotes").value = state.team.notes || "";
    $("#players").innerHTML = ""; (state.team.players || []).forEach(playerRow);
    $("#focusSelect").innerHTML = `<option value="">— ganzes Team —</option>` + state.team.players.map((p) => `<option ${p.ingame === state.focus ? "selected" : ""}>${esc(p.ingame)}</option>`).join("");
    $("#profSelect").innerHTML = `<option value="">— wählen —</option><option value="team:${esc(state.team.name)}">Team: ${esc(state.team.name)}</option>` +
      state.team.players.map((p) => `<option value="player:${esc(p.ingame)}">${esc(p.ingame)}</option>`).join("");
    const s = state.settings;
    $("#sName").value = s["coach.name"] || ""; $("#sPers").value = s["coach.personality"] || ""; $("#sVibe").value = s["coach.vibe"] || "normal"; $("#sRoast").value = String(s["coach.roast_level"] ?? 1);
    $("#alGame").value = s["autolearn.game"] || "Modern Warfare 4"; $("#alAuto").checked = s["autolearn.autostart"] !== false; $("#sLang").value = s.language || "auto";
    $("#sEngine").value = s["tts.engine"] || "piper"; $("#sSupVoice").value = s["tts.supertonic_voice"] || "M1"; $("#sStt").value = s["audio.stt_engine"] || "parakeet"; $("#sPiperDe").value = s["tts.piper_de"] || ""; $("#sPiperEn").value = s["tts.piper_en"] || "";
    $("#sSpeed").value = s["tts.piper_speed"] || 1; $("#speedVal").textContent = Number($("#sSpeed").value).toFixed(2);
    $("#sSameVoice").checked = !!s["tts.same_voice"]; voiceWanted = s["tts.xtts_voice"] || ""; voiceWantedEn = s["tts.xtts_voice_en"] || ""; loadVoices(); engineBoxes(); $("#sVision").value = s["capture.vision_interval_s"] || 20; $("#sPtt").value = s["audio.push_to_talk_key"] || "";
    $("#sCallouts").checked = !!s["callouts.enabled"]; $("#sWeb").checked = !!s["web.research"]; $("#webToggle").checked = !!s["web.research"];
    $("#sProvider").value = s["llm.provider"] || "ollama"; $("#sLocalModel").value = s["llm.local_model"] || "";
    $("#sBaseUrl").value = s["llm.base_url"] || ""; $("#sLocalVision").checked = s["llm.local_vision"] !== false;
    $("#sGaming").value = s["performance.gaming_mode"] || "auto"; $("#sGameModel").value = s["performance.game_model"] || ""; $("#sFollow").value = s["discord.follow_user"] || "";
    loadSessions();
  }
}
refresh(true);
setInterval(refresh, 3000);

// --- Taktik: Heatmaps & Strat-Board ----------------------------------------
async function loadMapsInto(sel, keep) {
  const maps = await api("/api/maps");
  const cur = keep || sel.value || (state.match && state.match.map) || "";
  const list = Array.isArray(maps) ? maps : [];
  if (cur && !list.includes(cur)) list.unshift(cur);
  sel.innerHTML = list.length ? list.map((m) => `<option>${esc(m)}</option>`).join("") : '<option value="">— keine Map —</option>';
  if (cur) sel.value = cur;
}
async function loadHeatmap() {
  const map = $("#hmMap").value;
  const img = $("#hmImg");
  if (!map) { img.classList.add("hidden"); $("#hmInfo").textContent = "Noch keine Maps – starte ein Match mit Map-Namen."; return; }
  const hm = (await api("/api/heatmaps")) || [];
  const info = (Array.isArray(hm) ? hm : []).find((h) => h.map.toLowerCase() === map.toLowerCase());
  img.onerror = () => { img.classList.add("hidden"); $("#hmInfo").textContent = "Für diese Map gibt es noch keine Heatmap (Minimap muss eingerichtet sein)."; };
  img.onload = () => { img.classList.remove("hidden"); $("#hmInfo").textContent = info ? `${info.matches} Match(es) gesammelt` : "Aus dem laufenden Match"; };
  img.src = `/api/heatmap.png?map=${encodeURIComponent(map)}&layer=${$("#hmLayer").value}&t=${Date.now()}`;
}
$("#hmMap").onchange = loadHeatmap; $("#hmLayer").onchange = loadHeatmap;
$("#hmPost").onclick = () => act($("#hmPost"), api("/api/heatmap/discord", { method: "POST", body: { map: $("#hmMap").value, layer: $("#hmLayer").value } }),
  "In Discord gepostet", "Posten fehlgeschlagen");

async function loadStrats() {
  const list = await api("/api/strats");
  const ul = $("#sbList");
  ul.innerHTML = "";
  for (const s of Array.isArray(list) ? list : []) {
    const li = document.createElement("li");
    li.innerHTML = `<img src="/api/strats/${s.id}.png?t=${Math.round(s.ts)}" alt="">
      <div><b>${esc(s.name)}</b><div class="hint">${esc(s.map || "")} ${s.mode ? "· " + esc(s.mode) : ""}</div>
      <div class="acts"><a data-a="edit">Bearbeiten</a><a data-a="post">In Discord</a><a data-a="del">Löschen</a></div></div>`;
    li.querySelector('[data-a="edit"]').onclick = () => openBoard(s.id);
    li.querySelector('[data-a="post"]').onclick = async () => {
      const r = await api(`/api/strats/${s.id}/discord`, { method: "POST", body: {} });
      toast(r.error ? "Posten fehlgeschlagen: " + r.error : "In Discord gepostet", !r.error);
    };
    li.querySelector('[data-a="del"]').onclick = async () => { await api(`/api/strats/${s.id}`, { method: "DELETE" }); loadStrats(); };
    ul.appendChild(li);
  }
  if (!ul.children.length) ul.innerHTML = '<li class="hint" style="display:block">Noch keine Strats.</li>';
}

const SB = { id: null, shapes: [], tool: "pen", color: "#ff4655", bg: null, drawing: null };
const sbCv = $("#sbCanvas"), sbCtx = sbCv.getContext("2d");
document.querySelectorAll(".sb-colors i").forEach((i) => {
  i.style.background = i.dataset.c;
  i.onclick = () => { document.querySelectorAll(".sb-colors i").forEach((x) => x.classList.toggle("on", x === i)); SB.color = i.dataset.c; };
});
document.querySelectorAll(".sb-tools [data-tool]").forEach((b) => b.onclick = () => {
  document.querySelectorAll(".sb-tools [data-tool]").forEach((x) => x.classList.toggle("active", x === b)); SB.tool = b.dataset.tool;
});
function sbLoadBg() {
  const map = $("#sbMap").value;
  const img = new Image();
  img.onload = () => { SB.bg = img; sbDraw(); };
  img.onerror = () => { SB.bg = null; sbDraw(); };
  img.src = `/api/strats/bg.png?map=${encodeURIComponent(map)}&t=${Date.now()}`;
}
function sbArrow(a, b, c) {
  const W = 600, [x1, y1, x2, y2] = [a[0] * W, a[1] * W, b[0] * W, b[1] * W], ang = Math.atan2(y2 - y1, x2 - x1);
  sbCtx.strokeStyle = sbCtx.fillStyle = c; sbCtx.lineWidth = 4;
  sbCtx.beginPath(); sbCtx.moveTo(x1, y1); sbCtx.lineTo(x2, y2); sbCtx.stroke();
  sbCtx.beginPath(); sbCtx.moveTo(x2, y2);
  sbCtx.lineTo(x2 - 16 * Math.cos(ang - 0.45), y2 - 16 * Math.sin(ang - 0.45));
  sbCtx.lineTo(x2 - 16 * Math.cos(ang + 0.45), y2 - 16 * Math.sin(ang + 0.45)); sbCtx.closePath(); sbCtx.fill();
}
function sbShape(s) {
  const W = 600;
  sbCtx.lineCap = sbCtx.lineJoin = "round";
  if (s.t === "pen") {
    sbCtx.strokeStyle = s.c; sbCtx.lineWidth = 4; sbCtx.beginPath();
    s.pts.forEach((p, i) => (i ? sbCtx.lineTo(p[0] * W, p[1] * W) : sbCtx.moveTo(p[0] * W, p[1] * W))); sbCtx.stroke();
  } else if (s.t === "arrow") sbArrow(s.a, s.b, s.c);
  else if (s.t === "circle") {
    sbCtx.strokeStyle = s.c; sbCtx.lineWidth = 3; sbCtx.fillStyle = s.c + "33";
    sbCtx.beginPath(); sbCtx.arc(s.a[0] * W, s.a[1] * W, Math.max(6, s.r * W), 0, 7); sbCtx.fill(); sbCtx.stroke();
  } else if (s.t === "x") {
    const [x, y] = [s.a[0] * W, s.a[1] * W]; sbCtx.strokeStyle = s.c; sbCtx.lineWidth = 5;
    sbCtx.beginPath(); sbCtx.moveTo(x - 10, y - 10); sbCtx.lineTo(x + 10, y + 10); sbCtx.moveTo(x + 10, y - 10); sbCtx.lineTo(x - 10, y + 10); sbCtx.stroke();
  } else if (s.t === "player" || s.t === "text") {
    const [x, y] = [s.a[0] * W, s.a[1] * W];
    sbCtx.font = "bold 16px system-ui, sans-serif"; sbCtx.textBaseline = "middle";
    if (s.t === "player") {
      sbCtx.fillStyle = s.c; sbCtx.beginPath(); sbCtx.arc(x, y, 11, 0, 7); sbCtx.fill();
      sbCtx.fillStyle = "#000"; sbCtx.textAlign = "center"; sbCtx.fillText((s.s || "?").slice(0, 1).toUpperCase(), x, y + 1);
      sbCtx.textAlign = "left"; sbCtx.fillStyle = "#fff"; sbCtx.strokeStyle = "#000"; sbCtx.lineWidth = 3;
      sbCtx.strokeText(s.s, x + 15, y); sbCtx.fillText(s.s, x + 15, y);
    } else {
      sbCtx.textAlign = "left"; sbCtx.strokeStyle = "#000"; sbCtx.lineWidth = 4; sbCtx.fillStyle = s.c;
      sbCtx.strokeText(s.s, x, y); sbCtx.fillText(s.s, x, y);
    }
  }
}
function sbDraw() {
  sbCtx.clearRect(0, 0, 600, 600);
  if (SB.bg) sbCtx.drawImage(SB.bg, 0, 0, 600, 600);
  else {
    sbCtx.fillStyle = "#1c1c1c"; sbCtx.fillRect(0, 0, 600, 600); sbCtx.strokeStyle = "#333"; sbCtx.lineWidth = 1;
    sbCtx.font = "12px system-ui"; sbCtx.fillStyle = "#555";
    for (let i = 0; i < 5; i++) for (let j = 0; j < 5; j++) sbCtx.fillText("ABCDE"[i] + (j + 1), i * 120 + 6, j * 120 + 16);
    for (let i = 1; i < 5; i++) { sbCtx.beginPath(); sbCtx.moveTo(i * 120, 0); sbCtx.lineTo(i * 120, 600); sbCtx.moveTo(0, i * 120); sbCtx.lineTo(600, i * 120); sbCtx.stroke(); }
  }
  SB.shapes.forEach(sbShape);
  if (SB.drawing) sbShape(SB.drawing);
}
function sbPos(e) {
  const r = sbCv.getBoundingClientRect();
  return [Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)), Math.min(1, Math.max(0, (e.clientY - r.top) / r.height))];
}
sbCv.addEventListener("pointerdown", (e) => {
  const p = sbPos(e), c = SB.color;
  sbCv.setPointerCapture(e.pointerId);
  if (SB.tool === "pen") SB.drawing = { t: "pen", c, pts: [p] };
  else if (SB.tool === "arrow") SB.drawing = { t: "arrow", c, a: p, b: p };
  else if (SB.tool === "circle") SB.drawing = { t: "circle", c, a: p, r: 0 };
  else if (SB.tool === "x") { SB.shapes.push({ t: "x", c, a: p }); sbDraw(); }
  else if (SB.tool === "player") { SB.shapes.push({ t: "player", c, a: p, s: $("#sbPlayer").value || "Spieler" }); sbDraw(); }
  else if (SB.tool === "text") {
    const s = $("#sbText").value.trim();
    if (!s) { toast("Erst oben Text eintippen, dann auf die Map klicken.", false); return; }
    SB.shapes.push({ t: "text", c, a: p, s }); sbDraw();
  }
});
sbCv.addEventListener("pointermove", (e) => {
  const d = SB.drawing; if (!d) return;
  const p = sbPos(e);
  if (d.t === "pen") d.pts.push(p); else if (d.t === "arrow") d.b = p; else if (d.t === "circle") d.r = Math.hypot(p[0] - d.a[0], p[1] - d.a[1]);
  sbDraw();
});
sbCv.addEventListener("pointerup", () => {
  const d = SB.drawing; SB.drawing = null;
  if (d && !(d.t === "arrow" && Math.hypot(d.b[0] - d.a[0], d.b[1] - d.a[1]) < 0.01)) {
    if (d.t === "pen") d.pts = d.pts.filter((_, i) => i % 2 === 0 || i === d.pts.length - 1).map((p) => [+p[0].toFixed(4), +p[1].toFixed(4)]);
    SB.shapes.push(d);
  }
  sbDraw();
});
$("#sbUndo").onclick = () => { SB.shapes.pop(); sbDraw(); };
$("#sbClear").onclick = () => { SB.shapes = []; sbDraw(); };
$("#sbClose").onclick = () => $("#sbModal").classList.add("hidden");
$("#sbMap").onchange = sbLoadBg;
$("#sbBg").onchange = async () => {
  const f = $("#sbBg").files[0]; if (!f) return;
  const fd = new FormData(); fd.append("map", $("#sbMap").value); fd.append("image", f);
  const r = await fetch("/api/strats/bg", { method: "POST", body: fd }).then((r) => r.json()).catch((e) => ({ error: e.message }));
  if (r.error) toast(r.error, false); else { toast("Kartenbild gespeichert"); sbLoadBg(); }
  $("#sbBg").value = "";
};
async function openBoard(id = null) {
  SB.id = id; SB.shapes = [];
  $("#sbName").value = ""; $("#sbNotes").value = ""; $("#sbMode").value = (state.match && state.match.mode) || "";
  $("#sbPlayer").innerHTML = ((state.team && state.team.players) || []).map((p) => `<option>${esc(p.ingame)}</option>`).join("") + "<option>Gegner</option>";
  let keep = "";
  if (id) {
    const all = await api("/api/strats?full=1");
    const s = (Array.isArray(all) ? all : []).find((x) => x.id === id);
    if (s) { SB.shapes = s.shapes || []; $("#sbName").value = s.name; $("#sbNotes").value = s.notes || ""; $("#sbMode").value = s.mode || ""; keep = s.map; }
  }
  await loadMapsInto($("#sbMap"), keep);
  $("#sbModal").classList.remove("hidden");
  sbLoadBg();
}
async function sbSave(post) {
  const r = await api("/api/strats", { method: "POST", body: {
    id: SB.id, name: $("#sbName").value || "Strat", map: $("#sbMap").value, mode: $("#sbMode").value,
    notes: $("#sbNotes").value, shapes: SB.shapes, png: sbCv.toDataURL("image/png") } });
  if (r.error) { toast("Speichern fehlgeschlagen: " + r.error, false); return; }
  SB.id = r.id;
  if (post) {
    const p = await api(`/api/strats/${r.id}/discord`, { method: "POST", body: {} });
    toast(p.error ? "Gespeichert, aber Posten fehlgeschlagen: " + p.error : "Gespeichert und in Discord gepostet", !p.error);
  } else toast("Strat gespeichert");
  loadStrats();
}
$("#sbSave").onclick = () => sbSave(false);
$("#sbSavePost").onclick = () => sbSave(true);
$("#sbNew").onclick = () => openBoard(null);
async function loadTactic() { await loadMapsInto($("#hmMap")); loadHeatmap(); loadStrats(); }

// --- Zuschauen ------------------------------------------------------------
let watchOn = false;
async function loadWatch(fillSources = false) {
  const w = await api("/api/watch");
  if (w.error) return;
  watchOn = !!w.on;
  if (fillSources || !$("#wSource").options.length) {
    const cur = $("#wSource").value || w.source;
    const opts = [];
    for (let i = 1; i <= (w.monitors || 1); i++) opts.push([`monitor:${i}`, `🖥️ Bildschirm ${i}`]);
    const wins = w.windows || [];
    ["Call of Duty", "Discord"].forEach((t) => { if (!wins.some((x) => x.toLowerCase().includes(t.toLowerCase()))) wins.unshift(t); });
    wins.forEach((t) => opts.push([`window:${t}`, `🪟 ${t}`]));
    $("#wSource").innerHTML = opts.map(([v, l]) => `<option value="${esc(v)}">${esc(l)}</option>`).join("");
    if (cur) $("#wSource").value = cur;
  }
  $("#wComment").checked = !!w.comment;
  $("#wToggle").textContent = watchOn ? "■ Stopp" : "▶ Zuschauen";
  $("#wToggle").classList.toggle("danger", watchOn);
  $("#wBox").classList.toggle("hidden", !watchOn);
  const last = (w.log || []).slice(-1)[0];
  $("#wLast").textContent = watchOn ? (last ? "Yvara AI sieht: " + last.text : "Yvara AI schaut hin … die erste Einschätzung kommt in ein paar Sekunden.") : "";
  if (!w.available) $("#wLast").textContent = "Bildschirmaufnahme ist aus.";
}
$("#wToggle").onclick = async () => {
  const r = await api("/api/watch", { method: "POST", body: { on: !watchOn, source: $("#wSource").value, comment: $("#wComment").checked } });
  if (r.error) toast(r.error, false); else toast(r.on ? "Yvara AI schaut zu" : "Zuschauen beendet");
  loadWatch();
};
$("#wSource").onchange = () => { if (watchOn) api("/api/watch", { method: "POST", body: { on: true, source: $("#wSource").value } }); };
$("#wComment").onchange = () => api("/api/watch", { method: "POST", body: { on: watchOn, comment: $("#wComment").checked } });
setInterval(() => {
  if (watchOn && !$("#tab-match").classList.contains("hidden")) $("#wImg").src = `/api/watch/preview.jpg?t=${Date.now()}`;
}, 1000);
setInterval(() => { if (!$("#tab-match").classList.contains("hidden")) loadWatch(); }, 5000);
loadWatch(true);


// ---------------------------------------------------------------- Startseite (Kacheln), Konsole, Chat-Fenster
const VIBE_NAMES = { normal: "Normal", toxic: "Toxisch", flirty: "Flirty" };
function homeUpdate() {
  if (!state) return;
  const set = state.settings || {};
  document.querySelectorAll("#modeTiles .tile").forEach((t) => t.classList.toggle("on", t.dataset.mode === state.coaching_mode));
  document.querySelectorAll("#vibeTiles .tile").forEach((t) => t.classList.toggle("on", t.dataset.vibe === (set["coach.vibe"] || "normal")));
  document.querySelectorAll("#voiceTiles .tile").forEach((t) => t.classList.toggle("on", set["tts.engine"] === "xtts" && t.dataset.voice === (set["tts.xtts_voice"] || "Damien Black")));
  const card = (id, ok, text) => { const c = $(id); c.classList.toggle("ok", ok === true); c.classList.toggle("bad", ok === false); c.querySelector("small").textContent = text; c.title = text; };
  const br = state.brain || { ok: true };
  card("#cBrain", !!br.ok, br.ok ? `Bereit · ${(state.llm && state.llm.model) || "lokal"}` : br.message);
  const d = state.discord || {};
  card("#cDiscord", state.discord_error ? false : d.ready ? true : null,
    state.discord_error || (d.ready ? (d.voice ? `Im Voice: ${d.voice}` : "Online · sag „Coach, komm rein“") : d.enabled ? "verbindet …" : "aus (lokaler Start)"));
  const v = set["tts.xtts_voice"] || "Damien Black";
  const vName = (homeVoices.find((x) => x.id === v) || {}).name || v.replace("custom:", "");
  card("#cVoice", true, set["tts.engine"] === "xtts" ? vName : "Piper (schnell)");
}
let homeVoices = [];
async function loadHomeVoices() {
  const v = await api("/api/voices");
  if (!v) return;
  const nice = { "Damien Black": ["🧔", "Yvara AI", "Männlich · Standard"], "Ana Florence": ["💜", "Yvara Girl", "Weiblich · erwachsen, flirty"] };
  homeVoices = [
    ...(v.xtts_speakers || []).map((x) => ({ id: x.id, ic: (nice[x.id] || ["🎙️"])[0], name: (nice[x.id] || [0, x.id])[1], sub: (nice[x.id] || [0, 0, x.label])[2] })),
    ...(v.custom || []).filter((c) => !c.silent).map((c) => ({ id: c.id, ic: "🎤", name: c.id.slice(7).replace(/^./, (m) => m.toUpperCase()), sub: c.style ? "Eigene Stimme · mit Stil" : "Eigene Stimme" })),
  ];
  $("#voiceTiles").innerHTML = homeVoices.map((x) => `<button class="tile" data-voice="${esc(x.id)}"><span class="ic">${x.ic}</span><b>${esc(x.name)}</b><small>${esc(x.sub)}</small><span class="play" title="Probe hören">▶</span></button>`).join("");
  document.querySelectorAll("#voiceTiles .tile").forEach((t) => {
    t.onclick = async (e) => {
      const id = t.dataset.voice;
      if (e.target.classList.contains("play")) {
        e.stopPropagation(); e.target.textContent = "…";
        try {
          const r = await api("/api/voices/preview", { method: "POST", body: { lang: "de", engine: "xtts", xtts_voice: id, xtts_voice_en: "" } });
          if (r && r.audio) { audioEl.src = r.audio; audioEl.play().catch(() => {}); } else toast("Probe ging nicht: " + ((r && r.error) || "?"), false);
        } finally { e.target.textContent = "▶"; }
        return;
      }
      await api("/api/settings", { method: "POST", body: { "tts.engine": "xtts", "tts.xtts_voice": id, "tts.xtts_voice_en": "" } });
      state.settings["tts.engine"] = "xtts"; state.settings["tts.xtts_voice"] = id; voiceWanted = id; voiceWantedEn = "";
      toast(`Stimme: ${t.querySelector("b").textContent}`); homeUpdate();
    };
  });
  homeUpdate();
}
document.querySelectorAll("#modeTiles .tile").forEach((t) => t.onclick = () => { $("#cMode").value = t.dataset.mode; $("#cMode").dispatchEvent(new Event("change")); });
document.querySelectorAll("#vibeTiles .tile").forEach((t) => t.onclick = async () => {
  await api("/api/settings", { method: "POST", body: { "coach.vibe": t.dataset.vibe } });
  state.settings["coach.vibe"] = t.dataset.vibe; $("#sVibe").value = t.dataset.vibe;
  toast(`Stil: ${VIBE_NAMES[t.dataset.vibe]}`); homeUpdate();
});
async function loadLogs() {
  const r = await api("/api/logs?n=400");
  if (!r) return;
  const box = $("#logBox"), atEnd = box.scrollTop + box.clientHeight >= box.scrollHeight - 30;
  box.innerHTML = (r.lines || []).map((l) => `<span class="${/ERROR|CRITICAL|Traceback/.test(l) ? "e" : /WARNING/.test(l) ? "w" : ""}">${esc(l)}</span>`).join("\n");
  if ($("#logFollow").checked && atEnd !== false) box.scrollTop = box.scrollHeight;
}
setInterval(() => { if (!$("#tab-console").classList.contains("hidden")) loadLogs(); }, 2000);
setInterval(homeUpdate, 1500);
$("#chatOpen").onclick = () => { $("#chatDock").classList.toggle("hidden"); if (!$("#chatDock").classList.contains("hidden")) $("#input").focus(); };
$("#chatClose").onclick = () => $("#chatDock").classList.add("hidden");
$("#quitBtn").onclick = async () => {
  toast("Yvara AI wird komplett beendet …");
  try { await api("/api/quit", { method: "POST", body: {} }); } catch (e) {}
  setTimeout(() => { try { window.close(); } catch (e) {} if (window.pywebview && window.pywebview.api && window.pywebview.api.close) window.pywebview.api.close(); }, 800);
};
loadHomeVoices();


// ---------------------------------------------------------------- Sprach-Call in der App (ohne Discord)
const call = { on: false, stream: null, ctx: null, proc: null, ring: [], rec: null, speaking: false, busy: false,
  floor: 0.004, voiced: 0, silent: 0, startedAt: 0 };
function callSet(st, text) {
  $("#callOrb").className = "call-orb " + st;
  $("#callState").textContent = text;
}
function callLog(cls, who, text) {
  const li = document.createElement("li"); li.className = cls; li.innerHTML = `<b>${esc(who)}:</b> ${esc(text)}`;
  $("#callLog").appendChild(li); $("#callLog").scrollTop = 1e9;
}
function wav16k(chunks, rate) {
  let n = 0; chunks.forEach((c) => n += c.length);
  const all = new Float32Array(n); let o = 0; chunks.forEach((c) => { all.set(c, o); o += c.length; });
  const ratio = rate / 16000, len = Math.floor(all.length / ratio), pcm = new Int16Array(len);
  for (let i = 0; i < len; i++) { const v = Math.max(-1, Math.min(1, all[Math.floor(i * ratio)])); pcm[i] = v * 32767; }
  const buf = new ArrayBuffer(44 + pcm.length * 2), dv = new DataView(buf);
  const w = (p, s) => { for (let i = 0; i < s.length; i++) dv.setUint8(p + i, s.charCodeAt(i)); };
  w(0, "RIFF"); dv.setUint32(4, 36 + pcm.length * 2, true); w(8, "WAVEfmt "); dv.setUint32(16, 16, true); dv.setUint16(20, 1, true);
  dv.setUint16(22, 1, true); dv.setUint32(24, 16000, true); dv.setUint32(28, 32000, true); dv.setUint16(32, 2, true); dv.setUint16(34, 16, true);
  w(36, "data"); dv.setUint32(40, pcm.length * 2, true); new Int16Array(buf, 44).set(pcm);
  return new Blob([buf], { type: "audio/wav" });
}
async function callSend(blob) {
  call.busy = true; callSet("think", "Yvara AI denkt …");
  try {
    const fd = new FormData(); fd.append("audio", blob, "call.wav");
    const r = await fetch("/api/call", { method: "POST", body: fd }).then((x) => x.json());
    if (r.text) callLog("me", "Du", r.text);
    for (const rep of r.replies || []) {
      if (!call.on) break;
      callLog("vy", state.name || "Yvara AI", rep.text);
      if (rep.audio) { call.speaking = true; callSet("speak", `${state.name || "Yvara AI"} spricht …`); await playWait(rep.audio); call.speaking = false; }
    }
  } catch (e) { toast("Call: " + e, false); }
  call.busy = false; call.ring = []; if (call.on) callSet("listen", "Ich höre zu …");
}
function playWait(url) {
  return new Promise((res) => { const a = new Audio(url); a.onended = res; a.onerror = res; a.play().catch(res); call.audio = a; });
}
async function callStart() {
  try {
    call.stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
  } catch (e) { toast("Kein Mikrofon-Zugriff: " + e, false); return; }
  call.on = true; $("#callBtn").classList.add("on"); $("#callBtn").textContent = "📞 Im Call";
  $("#callBox").classList.remove("hidden"); $("#callLog").innerHTML = "";
  document.querySelector('.tabs button[data-tab="home"]').click();
  call.ctx = new AudioContext();
  const src = call.ctx.createMediaStreamSource(call.stream);
  call.proc = call.ctx.createScriptProcessor(2048, 1, 1);
  const rate = call.ctx.sampleRate, pre = Math.ceil(rate * 0.4 / 2048);
  call.ring = []; call.rec = null;
  call.proc.onaudioprocess = (ev) => {
    const d = new Float32Array(ev.inputBuffer.getChannelData(0));
    let sum = 0; for (const v of d) sum += v * v; const rms = Math.sqrt(sum / d.length);
    $("#callLevel").style.width = Math.min(100, rms * 900) + "%";
    if (!call.on || call.busy || call.speaking) return;
    if (!call.rec) {
      call.ring.push(d); if (call.ring.length > pre) call.ring.shift();
      call.floor = call.floor * 0.97 + Math.min(rms, 0.05) * 0.03;
      if (rms > Math.max(0.015, call.floor * 3)) { call.voiced++; if (call.voiced >= 3) { call.rec = call.ring.slice(); call.silent = 0; call.startedAt = Date.now(); callSet("listen", "Du sprichst …"); } }
      else call.voiced = 0;
    } else {
      call.rec.push(d);
      if (rms < Math.max(0.01, call.floor * 2)) call.silent += d.length / rate; else call.silent = 0;
      const long = Date.now() - call.startedAt > 20000;
      if (call.silent > 0.8 || long) {
        const chunks = call.rec; call.rec = null; call.voiced = 0;
        if (chunks.length * 2048 / rate > 0.6) callSend(wav16k(chunks, rate)); else callSet("listen", "Ich höre zu …");
      }
    }
  };
  src.connect(call.proc); call.proc.connect(call.ctx.destination);
  callSet("listen", "Ich höre zu …");
}
function callStop() {
  call.on = false; call.rec = null;
  try { call.proc && call.proc.disconnect(); call.ctx && call.ctx.close(); } catch (e) {}
  if (call.stream) call.stream.getTracks().forEach((t) => t.stop());
  if (call.audio) call.audio.pause();
  $("#callBtn").classList.remove("on"); $("#callBtn").textContent = "📞 Call"; $("#callBox").classList.add("hidden");
}
$("#callBtn").onclick = () => (call.on ? callStop() : callStart());
$("#callEnd").onclick = callStop;

// --- Caster-Modus -----------------------------------------------------------
let caster = null, castLastId = 0, castVoicesLoaded = false;
const KF_PRESET = [0.0, 0.42, 0.3, 0.2];  // CoD: Killfeed links über der Minimap/Mitte links
const SRC_HINT = {
  stream: "Port, auf dem OBS das Bild schickt (Standard 9000). In OBS: srt://IP-DIESES-GERÄTS:9000 – siehe Anleitung.",
  screen: "Yvara AI schaut auf den Hauptbildschirm dieses Geräts.",
  monitor: "Monitor-Nummer, z. B. 2",
  window: "Teil des Fenstertitels, z. B. „OBS“ oder „Fensterprojektor“",
  device: "Gerätenummer der Capture-Karte, meistens 0",
  sync: "Bild kommt per Sync vom Gaming-PC (unter 🔗 Sync einrichten) – kaum Verzögerung.",
  twitch: "Twitch-Kanal (hat 2–5 s Verzögerung – für Sofort-Reaktionen lieber OBS übers Heimnetz)",
};
function castSrcSplit(src) {
  src = src || "screen";
  const i = src.indexOf(":");
  return i < 0 ? [src, ""] : [src.slice(0, i), src.slice(i + 1)];
}
async function loadCastVoices() {
  const v = await api("/api/voices");
  if (failed(v)) return;
  const nice = { "Damien Black": "Yvara AI – männlich (Standard)", "Ana Florence": "Yvara Girl – weiblich, erwachsen" };
  const opts = [["", "Wie die Haupt-Stimme"]];
  (v.xtts_speakers || []).forEach((x) => opts.push([x.id, "XTTS · " + (nice[x.id] || x.label || x.id)]));
  (v.custom || []).filter((c) => !c.silent).forEach((c) => opts.push([c.id, "Eigene Stimme · " + c.id.slice(7)]));
  for (let i = 1; i <= 5; i++) opts.push(["sup:M" + i, "Supertonic · männlich " + i + " (sehr schnell)"]);
  for (let i = 1; i <= 5; i++) opts.push(["sup:F" + i, "Supertonic · weiblich " + i + " (sehr schnell)"]);
  $("#castVoice").innerHTML = opts.map(([id, l]) => `<option value="${esc(id)}">${esc(l)}</option>`).join("");
  castVoicesLoaded = true;
}
function castFill(d) {
  caster = d;
  document.querySelectorAll("#castSwitch button").forEach((b) => b.classList.toggle("on", (b.dataset.role === "cast") === !!d.active));
  document.querySelectorAll("#castLang .tile").forEach((t) => t.classList.toggle("on", t.dataset.v === d.lang));
  document.querySelectorAll("#castStyle .tile").forEach((t) => t.classList.toggle("on", t.dataset.v === d.style));
  const src = d.source || {};
  $("#castSrcPill").textContent = "Bild: " + (src.ok ? "✓ " : "✕ ") + (src.source || "–") + (src.error ? " (" + src.error + ")" : "");
  $("#castSrcPill").className = "pill " + (src.ok ? "ok" : "bad");
  const tw = d.twitch || {};
  $("#castChatPill").textContent = tw.channel ? `Chat: #${tw.channel} ${tw.connected ? "✓" : d.active ? "verbindet …" : ""}` : "Chat: aus";
  $("#castChatPill").className = "pill " + (tw.connected ? "ok" : "");
  $("#castOutPill").textContent = "Ausgabe: " + (d.output === "overlay" ? "OBS-Overlay" : "Lautsprecher");
  $("#castOverlayUrl").textContent = d.overlay_url || "–";
  const editing = document.activeElement && document.activeElement.closest && document.activeElement.closest(".castgrid");
  if (!editing && !castFill.done) {
    const [kind, ref] = castSrcSplit(src.source || "screen");
    $("#castSrcKind").value = kind; $("#castSrcRef").value = ref || (kind === "stream" ? "9000" : "");
    $("#castSrcHint").textContent = SRC_HINT[kind] || "";
    $("#castOutput").value = d.output || "local";
    $("#castLan").checked = !!d.lan_overlay;
    $("#castTwitch").value = tw.channel || "";
    $("#castInterval").value = d.interval_s || 7;
    const m = d.match || {};
    $("#castTitle").value = m.title || ""; $("#castTeamA").value = m.team_a || ""; $("#castTeamB").value = m.team_b || ""; $("#castScore").value = m.score || "";
    const r = d.killfeed_region;
    ["#kfX", "#kfY", "#kfW", "#kfH"].forEach((s, i) => $(s).value = r ? Math.round(r[i] * 100) : "");
    if (castVoicesLoaded) $("#castVoice").value = d.voice || "";
    castFill.done = true;
  }
  const lines = (d.lines || []).filter((x) => x.id > castLastId);
  if (lines.length) {
    const ul = $("#castLines");
    if (!castLastId) ul.innerHTML = "";
    lines.forEach((x) => {
      const li = document.createElement("li");
      li.className = (x.kind === "shout" ? "shout " : "") + "h" + (x.hype | 0);
      li.innerHTML = `<small>${new Date(x.ts * 1000).toLocaleTimeString().slice(0, 8)}</small>`;
      li.appendChild(document.createTextNode((x.reply_to ? "@" + x.reply_to + " " : "") + x.text));
      ul.appendChild(li);
    });
    while (ul.children.length > 60) ul.firstChild.remove();
    ul.scrollTop = ul.scrollHeight;
    castLastId = lines[lines.length - 1].id;
  }
  const chat = d.chat || [];
  if (chat.length) {
    $("#castChat").innerHTML = chat.map((m) => `<li><small>${esc(m.user)}${m.mod ? " 🛡️" : m.sub ? " ⭐" : ""}</small>${esc(m.text)}</li>`).join("");
    $("#castChat").scrollTop = $("#castChat").scrollHeight;
  }
  const L = d.learn || {};
  const lib = d.style_lib || {};
  $("#castLearnState").textContent = (L.msg || "") + (lib.phrases ? ` · Bibliothek: ${lib.phrases} Sprüche, ${lib.techniques} Technik-Tipps` : "");
  $("#castLearnBtn").disabled = !!L.running;
  if (d.error) $("#castSrcHint").textContent = "⚠️ " + d.error;
}
async function loadCaster() {
  if (!castVoicesLoaded) { await loadCastVoices(); castFill.done = false; }
  const d = await api("/api/caster");
  if (!failed(d)) castFill(d);
}
function castPreview() { $("#castPreview").src = "/api/caster/preview.jpg?t=" + Date.now(); }
async function castPost(body, okText) {
  const r = await api("/api/caster", { method: "POST", body });
  if (failed(r)) { toast(r.error || "Ging nicht", false); return r; }
  castFill({ ...caster, ...r, overlay_url: caster && caster.overlay_url, lan_overlay: body.lan_overlay ?? (caster && caster.lan_overlay) });
  if (okText) toast(okText);
  return r;
}
document.querySelectorAll("#castSwitch button").forEach((b) => b.onclick = async () => {
  const on = b.dataset.role === "cast";
  await castPost({ active: on }, on ? "Caster-Modus an 📣" : "Zurück im Coach-Modus 🧠");
  refresh();
});
document.querySelectorAll("#castLang .tile").forEach((t) => t.onclick = () => castPost({ lang: t.dataset.v }, t.dataset.v === "de" ? "Cast auf Deutsch" : "Casting in English"));
document.querySelectorAll("#castStyle .tile").forEach((t) => t.onclick = () => castPost({ style: t.dataset.v }, "Stil: " + t.querySelector("b").textContent));
document.querySelectorAll("#castMoments button").forEach((b) => b.onclick = async () => {
  const r = await api("/api/caster/moment", { method: "POST", body: { kind: b.dataset.k } });
  if (failed(r)) toast(r.error || "Ging nicht", false, b); else toast("📣 " + r.shout, true, b);
});
$("#castSrcKind").onchange = () => {
  const k = $("#castSrcKind").value;
  $("#castSrcHint").textContent = SRC_HINT[k] || "";
  $("#castSrcRef").placeholder = { stream: "9000", monitor: "2", window: "OBS", device: "0", twitch: "kanalname", screen: "", sync: "" }[k] || "";
  if (k === "stream" && !$("#castSrcRef").value) $("#castSrcRef").value = "9000";
  $("#castSrcRef").classList.toggle("hidden", k === "screen" || k === "sync");
};
$("#kfPreset").onclick = () => { ["#kfX", "#kfY", "#kfW", "#kfH"].forEach((s, i) => $(s).value = Math.round(KF_PRESET[i] * 100)); };
$("#kfOff").onclick = () => { ["#kfX", "#kfY", "#kfW", "#kfH"].forEach((s) => $(s).value = ""); };
$("#castPrevBtn").onclick = castPreview;
$("#castCopyUrl").onclick = () => { navigator.clipboard && navigator.clipboard.writeText($("#castOverlayUrl").textContent).then(() => toast("Adresse kopiert")); };
$("#castSave").onclick = async () => {
  const kind = $("#castSrcKind").value, ref = $("#castSrcRef").value.trim();
  const kf = ["#kfX", "#kfY", "#kfW", "#kfH"].map((s) => $(s).value === "" ? null : Number($(s).value) / 100);
  const body = {
    source: kind === "screen" || kind === "sync" ? kind : `${kind}:${ref}`, voice: $("#castVoice").value, output: $("#castOutput").value,
    twitch_channel: $("#castTwitch").value.trim(), interval_s: Number($("#castInterval").value) || 7,
    title: $("#castTitle").value, team_a: $("#castTeamA").value, team_b: $("#castTeamB").value, score: $("#castScore").value,
    killfeed_region: kf.some((v) => v === null) ? null : kf, lan_overlay: $("#castLan").checked,
  };
  const r = await castPost(body, "Caster-Einstellungen gespeichert");
  if (!failed(r)) setTimeout(castPreview, 1500);
};
$("#castLearnBtn").onclick = async () => {
  const f = $("#castLearnFile").files[0], url = $("#castLearnUrl").value.trim(), text = $("#castLearnText").value.trim();
  if (!f && !url && !text) return toast("Datei, Link oder Text angeben", false);
  const fd = new FormData();
  if (f) fd.append("file", f);
  if (url) fd.append("url", url);
  if (text) fd.append("text", text);
  $("#castLearnBtn").disabled = true;
  const r = await fetch("/api/caster/learn", { method: "POST", body: fd }).then((x) => x.json()).catch(() => ({ ok: false, error: "Yvara AI nicht erreichbar" }));
  $("#castLearnBtn").disabled = false;
  if (!r.ok) return toast(r.error || "Ging nicht", false);
  toast(r.started ? "Yvara AI schaut sich den Cast an …" : `Gelernt: ${r.phrases} Sprüche, ${r.techniques} Tipps`);
  $("#castLearnText").value = ""; $("#castLearnUrl").value = ""; $("#castLearnFile").value = "";
  loadCaster();
};
document.querySelector('.tabs button[data-tab="caster"]').addEventListener("click", () => { loadCaster(); castPreview(); });
setInterval(() => { if (!$("#tab-caster").classList.contains("hidden")) loadCaster(); }, 1000);

// --- Sync (zwei Geräte) ------------------------------------------------------
let syncRoleSel = "off";
function syncShow() {
  document.querySelectorAll("#syncRole .tile").forEach((t) => t.classList.toggle("on", t.dataset.v === syncRoleSel));
  $("#syncRecvBox").classList.toggle("hidden", syncRoleSel !== "receive");
  $("#syncSendBox").classList.toggle("hidden", syncRoleSel !== "send");
}
async function loadSync(fill = true) {
  const d = await api("/api/sync");
  if (failed(d)) return;
  if (fill) {
    syncRoleSel = d.role || "off";
    const s = d.settings || {};
    $("#syncHost").value = s.host || ""; $("#syncPair").value = s.code || "";
    $("#syncVideo").checked = s.video !== false; $("#syncAudio").checked = s.audio !== false; $("#syncMic").checked = !!s.mic;
    $("#syncMon").value = s.monitor || 1;
  }
  $("#syncIp").textContent = d.ip || "–"; $("#syncCode").textContent = d.code || "(nach dem Neustart)";
  const r = d.receiver;
  $("#syncRecvState").textContent = !r ? "Empfang startet nach dem Speichern" : r.connected ? `✓ verbunden mit ${r.peer}${r.picture ? " · Bild kommt an" : ""}` : "Wartet auf den PC …";
  $("#syncRecvState").className = "pill " + (r && r.connected ? "ok" : "");
  syncShow();
}
document.querySelectorAll("#syncRole .tile").forEach((t) => t.onclick = () => { syncRoleSel = t.dataset.v; syncShow(); });
$("#syncSave").onclick = async () => {
  const body = { role: syncRoleSel, restart: true };
  if (syncRoleSel === "send") Object.assign(body, { host: $("#syncHost").value.trim(), code: $("#syncPair").value.trim(),
    video: $("#syncVideo").checked, audio: $("#syncAudio").checked, mic: $("#syncMic").checked, monitor: Number($("#syncMon").value) || 1 });
  const r = await api("/api/sync", { method: "POST", body });
  if (failed(r)) return toast(r.error || "Ging nicht", false, $("#syncSave"));
  $("#syncInfo").textContent = "Gespeichert – Yvara AI startet neu …";
  setTimeout(() => location.reload(), 9000);
};
document.querySelector('.tabs button[data-tab="sync"]').addEventListener("click", () => loadSync(true));
setInterval(() => { if (!$("#tab-sync").classList.contains("hidden")) loadSync(false); }, 2000);
