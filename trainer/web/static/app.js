"use strict";

/* Hub — Vanilla JS, kein Framework, kein Build-Step.
 * Sidebar-App-Shell (Bento-Design, Light/Dark-Theme) mit Hash-Router und
 * 5 Views:
 *   #/               Overview (Ring-Hero + Quick-Stats + Stat-Tiles + Termine/Essen)
 *   #/chat/isa       Chat mit Isa
 *   #/health        Health-Karten (Sparklines, Schlaf-Heatmap, Workouts/Woche)
 *   #/training       Übungsliste, Fortschritt (Gewicht/e1RM), Volumen/Woche,
 *                    PR-Highlights, Session-Historie (such-/filterbar)
 *   #/ernaehrung     Makro-Donut (heute), Protein/Kalorien (14 Tage), Mahlzeiten
 * Es scrollt ausschließlich der View-Container bzw. die Chat-Nachrichtenliste,
 * nie der Body. */

const AGENT_META = {
  isa: { display: "Isa", subtitle: "Fitness & Ernährung" },
};
const EMPTY_CHAT_HINTS = {
  isa: "Noch keine Nachrichten — frag Isa nach deinem Schlaf.",
};

const REDUCE_MOTION = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

const el = {
  container: document.getElementById("view-container"),
  eyebrow: document.getElementById("view-eyebrow"),
  title: document.getElementById("view-title"),
  subtitle: document.getElementById("view-subtitle"),
  statusDot: document.getElementById("status-dot"),
  footerStatusText: document.getElementById("footer-status-text"),
  themeToggle: document.getElementById("theme-toggle"),
  themeToggleLabel: document.getElementById("theme-toggle-label"),
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

function todayIso() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(
    d.getDate()
  ).padStart(2, "0")}`;
}

/* ---------------------------------------------------------------------
 * API-Zugriff: Bearer-Token (WEB_AUTH_TOKEN aus .env) wird einmal per
 * prompt() abgefragt und in localStorage gehalten. Bei 401 wird er
 * verworfen und neu abgefragt.
 * --------------------------------------------------------------------- */

const TOKEN_KEY = "trainer.webAuthToken";

function readToken() {
  try {
    return localStorage.getItem(TOKEN_KEY) || "";
  } catch {
    return "";
  }
}

function askForToken(message) {
  const token = window.prompt(message || "Zugangs-Token für das Dashboard (WEB_AUTH_TOKEN aus .env):");
  if (!token) return "";
  try {
    localStorage.setItem(TOKEN_KEY, token.trim());
  } catch {
    /* privater Modus o.ä. — dann eben pro Seitenaufruf */
  }
  return token.trim();
}

async function apiFetch(url, options = {}) {
  let token = readToken() || askForToken();
  for (let attempt = 0; attempt < 2; attempt++) {
    const headers = { ...(options.headers || {}), Authorization: `Bearer ${token}` };
    const res = await fetch(url, { ...options, headers });
    if (res.status !== 401) return res;
    try {
      localStorage.removeItem(TOKEN_KEY);
    } catch {
      /* ignore */
    }
    token = askForToken("Token falsch oder abgelaufen — bitte erneut eingeben:");
    if (!token) return res;
  }
  return fetch(url, { ...options, headers: { ...(options.headers || {}), Authorization: `Bearer ${token}` } });
}

async function fetchJson(url) {
  if (state.cache[url]) return state.cache[url];
  const res = await apiFetch(url);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const data = await res.json();
  state.cache[url] = data;
  return data;
}

function invalidateCache() {
  state.cache = {};
}

/* ---------------------------------------------------------------------
 * Theme (Light/Dark) — Attribut auf <html>, persistiert in localStorage.
 * Initialer Wert wird bereits inline im <head> von index.html gesetzt,
 * damit kein falsches Theme aufblitzt; hier nur Toggle + UI-Sync.
 * --------------------------------------------------------------------- */

function currentTheme() {
  return document.documentElement.dataset.theme === "light" ? "light" : "dark";
}

function applyThemeUi(theme) {
  el.themeToggle.setAttribute("aria-checked", theme === "light" ? "true" : "false");
  el.themeToggleLabel.textContent = theme === "light" ? "Helles Design" : "Dunkles Design";
}

function setTheme(theme) {
  document.documentElement.dataset.theme = theme;
  try {
    localStorage.setItem("hub-theme", theme);
  } catch {
    /* Storage evtl. blockiert (privates Fenster) — Theme gilt dann nur für die Session. */
  }
  applyThemeUi(theme);
}

el.themeToggle.addEventListener("click", () => {
  setTheme(currentTheme() === "light" ? "dark" : "light");
});

/* ---------------------------------------------------------------------
 * SVG-Chart-Primitives
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

  const paths = segments.map((seg) => ({
    line: seg.map((p, i) => `${i === 0 ? "M" : "L"}${p[0].toFixed(2)},${p[1].toFixed(2)}`).join(" "),
    area: `${seg
      .map((p, i) => `${i === 0 ? "M" : "L"}${p[0].toFixed(2)},${p[1].toFixed(2)}`)
      .join(" ")} L${seg[seg.length - 1][0].toFixed(2)},${(height - padding).toFixed(
      2
    )} L${seg[0][0].toFixed(2)},${(height - padding).toFixed(2)} Z`,
  }));

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

let sparklineIdSeq = 0;

function renderSparkline(svgEl, values) {
  svgEl.innerHTML = "";
  const width = 280;
  const height = 56;
  svgEl.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svgEl.setAttribute("preserveAspectRatio", "none");
  const { paths } = buildPathSegments(values, width, height, 6);
  const gradId = `spark-grad-${sparklineIdSeq++}`;

  const defs = document.createElementNS("http://www.w3.org/2000/svg", "defs");
  const grad = document.createElementNS("http://www.w3.org/2000/svg", "linearGradient");
  grad.setAttribute("id", gradId);
  grad.setAttribute("x1", "0");
  grad.setAttribute("y1", "0");
  grad.setAttribute("x2", "0");
  grad.setAttribute("y2", "1");
  const stop1 = document.createElementNS("http://www.w3.org/2000/svg", "stop");
  stop1.setAttribute("offset", "0%");
  stop1.setAttribute("stop-color", "var(--accent)");
  stop1.setAttribute("stop-opacity", "0.28");
  const stop2 = document.createElementNS("http://www.w3.org/2000/svg", "stop");
  stop2.setAttribute("offset", "100%");
  stop2.setAttribute("stop-color", "var(--accent)");
  stop2.setAttribute("stop-opacity", "0");
  grad.appendChild(stop1);
  grad.appendChild(stop2);
  defs.appendChild(grad);
  svgEl.appendChild(defs);

  paths.forEach(({ line, area }) => {
    const areaPath = document.createElementNS("http://www.w3.org/2000/svg", "path");
    areaPath.setAttribute("d", area);
    areaPath.setAttribute("class", "sparkline-area");
    areaPath.setAttribute("fill", `url(#${gradId})`);
    svgEl.appendChild(areaPath);

    const linePath = document.createElementNS("http://www.w3.org/2000/svg", "path");
    linePath.setAttribute("d", line);
    linePath.setAttribute("class", "sparkline-path");
    svgEl.appendChild(linePath);
    drawIn(linePath);
  });
}

