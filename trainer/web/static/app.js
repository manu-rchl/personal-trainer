"use strict";

/* Hub — Vanilla JS, kein Framework, kein Build-Step.
 * Sidebar-App-Shell mit Hash-Router und 6 Views:
 *   #/               Dashboard (Puls-Hero + Stat-Tiles + Termine/Workouts/Essen)
 *   #/chat/isa       Chat mit Isa
 *   #/chat/assistant Chat mit dem Assistant
 *   #/health         Health-Karten (Sparklines, Workouts/Woche, Protein)
 *   #/training       Fortschritt (Gewicht/e1RM), Volumen/Woche, PR-Highlights,
 *                    Session-Historie (gesamt, such-/filterbar)
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

function fmtDuration(min) {
  if (min === null || min === undefined || Number.isNaN(min)) return "–";
  const total = Math.round(min);
  const h = Math.floor(total / 60);
  const m = total % 60;
  return `${h}h ${String(m).padStart(2, "0")}`;
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
    subtitle: "Fortschritt, Volumen & gesamte Session-Historie",
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
  let exercises;
  try {
    [ov, health, exercises] = await Promise.all([
      fetchJson("/api/overview"),
      fetchJson("/api/health/overview?days=30"),
      fetchJson("/api/exercises"),
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

  // --- Kommandozentrale: domänenübergreifende Zusatzkarten ---
  grid.appendChild(workoutsPerWeekCard(health.workouts_per_week || []));
  grid.appendChild(prHighlightsCard(exercises, 7));

  const avgCard = document.createElement("section");
  avgCard.className = "card info-card stagger";
  const avg7 = ov.meals_7d_avg || {};
  const avgBody =
    (avg7.days_logged || 0) > 0
      ? `<div class="meal-summary">
          <div class="meal-protein">${fmtNum(avg7.protein_g, 0)}<span class="unit">g Protein</span></div>
          <div class="meal-meta">${fmtNum(avg7.calories_kcal, 0)} kcal · Ø über ${
          avg7.days_logged
        } Tag${avg7.days_logged === 1 ? "" : "e"}</div>
        </div>`
      : '<p class="empty-state">Letzte 7 Tage noch nichts geloggt.</p>';
  avgCard.innerHTML = `<h3 class="card-title">Ø 7-Tage-Ernährung</h3>${avgBody}`;
  grid.appendChild(avgCard);
}

/* ---------------------------------------------------------------------
 * View 2/3 — Chat pro Agent
 * --------------------------------------------------------------------- */

/* Markdown-lite: nur **bold** — die Agenten schreiben Telegram-Markdown.
   Erst escapen, dann ersetzen: kein HTML aus Nachrichteninhalten möglich. */
function renderInlineMarkdown(text) {
  return esc(text).replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
}

