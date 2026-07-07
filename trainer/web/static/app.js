"use strict";

/* Agent Hub — Vanilla JS, kein Framework, kein Build-Step.
 * Zwei Ansichten (Chat / Health), zwei Agenten (isa / assistant), deren
 * Identitätsfarbe via CSS-Custom-Property --accent gesteuert wird. */

const AGENT_COLORS = { isa: "var(--isa)", assistant: "var(--assistant)" };
const AGENT_COLOR_HEX = { isa: "#ff6b5e", assistant: "#6ba8ff" };
const EMPTY_CHAT_HINTS = {
  isa: "Noch keine Nachrichten — frag Isa nach deinem Schlaf.",
  assistant: "Noch keine Nachrichten — frag deinen Assistenten, was heute ansteht.",
};

const REDUCE_MOTION = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

const state = {
  agents: [],
  currentAgent: "isa",
  currentView: "chat",
  sending: false,
};

const el = {
  agentDots: document.getElementById("agent-dots"),
  navChat: document.getElementById("nav-chat"),
  navHealth: document.getElementById("nav-health"),
  viewChat: document.getElementById("view-chat"),
  viewHealth: document.getElementById("view-health"),
  chatMessages: document.getElementById("chat-messages"),
  chatForm: document.getElementById("chat-form"),
  chatInput: document.getElementById("chat-input"),
  chatSend: document.getElementById("chat-send"),
  typingIndicator: document.getElementById("typing-indicator"),
  typingLabel: document.getElementById("typing-label"),
  healthGrid: document.getElementById("health-grid"),
  pulseSvg: document.getElementById("pulse-svg"),
  pulseLineGroup: document.getElementById("pulse-line-group"),
  pulseGlowGroup: document.getElementById("pulse-glow-group"),
  readoutHrv: document.getElementById("readout-hrv"),
  readoutReadiness: document.getElementById("readout-readiness"),
  readoutSleep: document.getElementById("readout-sleep"),
};

/* ---------------------------------------------------------------------
 * Kleine Helfer
 * --------------------------------------------------------------------- */

function fmtNum(v, digits = 0) {
  if (v === null || v === undefined || Number.isNaN(v)) return "–";
  return digits > 0 ? v.toFixed(digits) : Math.round(v).toString();
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

function average(values) {
  const nums = values.filter((v) => v !== null && v !== undefined && !Number.isNaN(v));
  if (nums.length === 0) return null;
  return nums.reduce((a, b) => a + b, 0) / nums.length;
}

function setAccent(agent) {
  document.documentElement.style.setProperty("--accent", AGENT_COLORS[agent] || AGENT_COLORS.isa);
}

/* ---------------------------------------------------------------------
 * Agenten-Rail
 * --------------------------------------------------------------------- */

async function loadAgents() {
  try {
    const res = await fetch("/api/agents");
    state.agents = await res.json();
  } catch {
    state.agents = [
      { name: "isa", display_name: "Isa" },
      { name: "assistant", display_name: "Assistant" },
    ];
  }
  renderAgentDots();
}

function renderAgentDots() {
  el.agentDots.innerHTML = "";
  state.agents.forEach((agent) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "agent-dot" + (agent.name === state.currentAgent ? " active" : "");
    btn.style.setProperty("--dot-color", AGENT_COLORS[agent.name] || "var(--text-dim)");
    btn.setAttribute("role", "tab");
    btn.setAttribute("aria-selected", agent.name === state.currentAgent ? "true" : "false");
    btn.title = agent.display_name;
    btn.innerHTML = `<span class="dot-circle" aria-hidden="true"></span><span class="agent-name">${agent.display_name}</span>`;
    btn.addEventListener("click", () => selectAgent(agent.name));
    el.agentDots.appendChild(btn);
  });
}

async function selectAgent(name) {
  if (name === state.currentAgent) return;
  state.currentAgent = name;
  setAccent(name);
  renderAgentDots();
  await loadHistory();
}

/* ---------------------------------------------------------------------
 * Ansicht (Chat / Health) umschalten
 * --------------------------------------------------------------------- */

function selectView(view) {
  state.currentView = view;
  const isChat = view === "chat";
  el.viewChat.classList.toggle("active", isChat);
  el.viewHealth.classList.toggle("active", !isChat);
  el.navChat.classList.toggle("active", isChat);
  el.navHealth.classList.toggle("active", !isChat);
  el.navChat.setAttribute("aria-selected", isChat ? "true" : "false");
  el.navHealth.setAttribute("aria-selected", !isChat ? "true" : "false");
}

el.navChat.addEventListener("click", () => selectView("chat"));
el.navHealth.addEventListener("click", () => selectView("health"));