function polarPoint(cx, cy, r, angleDeg) {
  const a = ((angleDeg - 90) * Math.PI) / 180;
  return [cx + r * Math.cos(a), cy + r * Math.sin(a)];
}

function arcPath(cx, cy, r, pct) {
  const end = Math.min(pct, 0.9995) * 360;
  const [sx, sy] = polarPoint(cx, cy, r, 0);
  const [ex, ey] = polarPoint(cx, cy, r, end);
  const large = end > 180 ? 1 : 0;
  return `M${sx.toFixed(2)},${sy.toFixed(2)} A${r},${r} 0 ${large} 1 ${ex.toFixed(2)},${ey.toFixed(2)}`;
}

/* Ring-Cluster für den Overview-Hero: bis zu 3 konzentrische Ringe, Wert
   jeweils als Anteil von 100 (Oura-Scores). null-Werte zeigen nur die
   Track-Linie (kein Fortschrittsbogen). */
function buildRingCluster(rings) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 200 200");
  svg.setAttribute("class", "ring-svg");
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", rings.map((r) => `${r.label}: ${r.value ?? "keine Daten"}`).join(", "));

  rings.forEach((r) => {
    const track = document.createElementNS("http://www.w3.org/2000/svg", "circle");
    track.setAttribute("cx", "100");
    track.setAttribute("cy", "100");
    track.setAttribute("r", String(r.r));
    track.setAttribute("class", "ring-track");
    track.setAttribute("stroke-width", "10");
    svg.appendChild(track);

    if (r.value != null && r.value > 0) {
      const arc = document.createElementNS("http://www.w3.org/2000/svg", "path");
      arc.setAttribute("d", arcPath(100, 100, r.r, r.value / 100));
      arc.setAttribute("class", "ring-arc");
      arc.setAttribute("stroke", r.color);
      arc.setAttribute("stroke-width", "10");
      svg.appendChild(arc);
    }
  });

  return svg;
}

/* Makro-Donut (Protein/Carbs/Fett). Butt-Cap-Segmente mit 0.4°-Lücke,
   im Uhrzeigersinn ab 12 Uhr. Gibt null zurück, wenn keine Daten da sind. */