function renderMessage(msg) {
  const wrap = document.createElement("div");
  const isUser = msg.role === "user";
  wrap.className = "msg " + (isUser ? "msg-user" : "msg-agent");
  const textEl = document.createElement("div");
  textEl.className = "msg-text";
  textEl.innerHTML = renderInlineMarkdown(msg.content);
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

/* Vergleicht Ø der letzten 7 Tage gegen die 7 Tage davor — bewusst OHNE
   Gut/Schlecht-Farbcodierung, da "höher = besser" je nach Metrik variiert
   (z.B. Ruhepuls runter ist gut, Schlaf-Score runter ist schlecht). Nur ein
   neutraler Pfeil + Delta. */
function trendBadge(values, digits = 0, unit = "", formatValue = fmtNum) {
  const n = values.length;
  const recent = average(values.slice(Math.max(0, n - 7)));
  const prior = average(values.slice(Math.max(0, n - 14), Math.max(0, n - 7)));
  if (recent === null || prior === null) return "";

  const diff = recent - prior;
  const factor = Math.pow(10, digits);
  const rounded = Math.round(diff * factor) / factor;
  if (rounded === 0) {
    return '<span class="trend-badge trend-flat">→</span>';
  }
  const arrow = rounded > 0 ? "↑" : "↓";
  const cls = rounded > 0 ? "trend-up" : "trend-down";
  return `<span class="trend-badge ${cls}">${arrow} ${formatValue(Math.abs(rounded), digits)}${
    unit ? " " + unit : ""
  }</span>`;
}

function metricCard(title, values, todayValue, unit, digits = 0, formatValue = fmtNum) {
  const avg = average(values);
  const compare =
    avg === null
      ? "Ø 30 Tage: keine Daten"
      : `Ø 30 Tage: ${formatValue(avg, digits)}${unit ? " " + unit : ""}`;
  const trend = trendBadge(values, digits, unit, formatValue);

  const wrap = document.createElement("section");
  wrap.className = "card";
  wrap.innerHTML = `
    <h3 class="card-title">${esc(title)}</h3>
    <div class="card-value">${todayValue != null ? formatValue(todayValue, digits) : "–"}${
    todayValue != null && unit ? `<span class="unit">${esc(unit)}</span>` : ""
  }</div>
    <div class="card-compare">${compare} ${trend}</div>
    <svg class="card-sparkline" aria-hidden="true"></svg>
  `;
  const svgEl = wrap.querySelector(".card-sparkline");
  renderSparkline(svgEl, values, "sparkline-path");

  const present = values.filter((v) => v != null);
  const svgTitle = document.createElementNS("http://www.w3.org/2000/svg", "title");
  svgTitle.textContent = `${title}, letzte 30 Tage: Min ${
    present.length ? formatValue(Math.min(...present), digits) : "–"
  }, Max ${present.length ? formatValue(Math.max(...present), digits) : "–"}`;
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

function macroBarsCard(days, title) {
  const wrap = document.createElement("section");
  wrap.className = "card card-wide stagger";
  const width = 640;
  const height = 120;
  const padding = 24;
  const barAreaH = height - padding * 2 - 10;

  const totals = days.map((d) => (d.protein_g || 0) + (d.carbs_g || 0) + (d.fat_g || 0));
  const maxTotal = Math.max(...totals, 1);
  const gap = (width - padding * 2) / Math.max(days.length, 1);
  const barWidth = gap / 1.8;
  const scale = barAreaH / maxTotal;

  let bars = "";
  days.forEach((d, i) => {
    const x = padding + i * gap + (gap - barWidth) / 2;
    const dateLabel = new Date(d.date + "T00:00:00").toLocaleDateString("de-DE", {
      day: "2-digit",
      month: "2-digit",
    });
    const titleTxt = `${dateLabel}: P ${fmtNum(d.protein_g)} · C ${fmtNum(d.carbs_g)} · F ${fmtNum(
      d.fat_g
    )} g · ${fmtNum(d.calories_kcal, 0)} kcal`;
    let y = height - padding;
    [
      ["bar-protein", d.protein_g || 0],
      ["bar-carb", d.carbs_g || 0],
      ["bar-fat", d.fat_g || 0],
    ].forEach(([cls, grams]) => {
      const h = grams * scale;
      if (h <= 0) return;
      y -= h;
      bars += `<rect class="bar ${cls}" x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${barWidth.toFixed(
        1
      )}" height="${h.toFixed(1)}"><title>${esc(titleTxt)}</title></rect>`;
    });
  });

  const { paths: kcalPaths } = buildPathSegments(
    days.map((d) => (d.calories_kcal != null ? d.calories_kcal : null)),
    width,
    height,
    padding
  );
  const kcalPathsHtml = kcalPaths
    .map((d) => `<path class="calories-line" d="${d}"></path>`)
    .join("");

  const avgKcal = average(days.map((d) => d.calories_kcal));
  const avgProtein = average(days.map((d) => d.protein_g));

  wrap.innerHTML = `
    <div class="card-title-row">
      <h3 class="card-title">${esc(title)}</h3>
      <div class="macro-legend">
        <span class="macro-legend-item"><span class="macro-legend-dot bar-protein"></span>Protein</span>
        <span class="macro-legend-item"><span class="macro-legend-dot bar-carb"></span>Carbs</span>
        <span class="macro-legend-item"><span class="macro-legend-dot bar-fat"></span>Fett</span>
        <span class="macro-legend-item"><span class="macro-legend-line"></span>kcal</span>
      </div>
    </div>
    <svg class="bars-svg macro-bars-svg" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" role="img" aria-label="${esc(
    title
  )}, gestapelt Protein/Carbs/Fett mit Kalorien-Linie">
      ${bars}
      ${kcalPathsHtml}
    </svg>
    <div class="card-compare">Ø ${fmtNum(avgKcal, 0)} kcal · ${fmtNum(avgProtein, 0)} g Protein / Tag</div>
  `;
  wrap.querySelectorAll(".calories-line").forEach((p) => drawIn(p));
  return wrap;
}

function macroSplitBar(day) {
  const wrap = document.createElement("div");
  wrap.className = "macro-split-bar";
  if (!day) {
    wrap.innerHTML = '<span class="macro-split-empty">Keine Daten</span>';
    return wrap;
  }
  const pKcal = (day.protein_g || 0) * 4;
  const cKcal = (day.carbs_g || 0) * 4;
  const fKcal = (day.fat_g || 0) * 9;
  const total = pKcal + cKcal + fKcal;
  if (total <= 0) {
    wrap.innerHTML = '<span class="macro-split-empty">Keine Daten</span>';
    return wrap;
  }
  wrap.innerHTML = [
    ["bar-protein", "P", pKcal],
    ["bar-carb", "C", cKcal],
    ["bar-fat", "F", fKcal],
  ]
    .map(([cls, label, kcal]) => {
      const pct = Math.round((kcal / total) * 100);
      if (pct <= 0) return "";
      return `<div class="macro-split-segment ${cls}" style="flex-basis:${pct}%"><span>${esc(
        label
      )} ${pct}%</span></div>`;
    })
    .join("");
  return wrap;
}

function groupMealsByDay(meals) {
  const groups = [];
  let current = null;
  meals.forEach((m) => {
    const day = (m.ts || "").slice(0, 10);
    if (!current || current.day !== day) {
      current = { day, meals: [] };
      groups.push(current);
    }
    current.meals.push(m);
  });
  return groups;
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
    ["Aktivität", daily.map((d) => d.activity_score), today.activity_score, ""],
    ["Schritte", daily.map((d) => d.steps), today.steps, "Schritte"],
  ];
  tiles.forEach(([title, values, todayValue, unit]) => {
    const tile = metricCard(title, values, todayValue, unit, 0);
    tile.classList.add("stat-tile", "stagger");
    grid.appendChild(tile);
  });

  const sleepDurationTile = metricCard(
    "Schlafdauer",
    daily.map((d) => d.sleep_duration_min),
    today.sleep_duration_min,
    "",
    0,
    fmtDuration
  );
  sleepDurationTile.classList.add("stat-tile", "stagger");
  grid.appendChild(sleepDurationTile);

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

/* --- Fortschritts-Modul: Übungswahl + Gewichts-Verlaufsgraph --- */

function buildProgressCard(exercises) {
  const card = document.createElement("section");
  card.className = "card progress-card stagger";

  if (!exercises || exercises.length === 0) {
    card.innerHTML = `
      <h3 class="card-title">Fortschritt</h3>
      <p class="empty-state">Noch keine Übungen geloggt — sag's Isa oder logge dein erstes Workout.</p>
    `;
    return card;
  }

  card.innerHTML = `
    <div class="progress-head">
      <h3 class="card-title">Fortschritt</h3>
      <div class="progress-head-controls">
        <div class="metric-toggle" role="group" aria-label="Anzeige wählen">
          <button type="button" class="metric-toggle-btn active" data-metric="weight">Gewicht</button>
          <button type="button" class="metric-toggle-btn" data-metric="e1rm">e1RM</button>
        </div>
        <div class="select-wrap">
          <select class="exercise-select" aria-label="Übung wählen"></select>
          <svg class="select-chevron" viewBox="0 0 24 24" fill="none" aria-hidden="true">
            <path d="M6 9l6 6 6-6" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" />
          </svg>
        </div>
      </div>
    </div>
    <div class="progress-readouts">
      <div class="readout">
        <span class="readout-value progress-readout-value" data-field="current">–</span>
        <span class="readout-label">Aktuell</span>
      </div>
      <div class="readout">
        <span class="readout-value progress-readout-value" data-field="max">–</span>
        <span class="readout-label">Max</span>
      </div>
      <div class="readout">
        <span class="readout-value progress-readout-value" data-field="sessions">–</span>
        <span class="readout-label">Sessions</span>
      </div>
    </div>
    <div class="progress-graph-wrap">
      <svg class="progress-svg" viewBox="0 0 640 200" role="img" aria-label="Gewichtsverlauf der gewählten Übung"></svg>
      <p class="progress-hint" hidden></p>
    </div>
    <div class="progress-table-wrap" hidden>
      <h4 class="progress-table-title">Sätze im Verlauf</h4>
      <div class="progress-table" role="list" aria-label="Sätze im Verlauf, neueste zuerst"></div>
    </div>
  `;

  const selectEl = card.querySelector(".exercise-select");
  const metricButtons = card.querySelectorAll(".metric-toggle-btn");
  metricButtons.forEach((btn) => {
    btn.addEventListener("click", () => {
      if (btn.classList.contains("active")) return;
      metricButtons.forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      card.dataset.metric = btn.dataset.metric;
      if (card._lastProgress) renderProgressGraph(card, card._lastProgress);
    });
  });

  // Nach Trainingseinheit (category) gruppieren: Gruppen mit den meisten
  // Übungen zuerst, "Sonstige" immer zuletzt. Innerhalb einer Gruppe nach
  // sessions DESC (Eingabe ist bereits global sessions-DESC sortiert, hier
  // nochmal explizit für Robustheit).
  const groups = new Map();
  exercises.forEach((ex) => {
    const cat = (ex.category && String(ex.category).trim()) || "Sonstige";
    if (!groups.has(cat)) groups.set(cat, []);
    groups.get(cat).push(ex);
  });

  const groupEntries = Array.from(groups.entries());
  groupEntries.forEach(([, list]) => list.sort((a, b) => b.sessions - a.sessions));
  groupEntries.sort(([catA, listA], [catB, listB]) => {
    if (catA === "Sonstige" && catB !== "Sonstige") return 1;
    if (catB === "Sonstige" && catA !== "Sonstige") return -1;
    return listB.length - listA.length;
  });

  groupEntries.forEach(([cat, list]) => {
    const optgroup = document.createElement("optgroup");
    optgroup.label = cat;
    list.forEach((ex) => {
      const opt = document.createElement("option");
      opt.value = ex.name;
      opt.textContent = `${ex.name} · ${ex.sessions} Session${ex.sessions === 1 ? "" : "en"}`;
      optgroup.appendChild(opt);
    });
    selectEl.appendChild(optgroup);
  });

  // Default bleibt die insgesamt häufigste Übung — nicht zwingend die erste
  // Option im DOM, sobald optgroups die Reihenfolge verändern.
  selectEl.value = exercises[0].name;

  async function loadProgress(name) {
    let progress;
    try {
      progress = await fetchJson(`/api/exercise/progress?name=${encodeURIComponent(name)}`);
    } catch {
      return;
    }
    if (!document.body.contains(card)) return; // View gewechselt
    card._lastProgress = progress;
    renderProgressGraph(card, progress);
  }

  selectEl.addEventListener("change", () => loadProgress(selectEl.value));
  loadProgress(exercises[0].name);

  return card;
}

function renderProgressGraph(card, progress) {
  const points = progress.points || [];
  const currentEl = card.querySelector('[data-field="current"]');
  const maxEl = card.querySelector('[data-field="max"]');
  const sessionsEl = card.querySelector('[data-field="sessions"]');
  const svg = card.querySelector(".progress-svg");
  const hint = card.querySelector(".progress-hint");
  const tableWrap = card.querySelector(".progress-table-wrap");

  sessionsEl.textContent = String(points.length);

  if (points.length === 0) {
    currentEl.textContent = "–";
    maxEl.textContent = "–";
    svg.innerHTML = "";
    hint.hidden = false;
    hint.textContent = "Noch keine Daten für diese Übung.";
    if (tableWrap) tableWrap.hidden = true;
    return;
  }

  const metricField = card.dataset.metric === "e1rm" ? "est_1rm" : "top_weight_kg";
  const weights = points.map((p) => p[metricField]);
  const current = points[points.length - 1][metricField];
  const max = Math.max(...weights);
  currentEl.textContent = `${fmtNum(current, 1)} kg`;
  maxEl.textContent = `${fmtNum(max, 1)} kg`;

  const width = 640;
  const height = 200;
  const padding = 28;
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.innerHTML = "";

  const lo = Math.min(...weights);
  const hi = Math.max(...weights);
  const range = hi - lo || 1;
  const n = points.length;
  const stepX = n > 1 ? (width - padding * 2) / (n - 1) : 0;
  const usableH = height - padding * 2;

  const coords = points.map((p, i) => ({
    x: padding + i * stepX,
    y: padding + (1 - (p[metricField] - lo) / range) * usableH,
    p,
  }));

  // Dezente horizontale Gridlines
  const gridGroup = document.createElementNS("http://www.w3.org/2000/svg", "g");
  gridGroup.setAttribute("class", "progress-grid");
  [0, 0.5, 1].forEach((f) => {
    const y = padding + f * usableH;
    const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
    line.setAttribute("x1", padding.toFixed(2));
    line.setAttribute("x2", (width - padding).toFixed(2));
    line.setAttribute("y1", y.toFixed(2));
    line.setAttribute("y2", y.toFixed(2));
    gridGroup.appendChild(line);
  });
  svg.appendChild(gridGroup);

  if (n > 1) {
    const d = coords
      .map((c, i) => `${i === 0 ? "M" : "L"}${c.x.toFixed(2)},${c.y.toFixed(2)}`)
      .join(" ");
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", d);
    path.setAttribute("class", "progress-line");
    svg.appendChild(path);
    drawIn(path);
  }

  coords.forEach((c) => {
    // Gruppe aus unsichtbarer, größerer Trefferfläche + sichtbarem Punkt —
    // der r=3.5-Punkt allein wäre auf Touch zu klein zum Antippen.
    const group = document.createElementNS("http://www.w3.org/2000/svg", "g");
    group.setAttribute("class", "progress-point");

    const hit = document.createElementNS("http://www.w3.org/2000/svg", "circle");
    hit.setAttribute("cx", c.x.toFixed(2));
    hit.setAttribute("cy", c.y.toFixed(2));
    hit.setAttribute("r", "12");
    hit.setAttribute("class", "progress-dot-hit");
    group.appendChild(hit);

    const dot = document.createElementNS("http://www.w3.org/2000/svg", "circle");
    dot.setAttribute("cx", c.x.toFixed(2));
    dot.setAttribute("cy", c.y.toFixed(2));
    dot.setAttribute("r", "3.5");
    dot.setAttribute("class", "progress-dot");
    const title = document.createElementNS("http://www.w3.org/2000/svg", "title");
    const repsLabel = c.p.top_reps != null ? c.p.top_reps : "–";
    title.textContent = `${fmtDate(c.p.date)} · ${fmtNum(c.p.top_weight_kg, 1)} kg × ${repsLabel} (e1RM ${fmtNum(
      c.p.est_1rm,
      1
    )})`;
    dot.appendChild(title);
    group.appendChild(dot);

    group.addEventListener("click", () => highlightProgressRow(card, c.p.date));
    svg.appendChild(group);
  });

  // Achsen: Y min/max, X erstes/letztes Datum — minimalistisch, kein Overkill
  const yMinLabel = document.createElementNS("http://www.w3.org/2000/svg", "text");
  yMinLabel.setAttribute("x", "4");
  yMinLabel.setAttribute("y", (height - padding + 4).toFixed(2));
  yMinLabel.setAttribute("class", "progress-axis-label");
  yMinLabel.textContent = `${fmtNum(lo, 0)} kg`;
  svg.appendChild(yMinLabel);

  const yMaxLabel = document.createElementNS("http://www.w3.org/2000/svg", "text");
  yMaxLabel.setAttribute("x", "4");
  yMaxLabel.setAttribute("y", (padding - 8).toFixed(2));
  yMaxLabel.setAttribute("class", "progress-axis-label");
  yMaxLabel.textContent = `${fmtNum(hi, 0)} kg`;
  svg.appendChild(yMaxLabel);

  const xFirstLabel = document.createElementNS("http://www.w3.org/2000/svg", "text");
  xFirstLabel.setAttribute("x", padding.toFixed(2));
  xFirstLabel.setAttribute("y", (height - 6).toFixed(2));
  xFirstLabel.setAttribute("class", "progress-axis-label");
  xFirstLabel.textContent = fmtDate(points[0].date);
  svg.appendChild(xFirstLabel);

  const xLastLabel = document.createElementNS("http://www.w3.org/2000/svg", "text");
  xLastLabel.setAttribute("x", (width - padding).toFixed(2));
  xLastLabel.setAttribute("y", (height - 6).toFixed(2));
  xLastLabel.setAttribute("class", "progress-axis-label progress-axis-label-end");
  xLastLabel.textContent = fmtDate(points[points.length - 1].date);
  svg.appendChild(xLastLabel);

  if (n === 1) {
    hint.hidden = false;
    hint.textContent = "Mehr Sessions = Trendlinie";
  } else {
    hint.hidden = true;
  }

  renderProgressTable(card, points);
}

/* Kompakte Liste unter dem Graphen — neueste Session zuerst. Delta bezieht
   sich immer auf die zeitlich VORHERIGE (ältere) Session, auch wenn die
   Liste selbst rückwärts sortiert ist. */
function renderProgressTable(card, points) {
  const wrap = card.querySelector(".progress-table-wrap");
  const tableEl = card.querySelector(".progress-table");
  if (!wrap || !tableEl) return;

  tableEl.innerHTML = "";

  if (!points.length) {
    wrap.hidden = true;
    return;
  }
  wrap.hidden = false;

  for (let i = points.length - 1; i >= 0; i--) {
    const p = points[i];
    const prev = i > 0 ? points[i - 1] : null;

    const row = document.createElement("div");
    row.className = "progress-table-row";
    row.setAttribute("role", "listitem");
    row.dataset.date = p.date;

    let deltaHtml = '<span class="progress-delta progress-delta-none">–</span>';
    if (prev && prev.top_weight_kg != null && p.top_weight_kg != null) {
      const diff = Math.round((p.top_weight_kg - prev.top_weight_kg) * 10) / 10;
      if (diff > 0) {
        deltaHtml = `<span class="progress-delta progress-delta-up">↑ +${fmtNum(diff, 1)} kg</span>`;
      } else if (diff < 0) {
        deltaHtml = `<span class="progress-delta progress-delta-down">↓ −${fmtNum(
          Math.abs(diff),
          1
        )} kg</span>`;
      } else {
        deltaHtml = '<span class="progress-delta progress-delta-flat">→</span>';
      }
    }

    const repsLabel = p.top_reps != null ? p.top_reps : "–";
    row.innerHTML = `
      <span class="progress-table-date">${esc(fmtDate(p.date))}</span>
      <span class="progress-table-top">${fmtNum(p.top_weight_kg, 1)} kg × ${esc(String(repsLabel))}</span>
      <span class="progress-table-sets">${p.set_count != null ? p.set_count : "–"} Sätze</span>
      <span class="progress-table-e1rm">e1RM ${fmtNum(p.est_1rm, 1)}</span>
      ${deltaHtml}
    `;
    tableEl.appendChild(row);
  }
}

/* Tap auf einen Graph-Punkt hebt die zugehörige Tabellenzeile kurz hervor. */
function highlightProgressRow(card, dateIso) {
  const rows = card.querySelectorAll(".progress-table-row");
  rows.forEach((r) => {
    r.classList.toggle("highlighted", r.dataset.date === dateIso);
  });
}

function volumeBarsCard(volumePerWeek) {
  const wrap = document.createElement("section");
  wrap.className = "card card-wide stagger";
  const width = 640;
  const height = 92;
  const padding = 24;
  const maxVolume = Math.max(...volumePerWeek.map((w) => w.volume_kg), 1);
  const gap = (width - padding * 2) / Math.max(volumePerWeek.length, 1);
  const barWidth = gap / 1.8;

  let bars = "";
  volumePerWeek.forEach((w, i) => {
    const barH = (w.volume_kg / maxVolume) * (height - padding * 2 - 16);
    const x = padding + i * gap + (gap - barWidth) / 2;
    const y = height - padding - barH;
    const weekLabel = `KW ${isoWeekLabel(w.week)}`;
    const titleTxt = `${weekLabel}: ${fmtNum(w.volume_kg, 0)} kg (${w.set_count} Sätze, ${
      w.workout_count
    } Workout${w.workout_count === 1 ? "" : "s"})`;
    bars += `<rect class="bar" x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${barWidth.toFixed(
      1
    )}" height="${Math.max(barH, 1).toFixed(1)}" rx="2"><title>${esc(titleTxt)}</title></rect>`;
  });

  const avgVolume = average(volumePerWeek.map((w) => w.volume_kg));

  wrap.innerHTML = `
    <h3 class="card-title">Trainingsvolumen pro Woche</h3>
    <svg class="bars-svg" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" role="img" aria-label="Trainingsvolumen pro Woche, letzte ${
    volumePerWeek.length
  } Wochen">
      ${bars}
    </svg>
    <div class="card-compare">Ø ${fmtNum(avgVolume, 0)} kg / Woche</div>
  `;
  return wrap;
}

function prHighlightsCard(exercises, days = 30) {
  const wrap = document.createElement("section");
  wrap.className = "card info-card stagger";

  const cutoff = new Date();
  cutoff.setDate(cutoff.getDate() - days);
  const cutoffIso = cutoff.toISOString().slice(0, 10);

  const prs = (exercises || [])
    .filter((ex) => ex.pr_date && ex.pr_date >= cutoffIso)
    .sort((a, b) => (a.pr_date < b.pr_date ? 1 : -1));

  const rows = prs.length
    ? `<ul class="info-list">${prs
        .map(
          (ex) => `<li class="info-row">
            <span class="info-row-time">${esc(fmtDate(ex.pr_date))}</span>
            <span class="info-row-main">
              <span class="info-row-title">${esc(ex.name)}</span>
              <span class="info-row-sub">${fmtNum(ex.pr_weight_kg, 1)} kg · e1RM ${fmtNum(
              ex.pr_est_1rm,
              1
            )}</span>
            </span>
          </li>`
        )
        .join("")}</ul>`
    : `<p class="empty-state">Keine neuen PRs in den letzten ${days} Tagen.</p>`;

  wrap.innerHTML = `<h3 class="card-title">PR-Highlights</h3>${rows}`;
  return wrap;
}

async function renderTraining(panel) {
  panel.innerHTML = '<p class="empty-state">Lade Workouts …</p>';
  let base;
  let exercises;
  let volume;
  try {
    [base, exercises, volume] = await Promise.all([
      fetchJson("/api/workouts?days=1825"),
      fetchJson("/api/exercises"),
      fetchJson("/api/training/volume?weeks=12"),
    ]);
  } catch {
    panel.innerHTML = '<p class="empty-state">Workouts konnten nicht geladen werden.</p>';
    return;
  }
  if (state.route !== "#/training") return;

  panel.innerHTML = "";
  panel.appendChild(buildProgressCard(exercises));

  const allWorkouts = base.workouts || [];
  if (allWorkouts.length === 0) {
    const empty = document.createElement("p");
    empty.className = "empty-state";
    empty.innerHTML = `Noch nichts geloggt.
      <span class="empty-state-hint">Sag Isa im Chat, was du trainiert hast — oder importiere ein Strong-CSV.</span>`;
    panel.appendChild(empty);
    return;
  }

  panel.appendChild(volumeBarsCard(volume.volume_per_week || []));
  panel.appendChild(prHighlightsCard(exercises, 30));

  const heading = document.createElement("h3");
  heading.className = "section-heading stagger";
  heading.textContent = "Verlauf";
  panel.appendChild(heading);

  // Filterleiste: Type-Chips und Freitext filtern rein clientseitig gegen die
  // bereits geladene Liste (raw-Feld-Matches, keine Roundtrips pro Tastendruck).
  // Der Übungs-Filter braucht dagegen die serverseitige `canonicalize()`-
  // Heuristik (Strong-/Hevy-Namensvarianten zusammenführen) — die wird hier
  // NICHT dupliziert, stattdessen holt ein Select-Change gezielt
  // `/api/workouts?exercise=...` nach (selten genug, dass ein Roundtrip okay ist).
  const filterBar = document.createElement("div");
  filterBar.className = "filter-bar stagger";
  filterBar.innerHTML = `
    <input type="search" class="filter-input" placeholder="Suche (Übung, Notiz, Typ) …" aria-label="Workouts durchsuchen" />
    <div class="filter-chips" role="group" aria-label="Nach Trainingstyp filtern"></div>
    <div class="select-wrap">
      <select class="exercise-select filter-exercise-select" aria-label="Nach Übung filtern">
        <option value="">Alle Übungen</option>
      </select>
      <svg class="select-chevron" viewBox="0 0 24 24" fill="none" aria-hidden="true">
        <path d="M6 9l6 6 6-6" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" />
      </svg>
    </div>
    <button type="button" class="filter-reset">Filter zurücksetzen</button>
    <span class="filter-count"></span>
  `;
  panel.appendChild(filterBar);

  const chipsEl = filterBar.querySelector(".filter-chips");
  (base.facets?.types || []).forEach((t) => {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "filter-chip";
    chip.textContent = t;
    chip.dataset.type = t;
    chipsEl.appendChild(chip);
  });

  const exerciseSelectEl = filterBar.querySelector(".filter-exercise-select");
  exercises.forEach((ex) => {
    const opt = document.createElement("option");
    opt.value = ex.name;
    opt.textContent = ex.name;
    exerciseSelectEl.appendChild(opt);
  });

  const searchEl = filterBar.querySelector(".filter-input");
  const countEl = filterBar.querySelector(".filter-count");
  const resetBtn = filterBar.querySelector(".filter-reset");

  const list = document.createElement("div");
  list.className = "workout-list";
  panel.appendChild(list);

  const filterState = { types: new Set(), q: "", workouts: allWorkouts };

  function applyFilters() {
    const q = filterState.q.trim().toLowerCase();
    const filtered = filterState.workouts.filter((w) => {
      if (filterState.types.size && !filterState.types.has(w.type)) return false;
      if (!q) return true;
      const inType = (w.type || "").toLowerCase().includes(q);
      const inNotes = (w.notes || "").toLowerCase().includes(q);
      const inSets = (w.sets || []).some((s) => (s.exercise || "").toLowerCase().includes(q));
      return inType || inNotes || inSets;
    });

    list.innerHTML = "";
    if (filtered.length === 0) {
      const empty = document.createElement("p");
      empty.className = "empty-state";
      empty.textContent = "Keine Workouts passen zu diesem Filter.";
      list.appendChild(empty);
    } else {
      filtered.forEach((w) => list.appendChild(workoutRow(w)));
    }
    countEl.textContent = `${filtered.length} von ${filterState.workouts.length} Sessions`;
  }

  async function setExerciseFilter(name) {
    if (!name) {
      filterState.workouts = allWorkouts;
      applyFilters();
      return;
    }
    countEl.textContent = "Lade …";
    try {
      const res = await fetchJson(`/api/workouts?days=1825&exercise=${encodeURIComponent(name)}`);
      filterState.workouts = res.workouts || [];
    } catch {
      filterState.workouts = [];
    }
    applyFilters();
  }

  chipsEl.addEventListener("click", (ev) => {
    const chip = ev.target.closest(".filter-chip");
    if (!chip) return;
    const t = chip.dataset.type;
    if (filterState.types.has(t)) {
      filterState.types.delete(t);
      chip.classList.remove("active");
    } else {
      filterState.types.add(t);
      chip.classList.add("active");
    }
    applyFilters();
  });

  searchEl.addEventListener("input", () => {
    filterState.q = searchEl.value;
    applyFilters();
  });

  exerciseSelectEl.addEventListener("change", () => setExerciseFilter(exerciseSelectEl.value));

  resetBtn.addEventListener("click", () => {
    filterState.types.clear();
    filterState.q = "";
    searchEl.value = "";
    exerciseSelectEl.value = "";
    chipsEl.querySelectorAll(".filter-chip.active").forEach((c) => c.classList.remove("active"));
    filterState.workouts = allWorkouts;
    applyFilters();
  });

  applyFilters();
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

  // Tages-Summen zu 30 durchgehenden Tagen auffüllen (fehlender Tag = 0)
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
    days.push({
      date: iso,
      protein_g: t ? t.protein_g_sum || 0 : 0,
      carbs_g: t ? t.carbs_g_sum || 0 : 0,
      fat_g: t ? t.fat_g_sum || 0 : 0,
      calories_kcal: t ? t.calories_kcal_sum || 0 : 0,
    });
  }
  panel.appendChild(macroBarsCard(days, "Makros, letzte 30 Tage"));

  const latestTotal = (data.daily_totals || [])[0];
  const splitCard = document.createElement("section");
  splitCard.className = "card stagger";
  splitCard.innerHTML = `
    <h3 class="card-title">Makro-Verteilung</h3>
    <div class="card-compare">${
      latestTotal ? esc(fmtDate(latestTotal.day)) + " — letzter geloggter Tag" : "Noch keine Daten"
    }</div>
    <div class="macro-split-wrap"></div>
  `;
  splitCard.querySelector(".macro-split-wrap").appendChild(
    macroSplitBar(
      latestTotal
        ? {
            protein_g: latestTotal.protein_g_sum,
            carbs_g: latestTotal.carbs_g_sum,
            fat_g: latestTotal.fat_g_sum,
          }
        : null
    )
  );
  panel.appendChild(splitCard);

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

  groupMealsByDay(meals).forEach((group) => {
    const kcalSum = group.meals.reduce((a, m) => a + (m.calories_kcal || 0), 0);
    const heading = document.createElement("h4");
    heading.className = "day-group-heading";
    heading.textContent = `${fmtDate(group.day)} — ${group.meals.length} Mahlzeit${
      group.meals.length === 1 ? "" : "en"
    }, ${fmtNum(kcalSum, 0)} kcal`;
    list.appendChild(heading);

    group.meals.forEach((meal) => {
      const row = document.createElement("article");
      row.className = "meal-row stagger";
      const macros = `P ${fmtNum(meal.protein_g, 0)} · C ${fmtNum(meal.carbs_g, 0)} · F ${fmtNum(
        meal.fat_g,
        0
      )} · ${fmtNum(meal.calories_kcal, 0)} kcal`;
      row.innerHTML = `
        <span class="meal-row-time">${esc(fmtTime(meal.ts))}</span>
        <span class="meal-row-desc">${esc(meal.description || "Mahlzeit")}</span>
        <span class="meal-row-macros">${esc(macros)}</span>
      `;
      list.appendChild(row);
    });
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