/* ---------------------------------------------------------------------
 * Chat
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

async function loadHistory() {
  el.chatMessages.innerHTML = '<p class="empty-state">Lade Nachrichten …</p>';
  try {
    const res = await fetch(`/api/chat/${state.currentAgent}/history?limit=50`);
    const history = await res.json();
    el.chatMessages.innerHTML = "";
    if (!Array.isArray(history) || history.length === 0) {
      const hint = document.createElement("p");
      hint.className = "empty-state";
      hint.textContent = EMPTY_CHAT_HINTS[state.currentAgent] || "Noch keine Nachrichten.";
      el.chatMessages.appendChild(hint);
    } else {
      history.forEach((msg) => el.chatMessages.appendChild(renderMessage(msg)));
      el.chatMessages.scrollTop = el.chatMessages.scrollHeight;
    }
  } catch {
    el.chatMessages.innerHTML =
      '<p class="empty-state">Nachrichten konnten nicht geladen werden.</p>';
  }
}

function autoResizeTextarea() {
  el.chatInput.style.height = "auto";
  el.chatInput.style.height = Math.min(el.chatInput.scrollHeight, 160) + "px";
}

el.chatInput.addEventListener("input", autoResizeTextarea);

el.chatInput.addEventListener("keydown", (ev) => {
  if (ev.key === "Enter" && !ev.shiftKey) {
    ev.preventDefault();
    el.chatForm.requestSubmit();
  }
});

el.chatForm.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const text = el.chatInput.value.trim();
  if (!text || state.sending) return;

  const emptyHint = el.chatMessages.querySelector(".empty-state");
  if (emptyHint) emptyHint.remove();

  el.chatMessages.appendChild(
    renderMessage({ role: "user", content: text, ts: new Date().toISOString() })
  );
  el.chatMessages.scrollTop = el.chatMessages.scrollHeight;

  el.chatInput.value = "";
  autoResizeTextarea();

  state.sending = true;
  el.chatSend.disabled = true;
  el.typingLabel.textContent = `${displayName(state.currentAgent)} antwortet …`;
  el.typingIndicator.hidden = false;

  const agentAtSend = state.currentAgent;

  try {
    const res = await fetch(`/api/chat/${agentAtSend}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: text }),
    });
    const data = await res.json();
    if (agentAtSend !== state.currentAgent) return; // Agent gewechselt während der Antwort

    if (!res.ok || data.error) {
      el.chatMessages.appendChild(
        renderMessage({
          role: "assistant",
          content: `Fehler: ${data.error || "Antwort fehlgeschlagen."}`,
          ts: new Date().toISOString(),
        })
      );
    } else {
      el.chatMessages.appendChild(
        renderMessage({ role: "assistant", content: data.reply, ts: new Date().toISOString() })
      );
    }
    el.chatMessages.scrollTop = el.chatMessages.scrollHeight;
  } catch {
    if (agentAtSend === state.currentAgent) {
      el.chatMessages.appendChild(
        renderMessage({
          role: "assistant",
          content: "Fehler: Server nicht erreichbar.",
          ts: new Date().toISOString(),
        })
      );
    }
  } finally {
    state.sending = false;
    el.chatSend.disabled = false;
    el.typingIndicator.hidden = true;
  }
});

function displayName(agentName) {
  const found = state.agents.find((a) => a.name === agentName);
  return found ? found.display_name : agentName;
}

/* ---------------------------------------------------------------------
 * SVG-Pfad-Helfer (Sparklines, Puls-Header) — lässt bei NULL-Werten
 * Lücken statt zu crashen oder zu interpolieren.
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
  const len = pathEl.getTotalLength();
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
  const height = 48;
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
 * Puls-Header (HRV der letzten 30 Tage)
 * --------------------------------------------------------------------- */

function renderPulseHeader(daily, today) {
  const width = 1000;
  const height = 90;
  const hrvValues = daily.map((d) => d.hrv_avg);
  const { paths } = buildPathSegments(hrvValues, width, height, 10);

  el.pulseLineGroup.innerHTML = "";
  el.pulseGlowGroup.innerHTML = "";

  paths.forEach((d) => {
    const glow = document.createElementNS("http://www.w3.org/2000/svg", "path");
    glow.setAttribute("d", d);
    glow.setAttribute("class", "pulse-glow");
    el.pulseGlowGroup.appendChild(glow);

    const line = document.createElementNS("http://www.w3.org/2000/svg", "path");
    line.setAttribute("d", d);
    line.setAttribute("class", "pulse-line");
    el.pulseLineGroup.appendChild(line);

    drawIn(glow);
    drawIn(line);
  });

  el.readoutHrv.textContent = today.hrv_avg != null ? `${fmtNum(today.hrv_avg, 0)} ms` : "–";
  el.readoutReadiness.textContent = fmtNum(today.readiness_score);
  el.readoutSleep.textContent = fmtNum(today.sleep_score);
}