function buildMacroDonut(protein, carbs, fat, size = 110) {
  const total = (protein || 0) + (carbs || 0) + (fat || 0);
  if (total <= 0) return null;

  const segs = [
    ["var(--ok)", protein || 0],
    ["var(--carb)", carbs || 0],
    ["var(--fat)", fat || 0],
  ];
  const r = size / 2 - 9;
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", `0 0 ${size} ${size}`);
  svg.setAttribute("style", `width:${size}px;height:${size}px;flex-shrink:0`);

  const track = document.createElementNS("http://www.w3.org/2000/svg", "circle");
  track.setAttribute("cx", String(size / 2));
  track.setAttribute("cy", String(size / 2));
  track.setAttribute("r", String(r));
  track.setAttribute("fill", "none");
  track.setAttribute("stroke", "var(--border)");
  track.setAttribute("stroke-width", "14");
  svg.appendChild(track);

  let acc = 0;
  segs.forEach(([color, val]) => {
    if (val <= 0) return;
    const pct = val / total;
    const from = acc;
    const to = acc + pct;
    acc += pct;
    const a1 = from * 360;
    const a2 = to * 360 - (to < 1 ? 0.4 : 0);
    const [sx, sy] = polarPoint(size / 2, size / 2, r, a1);
    const [ex, ey] = polarPoint(size / 2, size / 2, r, Math.max(a2, a1));
    const large = a2 - a1 > 180 ? 1 : 0;
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute(
      "d",
      `M${sx.toFixed(2)},${sy.toFixed(2)} A${r},${r} 0 ${large} 1 ${ex.toFixed(2)},${ey.toFixed(2)}`
    );
    path.setAttribute("fill", "none");
    path.setAttribute("stroke", color);
    path.setAttribute("stroke-width", "14");
    svg.appendChild(path);
  });

  return svg;
}

function macroLegend(protein, carbs, fat) {
  const wrap = document.createElement("div");
  wrap.className = "macro-legend2";
  wrap.innerHTML = `
    <div class="ml-row"><span class="ml-dot" style="background:var(--ok)"></span>Protein<span class="ml-val">${fmtNum(
      protein,
      0
    )}g</span></div>
    <div class="ml-row"><span class="ml-dot" style="background:var(--carb)"></span>Carbs<span class="ml-val">${fmtNum(
      carbs,
      0
    )}g</span></div>
    <div class="ml-row"><span class="ml-dot" style="background:var(--fat)"></span>Fett<span class="ml-val">${fmtNum(
      fat,
      0
    )}g</span></div>
  `;
  return wrap;
}

/* ---------------------------------------------------------------------
 * Router
 * --------------------------------------------------------------------- */

const ROUTES = {
  "#/": {
    eyebrow: "00 · Fitness",
    title: "Overview",
    subtitle: "Dein Tag auf einen Blick",
    render: renderDashboard,
  },
  "#/chat/isa": {
    eyebrow: "Agent · Isa",
    title: "Isa",
    subtitle: AGENT_META.isa.subtitle,
    render: (panel) => renderChat(panel, "isa"),
  },
  "#/health": {
    eyebrow: "01 · Fitness",
    title: "Health",
    subtitle: "Oura & Trends, letzte 30 Tage",
    render: renderHealth,
  },
  "#/training": {
    eyebrow: "02 · Fitness",
    title: "Training",
    subtitle: "Fortschritt, Volumen & Session-Historie",
    render: renderTraining,
  },
  "#/ernaehrung": {
    eyebrow: "03 · Fitness",
    title: "Ernährung",
    subtitle: "Mahlzeiten & Makros, letzte 30 Tage",
    render: renderErnaehrung,
  },
};

function normalizeHash(hash) {
  return ROUTES[hash] ? hash : "#/";
}

function updateActiveNav(route) {
  document
    .querySelectorAll(".section-item, .agent-item, .tab-item, .more-sheet-item")
    .forEach((item) => {
      item.classList.toggle("active", item.getAttribute("data-hash") === route);
    });
}

async function navigate() {
  const route = normalizeHash(location.hash || "#/");
  if (route !== location.hash) {
    history.replaceState(null, "", route);
  }
  state.route = route;
  closeMoreSheet();

  const def = ROUTES[route];
  el.eyebrow.textContent = def.eyebrow;
  el.title.textContent = def.title;
  el.subtitle.textContent = def.subtitle;
  updateActiveNav(route);

  const isChat = route.startsWith("#/chat/");
  el.container.classList.toggle("chat-mode", isChat);

  // Alte View sofort weg, neue View animiert rein (fadeUp im CSS).
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
}

window.addEventListener("hashchange", navigate);

/* ---------------------------------------------------------------------
 * Sidebar-Footer (System-Status)
 * --------------------------------------------------------------------- */

function renderFooter(system) {
  el.statusDot.classList.remove("offline");
  const rel = relativeTime(system && system.oura_last_sync);
  const dbText = system && system.db_size_mb != null ? `${fmtNum(system.db_size_mb, 1)} MB` : "–";
  el.footerStatusText.textContent = `Online · Oura-Sync ${rel || "–"} · DB ${dbText}`;
}

function renderFooterOffline() {
  el.statusDot.classList.add("offline");
  el.footerStatusText.textContent = "Offline";
}

