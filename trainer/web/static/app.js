"use strict";

/* Hub — Vanilla JS, kein Framework, kein Build-Step.
 * Sidebar-App-Shell mit Hash-Router und 6 Views:
 *   #/               Dashboard (Puls-Hero + Stat-Tiles + Termine/Workouts/Essen)
 *   #/chat/isa       Chat mit Isa
 *   #/chat/assistant Chat mit dem Assistant
 *   #/health         Health-Karten (Sparklines, Workouts/Woche, Protein)
 *   #/training       Workout-Liste (60 Tage, aufklappbare Sätze)
 *   #/ernaehrung     Protein-Balken (30 Tage) + Mahlzeiten-Liste
 * Es scrollt ausschließlich der View-Container bzw. die Chat-Nachrichtenliste,
 * nie der Body. */

const AGENT_COLORS = { isa: "var(--isa)", assistant: "var(--assistant)" };
const AGENT_META = {
  isa: { display: "Isa", subtitle: "Fitness & Ernährung" },
  assistant: { display: "Assistant", subtitle: "Dein Chief of Staff" },
};
const EMPTY_CHAT_HINTS = {
  isa: "Noch keine Nachrichten — frag Isa nach deinem Schlaf.",
  assistant: "Noch keine Nachrichten — frag deinen Assistenten, was heute ansteht.",
};

const REDUCE_MOTION = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

const el = {
  container: document.getElementById("view-container"),
  title: document.getElementById("view-title"),
  subtitle: document.getElementById("view-subtitle"),
  sidebarNav: document.getElementById("sidebar-nav"),
  navIndicator: document.getElementById("nav-indicator"),
  statusDot: document.getElementById("status-dot"),
  footerStatus: document.getElementById("footer-status"),
  footerSync: document.getElementById("footer-sync"),
  footerDb: document.getElementById("footer-db"),
  tabMore: document.getElementById("tab-more"),
  moreSheet: document.getElementById("more-sheet"),
};

const state = {
  route: "#/",
  sending: false,
  cache: {}, // pro-Endpoint-Cache, damit ein View-Wechsel nicht jedes Mal neu lädt
};

/* ---------------------------------------------------------------------
 * Kleine Helfer
 * --------------------------------------------------------------------- */

function esc(str) {
  return String(str ?? "").replace(
    /[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]
  );
}

function fmtNum(v, digits = 0) {
  if (v === null || v === undefined || Number.isNaN(v)) return "–";
  return digits > 0 ? Number(v).toFixed(digits) : Math.round(v).toString();
}