/* ---------------------------------------------------------------------
 * Health-Karten
 * --------------------------------------------------------------------- */

function metricCard(title, values, todayValue, unit, digits = 0) {
  const avg = average(values);
  const compare =
    avg === null
      ? "Ø 30 Tage: keine Daten"
      : `Ø 30 Tage: ${fmtNum(avg, digits)}${unit ? " " + unit : ""}`;

  const wrap = document.createElement("div");
  wrap.className = "card";
  wrap.innerHTML = `
    <h3 class="card-title">${title}</h3>
    <div class="card-value">${todayValue != null ? fmtNum(todayValue, digits) : "–"}${
    todayValue != null && unit ? `<span class="unit">${unit}</span>` : ""
  }</div>
    <div class="card-compare">${compare}</div>
    <svg class="card-sparkline" aria-hidden="true"></svg>
  `;
  const svgEl = wrap.querySelector(".card-sparkline");
  renderSparkline(svgEl, values, "sparkline-path");

  const svgTitle = document.createElementNS("http://www.w3.org/2000/svg", "title");
  svgTitle.textContent = `${title}, letzte 30 Tage: Min ${
    values.filter((v) => v != null).length ? fmtNum(Math.min(...values.filter((v) => v != null)), digits) : "–"
  }, Max ${
    values.filter((v) => v != null).length ? fmtNum(Math.max(...values.filter((v) => v != null)), digits) : "–"
  }`;
  svgEl.prepend(svgTitle);

  return wrap;
}

function workoutsCard(workoutsPerWeek) {
  const wrap = document.createElement("div");
  wrap.className = "card card-wide";
  const target = 3;
  const width = 640;
  const height = 92;
  const padding = 24;
  const maxCount = Math.max(target, ...workoutsPerWeek.map((w) => w.count), 1);
  const barWidth = (width - padding * 2) / workoutsPerWeek.length / 1.8;
  const gap = (width - padding * 2) / workoutsPerWeek.length;

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

function proteinCard(mealsDaily) {
  const wrap = document.createElement("div");
  wrap.className = "card card-wide";
  const width = 640;
  const height = 92;
  const padding = 24;
  const values = mealsDaily.map((m) => m.protein_g || 0);
  const maxV = Math.max(...values, 1);
  const gap = (width - padding * 2) / mealsDaily.length;
  const barWidth = gap / 1.8;

  let bars = "";
  mealsDaily.forEach((m, i) => {
    const barH = (m.protein_g / maxV) * (height - padding * 2 - 10);
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
    <h3 class="card-title">Protein, letzte 14 Tage</h3>
    <svg class="bars-svg" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" role="img" aria-label="Protein pro Tag, letzte 14 Tage">
      ${bars}
    </svg>
    <div class="card-compare">Ø ${fmtNum(avgProtein, 0)} g / Tag</div>
  `;
  return wrap;
}

function renderHealthGrid(data) {
  el.healthGrid.innerHTML = "";

  if (!data.daily || data.daily.length === 0) {
    el.healthGrid.innerHTML = '<p class="empty-state">Noch keine Health-Daten.</p>';
    return;
  }

  const sleepValues = data.daily.map((d) => d.sleep_score);
  const readinessValues = data.daily.map((d) => d.readiness_score);
  const hrvValues = data.daily.map((d) => d.hrv_avg);
  const rhrValues = data.daily.map((d) => d.resting_hr);

  el.healthGrid.appendChild(metricCard("Schlaf-Score", sleepValues, data.today.sleep_score, "", 0));
  el.healthGrid.appendChild(
    metricCard("Readiness", readinessValues, data.today.readiness_score, "", 0)
  );
  el.healthGrid.appendChild(metricCard("HRV", hrvValues, data.today.hrv_avg, "ms", 0));
  el.healthGrid.appendChild(metricCard("Ruhepuls", rhrValues, data.today.resting_hr, "bpm", 0));
  el.healthGrid.appendChild(workoutsCard(data.workouts_per_week || []));
  el.healthGrid.appendChild(proteinCard(data.meals_daily || []));
}

/* ---------------------------------------------------------------------
 * Init
 * --------------------------------------------------------------------- */

async function loadHealthOverview() {
  try {
    const res = await fetch("/api/health/overview?days=30");
    const data = await res.json();
    renderPulseHeader(data.daily || [], data.today || {});
    renderHealthGrid(data);
  } catch {
    el.healthGrid.innerHTML =
      '<p class="empty-state">Health-Daten konnten nicht geladen werden.</p>';
  }
}

async function init() {
  setAccent(state.currentAgent);
  await loadAgents();
  await loadHistory();
  await loadHealthOverview();
}

init();