/* ---------------------------------------------------------------------
 * View 1 — Overview (Dashboard)
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
  grid.className = "bento";
  panel.innerHTML = "";
  panel.appendChild(grid);

  const today = ov.today || {};
  const daily = health.daily || [];

  // --- Hero: Ring-Cluster (Schlaf/Readiness/Aktivität) ---
  const hero = document.createElement("section");
  hero.className = "card card-lg hero-card";
  hero.setAttribute("aria-label", "Manuels Vitalwerte, letzte 30 Tage");
  const dateLabel = new Date().toLocaleDateString("de-DE", {
    weekday: "short",
    day: "2-digit",
    month: "2-digit",
  });
  hero.innerHTML = `
    <div class="hero-top">
      <span class="hero-label">Vitalwerte, letzte 30 Tage</span>
      <span class="hero-date">${esc(dateLabel)}</span>
    </div>
    <div class="ring-row"></div>
  `;
  const rings = [
    { value: today.sleep_score, color: "var(--accent)", r: 70, label: "Schlaf-Score" },
    { value: today.readiness_score, color: "var(--assistant)", r: 54, label: "Readiness" },
    { value: today.activity_score, color: "var(--ok)", r: 38, label: "Aktivität" },
  ];
  const ringRow = hero.querySelector(".ring-row");
  ringRow.appendChild(buildRingCluster(rings));
  const readoutsWrap = document.createElement("div");
  readoutsWrap.className = "ring-readouts";
  rings.forEach((r) => {
    const item = document.createElement("div");
    item.className = "ring-readout";
    item.innerHTML = `<div class="v">${fmtNum(r.value)}</div><div class="l">${esc(r.label)}</div>`;
    readoutsWrap.appendChild(item);
  });
  ringRow.appendChild(readoutsWrap);
  grid.appendChild(hero);

  // --- Quick Stats ---
  const weeklyWorkouts = health.workouts_per_week || [];
  const goal = 3;
  const thisWeek = weeklyWorkouts[weeklyWorkouts.length - 1];
  let streakWeeks = 0;
  for (let i = weeklyWorkouts.length - 1; i >= 0; i--) {
    if (weeklyWorkouts[i].count >= goal) streakWeeks++;
    else break;
  }
  const avg7 = ov.meals_7d_avg || {};
  const lastPr = (exercises || [])
    .filter((ex) => ex.pr_date)
    .sort((a, b) => (a.pr_date < b.pr_date ? 1 : -1))[0];

  const quickStats = document.createElement("section");
  quickStats.className = "card card-md";
  quickStats.innerHTML = `
    <h3 class="card-title">Quick Stats</h3>
    <div class="quick-stats">
      <div class="qs-row"><span class="qs-label">Workouts diese Woche</span><span class="qs-value ${
        thisWeek && thisWeek.count >= goal ? "up" : ""
      }">${thisWeek ? thisWeek.count : 0} / ${goal}</span></div>
      <div class="qs-row"><span class="qs-label">Protein Ø 7 Tage</span><span class="qs-value">${
        avg7.protein_g != null ? fmtNum(avg7.protein_g, 0) + " g" : "–"
      }</span></div>
      <div class="qs-row"><span class="qs-label">Streak</span><span class="qs-value ${
        streakWeeks > 0 ? "up" : ""
      }">${streakWeeks} Woche${streakWeeks === 1 ? "" : "n"}</span></div>
      <div class="qs-row"><span class="qs-label">Letzter PR</span><span class="qs-value">${
        lastPr ? esc(lastPr.name) : "–"
      }</span></div>
    </div>
  `;
  grid.appendChild(quickStats);

  // --- 3 Stat-Tiles ---
  const tiles = [
    ["Schlaf-Score", daily.map((d) => d.sleep_score), today.sleep_score, ""],
    ["HRV", daily.map((d) => d.hrv_avg), today.hrv_avg, "ms"],
    ["Ruhepuls", daily.map((d) => d.resting_hr), today.resting_hr, "bpm"],
  ];
  tiles.forEach(([title, values, todayValue, unit]) => {
    const tile = metricCard(title, values, todayValue, unit, 0);
    tile.classList.add("card-third");
    grid.appendChild(tile);
  });

  // --- Nächste Termine ---
  const eventsCard = document.createElement("section");
  eventsCard.className = "card card-half";
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
    : '<p class="meal-summary-empty">Keine Termine in den nächsten 2 Tagen.</p>';
  eventsCard.innerHTML = `<h3 class="card-title">Nächste Termine</h3>${eventsRows}`;
  grid.appendChild(eventsCard);

  // --- Heute gegessen (Makro-Donut) ---
  const mealsCard = document.createElement("section");
  mealsCard.className = "card card-half";
  const m = ov.meals_today || {};
  mealsCard.innerHTML = `<h3 class="card-title">Heute gegessen</h3>`;
  if ((m.count || 0) > 0 && m.protein_g != null) {
    // meals_today liefert nur protein_g/calories_kcal — für den Donut
    // brauchen wir carbs/fat, die kommen aus den heutigen Einzel-Mahlzeiten
    // (separat unten via renderTodayMacroDonut nachgeladen).
    const wrap = document.createElement("div");
    wrap.className = "macro-ring-wrap";
    wrap.innerHTML = '<p class="meal-summary-empty">Lade Makros …</p>';
    mealsCard.appendChild(wrap);
    fetchJson("/api/meals?days=1")
      .then((mealsData) => {
        if (!document.body.contains(wrap)) return;
        const todays = (mealsData.meals || []).filter((mm) => (mm.ts || "").slice(0, 10) === todayIso());
        const protein = todays.reduce((a, mm) => a + (mm.protein_g || 0), 0);
        const carbs = todays.reduce((a, mm) => a + (mm.carbs_g || 0), 0);
        const fat = todays.reduce((a, mm) => a + (mm.fat_g || 0), 0);
        const donut = buildMacroDonut(protein, carbs, fat, 96);
        wrap.innerHTML = "";
        if (donut) {
          wrap.appendChild(donut);
          wrap.appendChild(macroLegend(protein, carbs, fat));
        } else {
          wrap.innerHTML = '<p class="meal-summary-empty">Heute noch nichts geloggt.</p>';
        }
      })
      .catch(() => {
        if (document.body.contains(wrap)) {
          wrap.innerHTML = '<p class="meal-summary-empty">Makros konnten nicht geladen werden.</p>';
        }
      });
  } else {
    mealsCard.innerHTML += '<p class="meal-summary-empty">Heute noch nichts geloggt.</p>';
  }
  grid.appendChild(mealsCard);

  // --- Zusatzkarten: domänenübergreifende Kennzahlen ---
  grid.appendChild(workoutsPerWeekCard(weeklyWorkouts));
  grid.appendChild(prHighlightsCard(exercises, 7));

  const avgCard = document.createElement("section");
  avgCard.className = "card card-half";
  const avgBody =
    (avg7.days_logged || 0) > 0
      ? `<div class="quick-stats" style="gap:8px">
          <div class="qs-row"><span class="qs-label">Protein</span><span class="qs-value">${fmtNum(
            avg7.protein_g,
            0
          )} g</span></div>
          <div class="qs-row"><span class="qs-label">Kalorien</span><span class="qs-value">${fmtNum(
            avg7.calories_kcal,
            0
          )} kcal</span></div>
          <div class="qs-row"><span class="qs-label">Geloggte Tage</span><span class="qs-value">${
            avg7.days_logged
          } / 7</span></div>
        </div>`
      : '<p class="meal-summary-empty">Letzte 7 Tage noch nichts geloggt.</p>';
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
  const row = document.createElement("div");
  const isUser = msg.role === "user";
  row.className = "bubble-row " + (isUser ? "msg-user" : "msg-agent");
  const bubble = document.createElement("div");
  bubble.className = "msg";
  const textEl = document.createElement("div");
  textEl.className = "msg-text";
  textEl.innerHTML = renderInlineMarkdown(msg.content);
  const tsEl = document.createElement("span");
  tsEl.className = "msg-ts";
  tsEl.textContent = fmtTs(msg.ts);
  bubble.appendChild(textEl);
  bubble.appendChild(tsEl);
  row.appendChild(bubble);
  return row;
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
      <form class="chat-input-bar">
        <textarea
          class="chat-input"
          placeholder="Nachricht an ${esc(meta.display)} …"
          rows="1"
          aria-label="Nachricht"
        ></textarea>
        <button type="submit" class="chat-send" aria-label="Senden">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden="true">
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
  const formEl = panel.querySelector(".chat-input-bar");
  const inputEl = panel.querySelector(".chat-input");
  const sendEl = panel.querySelector(".chat-send");
  const typingEl = panel.querySelector(".typing-indicator");

  function scrollToEnd() {
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  function autoResize() {
    inputEl.style.height = "auto";
    inputEl.style.height = Math.min(inputEl.scrollHeight, 140) + "px";
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
      const res = await apiFetch(`/api/chat/${agent}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: text }),
      });
      const data = await res.json();
      // Isa kann im Turn geloggt/gespeichert haben — Dashboard-Daten neu holen.
      invalidateCache();
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
    const res = await apiFetch(`/api/chat/${agent}/history?limit=50`);
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
    return '<span class="trend-flat">→</span>';
  }
  const arrow = rounded > 0 ? "↑" : "↓";
  const cls = rounded > 0 ? "trend-up" : "trend-down";
  return `<span class="${cls}">${arrow} ${formatValue(Math.abs(rounded), digits)}${
    unit ? " " + unit : ""
  }</span>`;
}

function metricCard(title, values, todayValue, unit, digits = 0, formatValue = fmtNum) {
  const avg = average(values);
  const compare = avg === null ? "Ø: keine Daten" : `Ø ${formatValue(avg, digits)}${unit ? " " + unit : ""}`;
  const trend = trendBadge(values, digits, unit, formatValue);

  const wrap = document.createElement("section");
  wrap.className = "card";
  wrap.innerHTML = `
    <h3 class="card-title">${esc(title)}</h3>
    <div class="v">${todayValue != null ? formatValue(todayValue, digits) : "–"}${
    todayValue != null && unit ? `<span class="unit">${esc(unit)}</span>` : ""
  }</div>
    <div class="cmp">${trend} ${esc(compare)}</div>
    <svg class="card-sparkline" aria-hidden="true"></svg>
  `;
  const svgEl = wrap.querySelector(".card-sparkline");
  renderSparkline(svgEl, values);

  const present = values.filter((v) => v != null);
  const svgTitle = document.createElementNS("http://www.w3.org/2000/svg", "title");
  svgTitle.textContent = `${title}: Min ${
    present.length ? formatValue(Math.min(...present), digits) : "–"
  }, Max ${present.length ? formatValue(Math.max(...present), digits) : "–"}`;
  svgEl.prepend(svgTitle);

  return wrap;
}

function isoWeekLabel(mondayIso) {
  // Durchgehend UTC rechnen: mit lokalem Datum + getUTC*-Methoden war in
  // Europe/Berlin jedes Montag-00:00 ein Sonntag-22:00-UTC -> KW um 1 zu niedrig.
  const d = new Date(mondayIso + "T00:00:00Z");
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

function workoutsPerWeekCard(workoutsPerWeek) {
  const wrap = document.createElement("section");
  wrap.className = "card card-full";
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
    const belowGoal = w.count < target ? " bar-below-goal" : "";
    bars += `<rect class="bar${belowGoal}" x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${barWidth.toFixed(
      1
    )}" height="${Math.max(barH, 1).toFixed(1)}" rx="3"><title>${weekLabel}: ${
      w.count
    } Workout(s)</title></rect>`;
  });

  wrap.innerHTML = `
    <h3 class="card-title">Workouts pro Woche · Ziel ${target}</h3>
    <svg class="bars-svg" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" role="img" aria-label="Workouts pro Woche, letzte ${
    workoutsPerWeek.length
  } Wochen, Ziel ${target} pro Woche">
      <line class="bar-target-line" x1="${padding}" y1="${targetY.toFixed(1)}" x2="${
    width - padding
  }" y2="${targetY.toFixed(1)}"><title>Ziel: ${target} pro Woche</title></line>
      ${bars}
    </svg>
  `;
  return wrap;
}

function proteinBarsCard(days, title) {
  const wrap = document.createElement("section");
  wrap.className = "card card-half";
  const width = 560;
  const height = 110;
  const padding = 20;
  const values = days.map((m) => m.protein_g || 0);
  const maxV = Math.max(...values, 1);
  const gap = (width - padding * 2) / Math.max(days.length, 1);
  const barWidth = gap / 1.9;

  let bars = "";
  days.forEach((m, i) => {
    const barH = ((m.protein_g || 0) / maxV) * (height - padding * 2 - 14);
    const x = padding + i * gap + (gap - barWidth) / 2;
    const y = height - padding - barH;
    const dateLabel = new Date(m.date + "T00:00:00").toLocaleDateString("de-DE", {
      day: "2-digit",
      month: "2-digit",
    });
    bars += `<rect class="bar" x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${barWidth.toFixed(
      1
    )}" height="${Math.max(barH, 1).toFixed(1)}" rx="3"><title>${dateLabel}: ${fmtNum(
      m.protein_g,
      0
    )} g Protein${m.calories_kcal != null ? " · " + fmtNum(m.calories_kcal, 0) + " kcal" : ""}</title></rect>`;
  });

  const { paths: kcalPaths } = buildPathSegments(
    days.map((d) => (d.calories_kcal != null ? d.calories_kcal : null)),
    width,
    height,
    padding
  );
  const kcalPathsHtml = kcalPaths.map((p) => `<path class="calories-line" d="${p.line}"></path>`).join("");

  const avgProtein = average(values);

  wrap.innerHTML = `
    <h3 class="card-title">${esc(title)}</h3>
    <svg class="bars-svg macro-bars-svg" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" role="img" aria-label="${esc(
    title
  )}, Protein-Balken mit Kalorien-Linie">
      ${bars}
      ${kcalPathsHtml}
    </svg>
    <div class="cmp">Ø ${fmtNum(avgProtein, 0)} g Protein / Tag · gestrichelt = kcal</div>
  `;
  wrap.querySelectorAll(".calories-line").forEach((p) => drawIn(p));
  return wrap;
}

function heatmapCard(daily) {
  const wrap = document.createElement("section");
  wrap.className = "card card-full";
  wrap.innerHTML = `<h3 class="card-title">Schlaf-Konsistenz, letzte ${daily.length} Tage</h3>`;
  const grid = document.createElement("div");
  grid.className = "heatmap";
  daily.forEach((d) => {
    const cell = document.createElement("div");
    cell.className = "heatmap-cell";
    if (d.sleep_score != null) {
      const pct = Math.max(0, Math.min(90, Math.round((d.sleep_score / 100) * 90)));
      cell.style.background = `color-mix(in oklab, var(--accent) ${pct}%, var(--surface2))`;
    }
    cell.title = `${fmtDate(d.date)}: ${d.sleep_score != null ? d.sleep_score : "keine Daten"}`;
    grid.appendChild(cell);
  });
  wrap.appendChild(grid);
  return wrap;
}

async function renderHealth(panel) {
  panel.innerHTML = '<p class="empty-state">Lade Health-Daten …</p>';
  let data;
  try {
    data = await fetchJson("/api/health/overview?days=35");
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
  grid.className = "bento";
  panel.innerHTML = "";
  panel.appendChild(grid);

  const daily35 = data.daily;
  const daily = daily35.slice(-30); // "Ø 30 Tage" für die Stat-Tiles
  const today = data.today || {};
  const tiles = [
    ["Schlaf-Score", daily.map((d) => d.sleep_score), today.sleep_score, ""],
    ["Readiness", daily.map((d) => d.readiness_score), today.readiness_score, ""],
    ["Aktivität", daily.map((d) => d.activity_score), today.activity_score, ""],
    ["HRV", daily.map((d) => d.hrv_avg), today.hrv_avg, "ms"],
    ["Ruhepuls", daily.map((d) => d.resting_hr), today.resting_hr, "bpm"],
    ["Schritte", daily.map((d) => d.steps), today.steps, "Schritte"],
  ];
  tiles.forEach(([title, values, todayValue, unit]) => {
    const tile = metricCard(title, values, todayValue, unit, 0);
    tile.classList.add("card-third");
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
  sleepDurationTile.classList.add("card-third");
  grid.appendChild(sleepDurationTile);

  grid.appendChild(heatmapCard(daily35));
  grid.appendChild(workoutsPerWeekCard(data.workouts_per_week || []));
}

/* ---------------------------------------------------------------------
 * View 5 — Training
 * --------------------------------------------------------------------- */