function fmtTs(ts) {
  if (!ts) return "";
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return ts;
  return d.toLocaleString("de-DE", {
    day: "2-digit",
    month: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function fmtTime(ts) {
  if (!ts) return "";
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return ts;
  return d.toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" });
}

function fmtDate(iso) {
  if (!iso) return "–";
  const d = new Date(String(iso).slice(0, 10) + "T00:00:00");
  if (Number.isNaN(d.getTime())) return String(iso);
  return d.toLocaleDateString("de-DE", { weekday: "short", day: "2-digit", month: "2-digit" });
}

function relativeTime(iso) {
  if (!iso) return null;
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return null;
  const diffMin = Math.round((Date.now() - then) / 60000);
  if (diffMin < 1) return "gerade eben";
  if (diffMin < 60) return `vor ${diffMin} Min`;
  const diffH = Math.round(diffMin / 60);
  if (diffH < 24) return `vor ${diffH} Std`;
  const diffD = Math.round(diffH / 24);
  return `vor ${diffD} Tag${diffD === 1 ? "" : "en"}`;
}

function average(values) {
  const nums = values.filter((v) => v !== null && v !== undefined && !Number.isNaN(v));
  if (nums.length === 0) return null;
  return nums.reduce((a, b) => a + b, 0) / nums.length;
}

function setAccent(agent) {
  document.documentElement.style.setProperty("--accent", AGENT_COLORS[agent] || AGENT_COLORS.isa);
}

async function fetchJson(url) {
  if (state.cache[url]) return state.cache[url];
  const res = await fetch(url);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const data = await res.json();
  state.cache[url] = data;
  return data;
}

/* Dekorativer Karten-Stagger: max 6 Stufen à 40ms, blockiert nichts. */
function applyStagger(rootEl) {
  const cards = rootEl.querySelectorAll(".stagger");
  cards.forEach((c, i) => {
    c.style.setProperty("--stagger-i", String(Math.min(i, 5)));
  });
}

/* ---------------------------------------------------------------------
 * SVG-Pfad-Helfer — lässt bei NULL-Werten Lücken statt zu interpolieren.
 * --------------------------------------------------------------------- */

function buildPathSegments(values, width, height, padding = 6) {
  const nums = values.filter((v) => v !== null && v !== undefined);
  if (nums.length === 0) return { paths: [], min: null, max: null };

  const lo = Math.min(...nums);
  const hi = Math.max(...nums);
  const range = hi - lo || 1;
  const n = values.length;
  const stepX = n > 1 ? (width - padding * 2) / (n - 1) : 0;
  const usableH = height - padding * 2;

  const points = values.map((v, i) => {
    if (v === null || v === undefined) return null;
    const x = padding + i * stepX;
    const y = padding + (1 - (v - lo) / range) * usableH;
    return [x, y];
  });

  const segments = [];
  let current = [];
  for (const p of points) {
    if (p === null) {
      if (current.length) segments.push(current);
      current = [];
    } else {
      current.push(p);
    }
  }
  if (current.length) segments.push(current);

  const paths = segments.map((seg) =>
    seg.map((p, i) => `${i === 0 ? "M" : "L"}${p[0].toFixed(2)},${p[1].toFixed(2)}`).join(" ")
  );

  return { paths, min: lo, max: hi };
}

function drawIn(pathEl) {
  if (REDUCE_MOTION) return;
  // ×3: wegen vector-effect non-scaling-stroke rechnet die Strichelung in
  // Bildschirm-Pixeln — auf breiten Screens wäre getTotalLength() (SVG-
  // Einheiten) zu kurz und die Linie bliebe abgeschnitten.
  const len = pathEl.getTotalLength() * 3;
  pathEl.style.strokeDasharray = String(len);
  pathEl.style.strokeDashoffset = String(len);
  // Zwei rAF, damit der Browser den Startzustand rendert, bevor die
  // CSS-Transition auf 0 greift.
  requestAnimationFrame(() => {
    requestAnimationFrame(() => {
      pathEl.style.strokeDashoffset = "0";
    });
  });
}

function renderSparkline(svgEl, values, cssClass) {
  svgEl.innerHTML = "";
  const width = 300;
  const height = 40;
  svgEl.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svgEl.setAttribute("preserveAspectRatio", "none");
  const { paths } = buildPathSegments(values, width, height, 4);
  paths.forEach((d) => {
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", d);
    path.setAttribute("class", cssClass);
    svgEl.appendChild(path);
    drawIn(path);
  });
}

/* ---------------------------------------------------------------------
 * Router
 * --------------------------------------------------------------------- */

const ROUTES = {
  "#/": { title: "Dashboard", subtitle: "Dein Tag auf einen Blick", render: renderDashboard },
  "#/chat/isa": {
    title: "Isa",
    subtitle: AGENT_META.isa.subtitle,
    render: (panel) => renderChat(panel, "isa"),
  },
  "#/chat/assistant": {
    title: "Assistant",
    subtitle: AGENT_META.assistant.subtitle,
    render: (panel) => renderChat(panel, "assistant"),
  },
  "#/health": { title: "Health", subtitle: "Oura & Trends, letzte 30 Tage", render: renderHealth },
  "#/training": {
    title: "Training",
    subtitle: "Workouts der letzten 60 Tage",
    render: renderTraining,
  },
  "#/ernaehrung": {
    title: "Ernährung",
    subtitle: "Mahlzeiten & Protein, letzte 30 Tage",
    render: renderErnaehrung,
  },
};

function normalizeHash(hash) {
  return ROUTES[hash] ? hash : "#/";
}

function moveNavIndicator(route) {
  const items = el.sidebarNav.querySelectorAll(".nav-item");
  let activeItem = null;
  items.forEach((item) => {
    const isActive = item.getAttribute("data-hash") === route;
    item.classList.toggle("active", isActive);
    if (isActive) activeItem = item;
  });
  document.querySelectorAll(".tabbar .tab-item, .more-sheet-item").forEach((item) => {
    item.classList.toggle("active", item.getAttribute("data-hash") === route);
  });

  if (activeItem) {
    // Ein einziger gleitender Indikator: translateY-Transition (200ms ease-out).
    const offset = activeItem.offsetTop;
    el.navIndicator.style.transform = `translateY(${offset}px)`;
    el.navIndicator.style.opacity = "1";
  } else {
    el.navIndicator.style.opacity = "0";
  }

  // Agent-Akzentfarbe: im Chat die Identitätsfarbe, sonst Isa-Koralle als Default.
  if (route === "#/chat/assistant") setAccent("assistant");
  else setAccent("isa");
}

async function navigate() {
  const route = normalizeHash(location.hash || "#/");
  if (route !== location.hash) {
    history.replaceState(null, "", route);
  }
  state.route = route;
  closeMoreSheet();

  const def = ROUTES[route];
  el.title.textContent = def.title;
  el.subtitle.textContent = def.subtitle;
  moveNavIndicator(route);

  const isChat = route.startsWith("#/chat/");
  el.container.classList.toggle("chat-mode", isChat);

  // Alte View sofort weg, neue View animiert rein (view-in im CSS).
  el.container.innerHTML = "";
  const panel = document.createElement("div");
  panel.className = "view-panel" + (isChat ? " chat-panel" : "");
  el.container.appendChild(panel);

  try {
    await def.render(panel);
  } catch (err) {
    panel.innerHTML = `<p class="empty-state">Ansicht konnte nicht geladen werden. (${esc(
      err && err.message
    )})</p>`;
  }

  // Rendern ist async und die Route kann inzwischen gewechselt haben.
  if (state.route === route) applyStagger(panel);
}

window.addEventListener("hashchange", navigate);

/* ---------------------------------------------------------------------
 * Sidebar-Footer (System-Status)
 * --------------------------------------------------------------------- */

function renderFooter(system) {
  el.statusDot.classList.remove("offline");
  el.footerStatus.textContent = "Online";
  const rel = relativeTime(system && system.oura_last_sync);
  el.footerSync.textContent = rel ? `Oura-Sync ${rel}` : "Oura-Sync: –";
  el.footerDb.textContent =
    system && system.db_size_mb != null ? `DB: ${fmtNum(system.db_size_mb, 1)} MB` : "DB: –";
}

function renderFooterOffline() {
  el.statusDot.classList.add("offline");
  el.footerStatus.textContent = "Offline";
  el.footerSync.textContent = "Oura-Sync: –";
  el.footerDb.textContent = "DB: –";
}

/* ---------------------------------------------------------------------
 * View 1 — Dashboard
 * --------------------------------------------------------------------- */

async function renderDashboard(panel) {
  panel.innerHTML = '<p class="empty-state">Lade Übersicht …</p>';

  let ov;
  let health;
  try {
    [ov, health] = await Promise.all([
      fetchJson("/api/overview"),
      fetchJson("/api/health/overview?days=30"),
    ]);
  } catch {
    renderFooterOffline();
    panel.innerHTML = '<p class="empty-state">Übersicht konnte nicht geladen werden.</p>';
    return;
  }
  if (state.route !== "#/") return;
  renderFooter(ov.system);

  const grid = document.createElement("div");
  grid.className = "dashboard-grid";
  panel.innerHTML = "";
  panel.appendChild(grid);

  // --- Puls-Hero ---
  const hero = document.createElement("section");
  hero.className = "card hero-card stagger";
  hero.setAttribute("aria-label", "Manuels Vitalwerte, letzte 30 Tage");
  hero.innerHTML = `
    <svg class="hero-svg" viewBox="0 0 1000 90" preserveAspectRatio="none" role="img" aria-labelledby="hero-title">
      <title id="hero-title">HRV-Verlauf der letzten 30 Tage</title>
      <g class="hero-glow-group"></g>
      <g class="hero-line-group"></g>
    </svg>
    <div class="hero-readouts">
      <div class="readout">
        <span class="readout-value" id="readout-hrv">–</span>
        <span class="readout-label">HRV heute</span>
      </div>
      <div class="readout">
        <span class="readout-value" id="readout-readiness">–</span>
        <span class="readout-label">Readiness</span>
      </div>
      <div class="readout">
        <span class="readout-value" id="readout-sleep">–</span>
        <span class="readout-label">Schlaf-Score</span>
      </div>
    </div>
  `;
  grid.appendChild(hero);

  const hrvValues = (ov.hrv_series_30d || []).map((d) => d.hrv_avg);
  const { paths } = buildPathSegments(hrvValues, 1000, 90, 10);
  const glowGroup = hero.querySelector(".hero-glow-group");
  const lineGroup = hero.querySelector(".hero-line-group");
  paths.forEach((d) => {
    const glow = document.createElementNS("http://www.w3.org/2000/svg", "path");
    glow.setAttribute("d", d);
    glow.setAttribute("class", "pulse-glow");
    glowGroup.appendChild(glow);
    const line = document.createElementNS("http://www.w3.org/2000/svg", "path");
    line.setAttribute("d", d);
    line.setAttribute("class", "pulse-line");
    lineGroup.appendChild(line);
    drawIn(glow);
    drawIn(line);
  });

  const today = ov.today || {};
  hero.querySelector("#readout-hrv").textContent =
    today.hrv_avg != null ? `${fmtNum(today.hrv_avg, 0)} ms` : "–";
  hero.querySelector("#readout-readiness").textContent = fmtNum(today.readiness_score);
  hero.querySelector("#readout-sleep").textContent = fmtNum(today.sleep_score);

  // --- 4 Stat-Tiles ---
  const daily = health.daily || [];
  const tiles = [
    ["Schlaf", daily.map((d) => d.sleep_score), today.sleep_score, ""],
    ["Readiness", daily.map((d) => d.readiness_score), today.readiness_score, ""],
    ["HRV", daily.map((d) => d.hrv_avg), today.hrv_avg, "ms"],
    ["Ruhepuls", daily.map((d) => d.resting_hr), today.resting_hr, "bpm"],
  ];
  tiles.forEach(([title, values, todayValue, unit]) => {
    const tile = metricCard(title, values, todayValue, unit, 0);
    tile.classList.add("stat-tile", "stagger");
    grid.appendChild(tile);
  });

  // --- Nächste Termine ---
  const eventsCard = document.createElement("section");
  eventsCard.className = "card info-card stagger";
  const events = ov.next_events || [];
  const eventsRows = events.length
    ? `<ul class="info-list">${events
        .map((ev) => {
          const time = ev.all_day ? "ganztags" : fmtTime(ev.start);
          const day = fmtDate(ev.start);
          return `<li class="info-row">
            <span class="info-row-time">${esc(time)}</span>
            <span class="info-row-main">
              <span class="info-row-title">${esc(ev.summary)}</span>
              <span class="info-row-sub">${esc(day)}${
            ev.calendar ? " · " + esc(ev.calendar) : ""
          }</span>
            </span>
          </li>`;
        })
        .join("")}</ul>`
    : '<p class="empty-state">Keine Termine in den nächsten 2 Tagen.</p>';
  eventsCard.innerHTML = `<h3 class="card-title">Nächste Termine</h3>${eventsRows}`;
  grid.appendChild(eventsCard);

  // --- Letzte Workouts ---
  const workoutsCardEl = document.createElement("section");
  workoutsCardEl.className = "card info-card stagger";
  const workouts = ov.last_workouts || [];
  const workoutRows = workouts.length
    ? `<ul class="info-list">${workouts
        .map(
          (w) => `<li class="info-row">
            <span class="info-row-time">${esc(fmtDate(w.date))}</span>
            <span class="info-row-main">
              <span class="info-row-title">${esc(w.type || "Workout")}</span>
              <span class="info-row-sub">${(w.sets || []).length} Sätze</span>
            </span>
          </li>`
        )
        .join("")}</ul>`
    : '<p class="empty-state">Noch nichts geloggt — sag’s Isa.</p>';
  workoutsCardEl.innerHTML = `<h3 class="card-title">Letzte Workouts</h3>${workoutRows}`;
  grid.appendChild(workoutsCardEl);

  // --- Heute gegessen ---
  const mealsCard = document.createElement("section");
  mealsCard.className = "card info-card stagger";
  const m = ov.meals_today || {};
  const mealBody =
    (m.count || 0) > 0
      ? `<div class="meal-summary">
          <div class="meal-protein">${fmtNum(m.protein_g, 0)}<span class="unit">g Protein</span></div>
          <div class="meal-meta">${fmtNum(m.calories_kcal, 0)} kcal · ${m.count} Mahlzeit${
          m.count === 1 ? "" : "en"
        }</div>
        </div>`
      : '<p class="empty-state">Heute noch nichts geloggt.</p>';
  mealsCard.innerHTML = `<h3 class="card-title">Heute gegessen</h3>${mealBody}`;
  grid.appendChild(mealsCard);
}

/* ---------------------------------------------------------------------
 * View 2/3 — Chat pro Agent
 * --------------------------------------------------------------------- */

function renderMessage(msg) {
  const wrap = document.createElement("div");
  const isUser = msg.role === "user";
  wrap.className = "msg " + (isUser ? "msg-user" : "msg-agent");
  const textEl = document.createElement("div");
  textEl.className = "msg-text";
  textEl.textContent = msg.content;
  const tsEl = document.createElement("span");
  tsEl.className = "msg-ts";
  tsEl.textContent = fmtTs(msg.ts);
  wrap.appendChild(textEl);
  wrap.appendChild(tsEl);
  return wrap;
}

async function renderChat(panel, agent) {
  const meta = AGENT_META[agent];

  panel.innerHTML = `
    <div class="chat-column">
      <div class="chat-messages" aria-live="polite"></div>
      <div class="typing-indicator" hidden>
        <span class="pulse-dot"></span>
        <span>${esc(meta.display)} antwortet …</span>
      </div>
      <form class="chat-input-row">
        <textarea
          class="chat-input"
          placeholder="Nachricht an ${esc(meta.display)} …"
          rows="1"
          aria-label="Nachricht"
        ></textarea>
        <button type="submit" class="chat-send" aria-label="Senden">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden="true">
            <path
              d="M4 12l16-7-6.5 7L20 19 4 12Z"
              stroke="currentColor"
              stroke-width="1.6"
              stroke-linejoin="round"
              fill="none"
            />
          </svg>
        </button>
      </form>
    </div>
  `;

  const messagesEl = panel.querySelector(".chat-messages");
  const formEl = panel.querySelector(".chat-input-row");
  const inputEl = panel.querySelector(".chat-input");
  const sendEl = panel.querySelector(".chat-send");
  const typingEl = panel.querySelector(".typing-indicator");

  function scrollToEnd() {
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  function autoResize() {
    inputEl.style.height = "auto";
    inputEl.style.height = Math.min(inputEl.scrollHeight, 160) + "px";
  }

  inputEl.addEventListener("input", autoResize);
  inputEl.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter" && !ev.shiftKey) {
      ev.preventDefault();
      formEl.requestSubmit();
    }
  });

  formEl.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const text = inputEl.value.trim();
    if (!text || state.sending) return;

    const emptyHint = messagesEl.querySelector(".empty-state");
    if (emptyHint) emptyHint.remove();

    messagesEl.appendChild(
      renderMessage({ role: "user", content: text, ts: new Date().toISOString() })
    );
    scrollToEnd();
    inputEl.value = "";
    autoResize();

    state.sending = true;
    sendEl.disabled = true;
    typingEl.hidden = false;

    try {
      const res = await fetch(`/api/chat/${agent}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: text }),
      });
      const data = await res.json();
      if (!document.body.contains(messagesEl)) return; // View gewechselt

      messagesEl.appendChild(
        renderMessage({
          role: "assistant",
          content:
            !res.ok || data.error
              ? `Fehler: ${data.error || "Antwort fehlgeschlagen."}`
              : data.reply,
          ts: new Date().toISOString(),
        })
      );
      scrollToEnd();
    } catch {
      if (document.body.contains(messagesEl)) {
        messagesEl.appendChild(
          renderMessage({
            role: "assistant",
            content: "Fehler: Server nicht erreichbar.",
            ts: new Date().toISOString(),
          })
        );
        scrollToEnd();
      }
    } finally {
      state.sending = false;
      sendEl.disabled = false;
      typingEl.hidden = true;
    }
  });

  // Historie laden (bewusst NICHT aus dem Cache — neue Nachrichten sollen erscheinen)
  messagesEl.innerHTML = '<p class="empty-state">Lade Nachrichten …</p>';
  try {
    const res = await fetch(`/api/chat/${agent}/history?limit=50`);
    const history = await res.json();
    if (!document.body.contains(messagesEl)) return;
    messagesEl.innerHTML = "";
    if (!Array.isArray(history) || history.length === 0) {
      const hint = document.createElement("p");
      hint.className = "empty-state";
      hint.textContent = EMPTY_CHAT_HINTS[agent] || "Noch keine Nachrichten.";
      messagesEl.appendChild(hint);
    } else {
      history.forEach((msg) => messagesEl.appendChild(renderMessage(msg)));
      scrollToEnd();
    }
  } catch {
    if (document.body.contains(messagesEl)) {
      messagesEl.innerHTML =
        '<p class="empty-state">Nachrichten konnten nicht geladen werden.</p>';
    }
  }
}

/* ---------------------------------------------------------------------
 * View 4 — Health
 * --------------------------------------------------------------------- */

function metricCard(title, values, todayValue, unit, digits = 0) {
  const avg = average(values);
  const compare =
    avg === null
      ? "Ø 30 Tage: keine Daten"
      : `Ø 30 Tage: ${fmtNum(avg, digits)}${unit ? " " + unit : ""}`;

  const wrap = document.createElement("section");
  wrap.className = "card";
  wrap.innerHTML = `
    <h3 class="card-title">${esc(title)}</h3>
    <div class="card-value">${todayValue != null ? fmtNum(todayValue, digits) : "–"}${
    todayValue != null && unit ? `<span class="unit">${esc(unit)}</span>` : ""
  }</div>
    <div class="card-compare">${compare}</div>
    <svg class="card-sparkline" aria-hidden="true"></svg>
  `;
  const svgEl = wrap.querySelector(".card-sparkline");
  renderSparkline(svgEl, values, "sparkline-path");

  const present = values.filter((v) => v != null);
  const svgTitle = document.createElementNS("http://www.w3.org/2000/svg", "title");
  svgTitle.textContent = `${title}, letzte 30 Tage: Min ${
    present.length ? fmtNum(Math.min(...present), digits) : "–"
  }, Max ${present.length ? fmtNum(Math.max(...present), digits) : "–"}`;
  svgEl.prepend(svgTitle);

  return wrap;
}

function workoutsPerWeekCard(workoutsPerWeek) {
  const wrap = document.createElement("section");
  wrap.className = "card card-wide stagger";
  const target = 3;
  const width = 640;
  const height = 92;
  const padding = 24;
  const maxCount = Math.max(target, ...workoutsPerWeek.map((w) => w.count), 1);
  const gap = (width - padding * 2) / Math.max(workoutsPerWeek.length, 1);
  const barWidth = gap / 1.8;

  const targetY = padding + (1 - target / maxCount) * (height - padding * 2 - 16);

  let bars = "";
  workoutsPerWeek.forEach((w, i) => {
    const barH = (w.count / maxCount) * (height - padding * 2 - 16);
    const x = padding + i * gap + (gap - barWidth) / 2;
    const y = height - padding - barH;
    const weekLabel = `KW ${isoWeekLabel(w.week)}`;
    bars += `<rect class="bar" x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${barWidth.toFixed(
      1
    )}" height="${Math.max(barH, 1).toFixed(1)}" rx="2"><title>${weekLabel}: ${
      w.count
    } Workout(s)</title></rect>`;
  });

  wrap.innerHTML = `
    <h3 class="card-title">Workouts pro Woche</h3>
    <svg class="bars-svg" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" role="img" aria-label="Workouts pro Woche, letzte 8 Wochen, Ziel 3 pro Woche">
      <line class="bar-target-line" x1="${padding}" y1="${targetY.toFixed(1)}" x2="${
    width - padding
  }" y2="${targetY.toFixed(1)}"><title>Ziel: ${target} pro Woche</title></line>
      ${bars}
    </svg>
    <div class="card-compare">Ziel-Linie bei ${target} Workouts/Woche</div>
  `;
  return wrap;
}

function isoWeekLabel(mondayIso) {
  const d = new Date(mondayIso + "T00:00:00");
  const target = new Date(d.valueOf());
  const dayNr = (d.getUTCDay() + 6) % 7;
  target.setUTCDate(target.getUTCDate() - dayNr + 3);
  const firstThursday = new Date(Date.UTC(target.getUTCFullYear(), 0, 4));
  const week =
    1 +
    Math.round(
      ((target.getTime() - firstThursday.getTime()) / 86400000 -
        3 +
        ((firstThursday.getUTCDay() + 6) % 7)) /
        7
    );
  return String(week).padStart(2, "0");
}

function proteinBarsCard(days, title) {
  const wrap = document.createElement("section");
  wrap.className = "card card-wide stagger";
  const width = 640;
  const height = 92;
  const padding = 24;
  const values = days.map((m) => m.protein_g || 0);
  const maxV = Math.max(...values, 1);
  const gap = (width - padding * 2) / Math.max(days.length, 1);
  const barWidth = gap / 1.8;

  let bars = "";
  days.forEach((m, i) => {
    const barH = ((m.protein_g || 0) / maxV) * (height - padding * 2 - 10);
    const x = padding + i * gap + (gap - barWidth) / 2;
    const y = height - padding - barH;
    const dateLabel = new Date(m.date + "T00:00:00").toLocaleDateString("de-DE", {
      day: "2-digit",
      month: "2-digit",
    });
    bars += `<rect class="bar" x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${barWidth.toFixed(
      1
    )}" height="${Math.max(barH, 1).toFixed(1)}" rx="2"><title>${dateLabel}: ${fmtNum(
      m.protein_g,
      0
    )} g Protein</title></rect>`;
  });

  const avgProtein = average(values);

  wrap.innerHTML = `
    <h3 class="card-title">${esc(title)}</h3>
    <svg class="bars-svg" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" role="img" aria-label="${esc(
    title
  )}">
      ${bars}
    </svg>
    <div class="card-compare">Ø ${fmtNum(avgProtein, 0)} g / Tag</div>
  `;
  return wrap;
}

async function renderHealth(panel) {
  panel.innerHTML = '<p class="empty-state">Lade Health-Daten …</p>';
  let data;
  try {
    data = await fetchJson("/api/health/overview?days=30");
  } catch {
    panel.innerHTML = '<p class="empty-state">Health-Daten konnten nicht geladen werden.</p>';
    return;
  }
  if (state.route !== "#/health") return;

  if (!data.daily || data.daily.length === 0) {
    panel.innerHTML = '<p class="empty-state">Noch keine Health-Daten.</p>';
    return;
  }

  const grid = document.createElement("div");
  grid.className = "health-grid";
  panel.innerHTML = "";
  panel.appendChild(grid);

  const daily = data.daily;
  const today = data.today || {};
  const tiles = [
    ["Schlaf-Score", daily.map((d) => d.sleep_score), today.sleep_score, ""],
    ["Readiness", daily.map((d) => d.readiness_score), today.readiness_score, ""],
    ["HRV", daily.map((d) => d.hrv_avg), today.hrv_avg, "ms"],
    ["Ruhepuls", daily.map((d) => d.resting_hr), today.resting_hr, "bpm"],
  ];
  tiles.forEach(([title, values, todayValue, unit]) => {
    const tile = metricCard(title, values, todayValue, unit, 0);
    tile.classList.add("stat-tile", "stagger");
    grid.appendChild(tile);
  });

  grid.appendChild(workoutsPerWeekCard(data.workouts_per_week || []));
  grid.appendChild(proteinBarsCard(data.meals_daily || [], "Protein, letzte 14 Tage"));
}

/* ---------------------------------------------------------------------
 * View 5 — Training
 * --------------------------------------------------------------------- */

const SOURCE_LABELS = {
  chat: "Chat",
  strong_csv: "Strong",
  apple_health: "Apple Health",
};

function workoutRow(w) {
  const row = document.createElement("article");
  row.className = "workout-row";

  const sets = w.sets || [];
  const sourceLabel = SOURCE_LABELS[w.source] || w.source || "?";

  const toggle = document.createElement("button");
  toggle.type = "button";
  toggle.className = "workout-row-toggle";
  toggle.setAttribute("aria-expanded", "false");
  toggle.innerHTML = `
    <span class="workout-date">${esc(fmtDate(w.date))}</span>
    <span class="workout-type">${esc(w.type || "Workout")}</span>
    <span class="source-badge">${esc(sourceLabel)}</span>
    <span class="set-count">${sets.length} Sätze</span>
    <svg class="chevron" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path d="M6 9l6 6 6-6" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" />
    </svg>
  `;

  const details = document.createElement("div");
  details.className = "workout-row-details";

  // Sätze nach Übung gruppieren (Reihenfolge beibehalten)
  const byExercise = new Map();
  sets.forEach((s) => {
    const key = s.exercise || "Übung";
    if (!byExercise.has(key)) byExercise.set(key, []);
    byExercise.get(key).push(s);
  });

  let setsHtml = "";
  byExercise.forEach((exSets, exercise) => {
    setsHtml += `<div class="sets-exercise-label">${esc(exercise)}</div>`;
    exSets.forEach((s) => {
      const parts = [];
      if (s.reps != null) parts.push(`${s.reps} Wdh`);
      if (s.weight_kg != null) parts.push(`${fmtNum(s.weight_kg, 1)} kg`);
      setsHtml += `<div class="set-line"><span class="set-no">#${
        s.set_no != null ? s.set_no : "–"
      }</span><span>${esc(parts.join(" · ") || "–")}</span></div>`;
    });
  });
  if (!setsHtml) setsHtml = '<p class="empty-state">Keine Sätze erfasst.</p>';

  details.innerHTML = `
    <div class="workout-row-details-inner">
      <div class="sets-table">${setsHtml}</div>
      ${w.notes ? `<div class="workout-notes">${esc(w.notes)}</div>` : ""}
    </div>
  `;

  toggle.addEventListener("click", () => {
    const open = row.classList.toggle("open");
    toggle.setAttribute("aria-expanded", open ? "true" : "false");
  });

  row.appendChild(toggle);
  row.appendChild(details);
  return row;
}

async function renderTraining(panel) {
  panel.innerHTML = '<p class="empty-state">Lade Workouts …</p>';
  let data;
  try {
    data = await fetchJson("/api/workouts?days=60");
  } catch {
    panel.innerHTML = '<p class="empty-state">Workouts konnten nicht geladen werden.</p>';
    return;
  }
  if (state.route !== "#/training") return;

  const workouts = data.workouts || [];
  if (workouts.length === 0) {
    panel.innerHTML = `<p class="empty-state">In den letzten 60 Tagen ist nichts geloggt.
      <span class="empty-state-hint">Sag Isa im Chat, was du trainiert hast — oder importiere ein Strong-CSV.</span></p>`;
    return;
  }

  const list = document.createElement("div");
  list.className = "workout-list";
  panel.innerHTML = "";
  panel.appendChild(list);

  workouts.forEach((w) => {
    const row = workoutRow(w);
    row.classList.add("stagger");
    list.appendChild(row);
  });
}

/* ---------------------------------------------------------------------
 * View 6 — Ernährung
 * --------------------------------------------------------------------- */

async function renderErnaehrung(panel) {
  panel.innerHTML = '<p class="empty-state">Lade Mahlzeiten …</p>';
  let data;
  try {
    data = await fetchJson("/api/meals?days=30");
  } catch {
    panel.innerHTML = '<p class="empty-state">Mahlzeiten konnten nicht geladen werden.</p>';
    return;
  }
  if (state.route !== "#/ernaehrung") return;

  panel.innerHTML = "";

  // Tages-Summen zu 30 durchgehenden Tagen auffüllen (fehlender Tag = 0 g)
  const totalsByDay = {};
  (data.daily_totals || []).forEach((t) => {
    totalsByDay[t.day] = t;
  });
  const days = [];
  const now = new Date();
  for (let i = 29; i >= 0; i--) {
    const d = new Date(now.getFullYear(), now.getMonth(), now.getDate() - i);
    const iso = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(
      d.getDate()
    ).padStart(2, "0")}`;
    const t = totalsByDay[iso];
    days.push({ date: iso, protein_g: t ? t.protein_g_sum || 0 : 0 });
  }
  panel.appendChild(proteinBarsCard(days, "Protein, letzte 30 Tage"));

  const meals = data.meals || [];
  if (meals.length === 0) {
    const hint = document.createElement("p");
    hint.className = "empty-state";
    hint.innerHTML = `In den letzten 30 Tagen ist nichts geloggt.
      <span class="empty-state-hint">Schick Isa ein Essens-Foto — sie schätzt die Makros und loggt für dich.</span>`;
    panel.appendChild(hint);
    return;
  }

  const list = document.createElement("div");
  list.className = "meal-list";
  panel.appendChild(list);

  meals.forEach((meal) => {
    const row = document.createElement("article");
    row.className = "meal-row stagger";
    const macros = `P ${fmtNum(meal.protein_g, 0)} · C ${fmtNum(meal.carbs_g, 0)} · F ${fmtNum(
      meal.fat_g,
      0
    )} · ${fmtNum(meal.calories_kcal, 0)} kcal`;
    row.innerHTML = `
      <span class="meal-row-time">${esc(fmtTs(meal.ts))}</span>
      <span class="meal-row-desc">${esc(meal.description || "Mahlzeit")}</span>
      <span class="meal-row-macros">${esc(macros)}</span>
    `;
    list.appendChild(row);
  });
}

/* ---------------------------------------------------------------------
 * Mobile: "Mehr"-Sheet
 * --------------------------------------------------------------------- */

function closeMoreSheet() {
  el.moreSheet.classList.remove("open");
  el.tabMore.setAttribute("aria-expanded", "false");
}

el.tabMore.addEventListener("click", () => {
  const open = el.moreSheet.classList.toggle("open");
  el.tabMore.setAttribute("aria-expanded", open ? "true" : "false");
});

document.addEventListener("click", (ev) => {
  if (
    el.moreSheet.classList.contains("open") &&
    !el.moreSheet.contains(ev.target) &&
    !el.tabMore.contains(ev.target)
  ) {
    closeMoreSheet();
  }
});

/* ---------------------------------------------------------------------
 * Init
 * --------------------------------------------------------------------- */

async function loadSystemStatus() {
  try {
    const ov = await fetchJson("/api/overview");
    renderFooter(ov.system);
  } catch {
    renderFooterOffline();
  }
}

setAccent("isa");
loadSystemStatus();
navigate();