const SOURCE_LABELS = {
  hevy: "Hevy",
  chat: "Chat",
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

function daysSince(iso) {
  const then = new Date(String(iso).slice(0, 10) + "T00:00:00").getTime();
  if (Number.isNaN(then)) return Infinity;
  return Math.round((Date.now() - then) / 86400000);
}

/* Übungsliste (linke Spalte): Klick wählt die aktive Übung für den
   Verlaufs-Chart rechts. PR-Chip nur bei PR innerhalb der letzten 7 Tage. */
function buildExerciseList(exercises, activeName, onSelect) {
  const wrap = document.createElement("div");
  wrap.className = "ex-list";
  exercises.forEach((ex) => {
    const item = document.createElement("button");
    item.type = "button";
    item.className = "ex-item" + (ex.name === activeName ? " active" : "");
    const isRecentPr = ex.pr_date && daysSince(ex.pr_date) <= 7;
    item.innerHTML = `
      <div class="ex-item-top">
        <span class="ex-item-name">${esc(ex.name)}</span>
        <span class="ex-item-weight">${ex.last_weight_kg != null ? fmtNum(ex.last_weight_kg, 1) + " kg" : "–"}</span>
      </div>
      <span class="ex-item-cat">${esc(ex.category || "Sonstige")}</span>
      ${isRecentPr ? `<span class="pr-chip">★ Neuer PR · ${esc(fmtDate(ex.pr_date))}</span>` : ""}
    `;
    item.addEventListener("click", () => onSelect(ex.name));
    wrap.appendChild(item);
  });
  return wrap;
}

function buildProgressCard() {
  const card = document.createElement("section");
  card.className = "card card-full progress-card";
  card.innerHTML = `
    <div class="progress-head">
      <h3 class="card-title" id="progress-card-title">Fortschritt</h3>
      <div class="progress-head-controls">
        <div class="metric-toggle" role="group" aria-label="Anzeige wählen">
          <button type="button" class="metric-toggle-btn active" data-metric="weight">Gewicht</button>
          <button type="button" class="metric-toggle-btn" data-metric="e1rm">e1RM</button>
        </div>
      </div>
    </div>
    <div class="progress-readouts">
      <div class="ring-readout">
        <span class="v" data-field="current" style="font-size:22px">–</span>
        <span class="l">Aktuell</span>
      </div>
      <div class="ring-readout">
        <span class="v" data-field="max" style="font-size:22px">–</span>
        <span class="l">Max</span>
      </div>
      <div class="ring-readout">
        <span class="v" data-field="sessions" style="font-size:22px">–</span>
        <span class="l">Sessions</span>
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

  return card;
}

async function loadProgressInto(card, name) {
  card.querySelector("#progress-card-title").textContent = `${name} — Verlauf`;
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

    const isMax = c.p[metricField] === max;
    const dot = document.createElementNS("http://www.w3.org/2000/svg", "circle");
    dot.setAttribute("cx", c.x.toFixed(2));
    dot.setAttribute("cy", c.y.toFixed(2));
    dot.setAttribute("r", isMax ? "5.5" : "3.5");
    dot.setAttribute("class", "progress-dot" + (isMax ? " is-max" : ""));
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
  wrap.className = "card card-full";
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
    )}" height="${Math.max(barH, 1).toFixed(1)}" rx="3"><title>${esc(titleTxt)}</title></rect>`;
  });

  const avgVolume = average(volumePerWeek.map((w) => w.volume_kg));

  wrap.innerHTML = `
    <h3 class="card-title">Volumen pro Woche</h3>
    <svg class="bars-svg" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" role="img" aria-label="Trainingsvolumen pro Woche, letzte ${
    volumePerWeek.length
  } Wochen">
      ${bars}
    </svg>
    <div class="cmp">Ø ${fmtNum(avgVolume, 0)} kg / Woche</div>
  `;
  return wrap;
}

function prHighlightsCard(exercises, days = 30) {
  const wrap = document.createElement("section");
  wrap.className = "card card-half";

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
    : `<p class="meal-summary-empty">Keine neuen PRs in den letzten ${days} Tagen.</p>`;

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

  if (!exercises || exercises.length === 0) {
    const empty = document.createElement("p");
    empty.className = "empty-state";
    empty.innerHTML = `Noch nichts geloggt.
      <span class="empty-state-hint">Sag Isa im Chat, was du trainiert hast — oder logge es in Hevy, der Sync holt es nachts.</span>`;
    panel.appendChild(empty);
    return;
  }

  const layout = document.createElement("div");
  layout.className = "ex-layout";
  panel.appendChild(layout);

  const progressCard = buildProgressCard();
  const rightCol = document.createElement("div");
  rightCol.appendChild(progressCard);
  rightCol.appendChild(volumeBarsCard(volume.volume_per_week || []));
  rightCol.appendChild(prHighlightsCard(exercises, 30));

  const exList = buildExerciseList(exercises, exercises[0].name, (name) => {
    exList.querySelectorAll(".ex-item").forEach((item) => {
      item.classList.toggle("active", item.querySelector(".ex-item-name").textContent === name);
    });
    loadProgressInto(progressCard, name);
  });

  layout.appendChild(exList);
  layout.appendChild(rightCol);

  loadProgressInto(progressCard, exercises[0].name);

  const allWorkouts = base.workouts || [];
  if (allWorkouts.length === 0) {
    const empty = document.createElement("p");
    empty.className = "empty-state";
    empty.innerHTML = `Noch keine Session geloggt.
      <span class="empty-state-hint">Sag Isa im Chat, was du trainiert hast — oder logge es in Hevy, der Sync holt es nachts.</span>`;
    rightCol.appendChild(empty);
    return;
  }

  const sectionTitle = document.createElement("div");
  sectionTitle.className = "section-title-row";
  sectionTitle.innerHTML = `<span class="num">Verlauf</span><h2>Session-Historie</h2>`;
  rightCol.appendChild(sectionTitle);

  // Filterleiste: Type-Chips und Freitext filtern rein clientseitig gegen die
  // bereits geladene Liste (raw-Feld-Matches, keine Roundtrips pro Tastendruck).
  // Der Übungs-Filter braucht dagegen die serverseitige `canonicalize()`-
  // Heuristik (Strong-/Hevy-Namensvarianten zusammenführen) — die wird hier
  // NICHT dupliziert, stattdessen holt ein Select-Change gezielt
  // `/api/workouts?exercise=...` nach (selten genug, dass ein Roundtrip okay ist).
  const filterBar = document.createElement("div");
  filterBar.className = "filter-bar";
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
  rightCol.appendChild(filterBar);

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
  rightCol.appendChild(list);

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

  const grid = document.createElement("div");
  grid.className = "bento";
  panel.innerHTML = "";
  panel.appendChild(grid);

  // --- Makro-Split heute (Donut) ---
  const todayMeals = (data.meals || []).filter((m) => (m.ts || "").slice(0, 10) === todayIso());
  const tProtein = todayMeals.reduce((a, m) => a + (m.protein_g || 0), 0);
  const tCarbs = todayMeals.reduce((a, m) => a + (m.carbs_g || 0), 0);
  const tFat = todayMeals.reduce((a, m) => a + (m.fat_g || 0), 0);
  const donutCard = document.createElement("section");
  donutCard.className = "card card-half";
  donutCard.innerHTML = `<h3 class="card-title">Makro-Split heute</h3>`;
  const donut = buildMacroDonut(tProtein, tCarbs, tFat, 140);
  if (donut) {
    const wrap = document.createElement("div");
    wrap.className = "macro-ring-wrap";
    wrap.appendChild(donut);
    wrap.appendChild(macroLegend(tProtein, tCarbs, tFat));
    donutCard.appendChild(wrap);
  } else {
    donutCard.innerHTML += '<p class="meal-summary-empty">Heute noch nichts geloggt.</p>';
  }
  grid.appendChild(donutCard);

  // --- Protein & Kalorien, 14 Tage ---
  const totalsByDay = {};
  (data.daily_totals || []).forEach((t) => {
    totalsByDay[t.day] = t;
  });
  const days14 = [];
  const now = new Date();
  for (let i = 13; i >= 0; i--) {
    const d = new Date(now.getFullYear(), now.getMonth(), now.getDate() - i);
    const iso = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(
      d.getDate()
    ).padStart(2, "0")}`;
    const t = totalsByDay[iso];
    days14.push({
      date: iso,
      protein_g: t ? t.protein_g_sum || 0 : 0,
      calories_kcal: t ? t.calories_kcal_sum || 0 : 0,
    });
  }
  grid.appendChild(proteinBarsCard(days14, "Protein & Kalorien, 14 Tage"));

  // --- Mahlzeiten ---
  const meals = data.meals || [];
  const mealsCard = document.createElement("section");
  mealsCard.className = "card card-full";
  const sectionTitle = document.createElement("div");
  sectionTitle.className = "section-title-row";
  sectionTitle.style.marginTop = "0";
  sectionTitle.innerHTML = `<span class="num">Verlauf</span><h2>Mahlzeiten</h2>`;
  mealsCard.appendChild(sectionTitle);

  if (meals.length === 0) {
    const hint = document.createElement("p");
    hint.className = "empty-state";
    hint.innerHTML = `In den letzten 30 Tagen ist nichts geloggt.
      <span class="empty-state-hint">Schick Isa ein Essens-Foto — sie schätzt die Makros und loggt für dich.</span>`;
    mealsCard.appendChild(hint);
  } else {
    const list = document.createElement("div");
    list.className = "meal-list";
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
        row.className = "meal-row";
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
    mealsCard.appendChild(list);
  }
  grid.appendChild(mealsCard);
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

applyThemeUi(currentTheme());
loadSystemStatus();
navigate();
