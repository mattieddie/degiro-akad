"use strict";

/*
 * Alle Daten (Proxy-Adresse/-Token, Portfolio, Transaktionen, Verlauf) werden
 * ausschliesslich im localStorage dieses Browsers gehalten. Nichts davon wird
 * jemals an GitHub oder einen anderen Server als den lokalen Proxy gesendet.
 * Das DEGIRO-Passwort selbst wird NIE gespeichert - es wird bei jedem
 * "Verbinden" neu eingegeben und nur an den lokalen Proxy uebermittelt.
 *
 * Alle Geldbetraege werden vom Proxy bereits in CHF umgerechnet geliefert.
 *
 * Verlauf ("seit Kauf"): Der Proxy rekonstruiert beim Verbinden einen
 * Best-Effort-Verlauf aus Transaktionshistorie + DEGIRO-Kurscharts
 * (/api/history/backfill). Dieser wird lokal mit echten taeglichen
 * Schnappschuessen ueberlagert, die bei jedem Seitenaufruf mit laufendem
 * Proxy entstehen - fuer bereits erfasste Tage gewinnt immer der exakte,
 * lokal gemessene Wert.
 */

const LS_KEYS = {
  proxyUrl: "degiro_proxy_url",
  proxyToken: "degiro_proxy_token",
  username: "degiro_username",
  portfolio: "degiro_portfolio",
  transactions: "degiro_transactions",
  history: "degiro_history",
  historyBackfill: "degiro_history_backfill",
  positionHistory: "degiro_position_history",
  closedPositions: "degiro_closed_positions",
  showClosed: "degiro_show_closed",
  fmpKey: "degiro_fmp_key",
};

const RANGE_ORDER = ["1D", "1W", "1M", "3M", "6M", "1Y", "YTD", "MAX"];

const el = (id) => document.getElementById(id);

let lastPositions = [];
let currentRange = "MAX";
let currentMode = "value";
let historyChart = null;
let lastTotals = null;
let latestNetDeposits = null;
let plTotalMode = "unrealized";
let modalChart = null;

function loadLocal(key, fallback) {
  try {
    const raw = localStorage.getItem(key);
    return raw ? JSON.parse(raw) : fallback;
  } catch {
    return fallback;
  }
}

function saveLocal(key, value) {
  localStorage.setItem(key, JSON.stringify(value));
}

function proxyUrl() {
  return (el("input-proxy-url").value || "http://127.0.0.1:8765").replace(/\/$/, "");
}

function proxyToken() {
  return el("input-proxy-token").value.trim();
}

function setMessage(text, kind) {
  const node = el("connect-message");
  node.textContent = text;
  node.className = "message" + (kind ? " " + kind : "");
}

function fmtChf(value) {
  if (value === null || value === undefined || Number.isNaN(value)) return "–";
  try {
    return new Intl.NumberFormat("de-CH", { style: "currency", currency: "CHF" }).format(value);
  } catch {
    return `CHF ${value.toFixed(2)}`;
  }
}

function fmtNative(value, currency) {
  if (value === null || value === undefined || Number.isNaN(value)) return "–";
  try {
    return new Intl.NumberFormat("de-CH", { style: "currency", currency: currency || "EUR" }).format(value);
  } catch {
    return `${value.toFixed(2)} ${currency || ""}`;
  }
}

async function proxyFetch(path, options = {}) {
  const res = await fetch(proxyUrl() + path, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      Authorization: "Bearer " + proxyToken(),
      ...(options.headers || {}),
    },
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    throw new Error(body.error || `Proxy-Fehler (HTTP ${res.status})`);
  }
  return body;
}

function setOnlineBadge(online) {
  const badge = el("status-badge");
  badge.textContent = online ? "Verbunden" : "Nicht verbunden";
  badge.className = "badge " + (online ? "badge--online" : "badge--offline");
  el("btn-refresh").disabled = !online;
  el("btn-disconnect").disabled = !online;
}

// --- Portfolio-Verlauf (Gesamt) --------------------------------------------

function appendHistoryPoint(totals) {
  const history = loadLocal(LS_KEYS.history, []);
  const today = new Date().toISOString().slice(0, 10);
  const point = { date: today, equity: totals.equityChf, cash: totals.cashChf };
  const idx = history.findIndex((h) => h.date === today);
  if (idx >= 0) history[idx] = point;
  else history.push(point);
  history.sort((a, b) => a.date.localeCompare(b.date));
  saveLocal(LS_KEYS.history, history);
  return history;
}

function mergeHistorySeries(backfill, organic) {
  const byDate = new Map();
  for (const row of backfill || []) {
    byDate.set(row.date, { date: row.date, equity: row.equityChf, cash: row.cashChf, netDeposits: row.netDepositsChf });
  }
  for (const row of organic || []) {
    // netDeposits kommt nur vom Backfill (Proxy) - beim Ueberschreiben mit dem
    // exakten lokalen Tages-Snapshot fuer denselben Tag uebernehmen, sonst
    // reisst die einzahlungsbereinigte %-Kurve an dieser Stelle ab.
    const existing = byDate.get(row.date);
    byDate.set(row.date, { date: row.date, equity: row.equity, cash: row.cash, netDeposits: existing ? existing.netDeposits : undefined });
  }
  return [...byDate.values()].sort((a, b) => a.date.localeCompare(b.date));
}

function filterByRange(series, range) {
  if (!series.length || range === "MAX") return series;
  // Reine "YYYY-MM-DD"-Strings werden von Date() als UTC-Mitternacht geparst;
  // ab hier durchgehend UTC-Getter/Setter verwenden, sonst verschiebt sich der
  // Zeitraum je nach Zeitzone des Betrachters um einen Tag.
  const end = new Date(series[series.length - 1].date);
  const cutoff = new Date(end);
  if (range === "1D") cutoff.setUTCDate(cutoff.getUTCDate() - 1);
  else if (range === "1W") cutoff.setUTCDate(cutoff.getUTCDate() - 7);
  else if (range === "1M") cutoff.setUTCMonth(cutoff.getUTCMonth() - 1);
  else if (range === "3M") cutoff.setUTCMonth(cutoff.getUTCMonth() - 3);
  else if (range === "6M") cutoff.setUTCMonth(cutoff.getUTCMonth() - 6);
  else if (range === "1Y") cutoff.setUTCFullYear(cutoff.getUTCFullYear() - 1);
  else if (range === "YTD") { cutoff.setUTCMonth(0); cutoff.setUTCDate(1); }
  const cutoffIso = cutoff.toISOString().slice(0, 10);
  return series.filter((p) => p.date >= cutoffIso);
}

function computeRebasedPerformance(series) {
  // G(t) = (Depotwert - kumulierte Netto-Einzahlungen) / kumulierte Netto-Einzahlungen.
  // Das ist bereits einzahlungsbereinigt (eine Einzahlung + Kauf veraendert G
  // nicht sprunghaft). Fuer die Anzeige wird die Kurve zusaetzlich relativ
  // zum ersten Punkt im gewaehlten Zeitraum reskaliert, damit sie dort IMMER
  // bei 0% beginnt - ohne die Einzahlungsbereinigung zu verlieren (analog zu
  // einer verketteten Zeitgewichteten Rendite zwischen zwei Zeitpunkten).
  const g = series.map((h) => {
    if (h.netDeposits === null || h.netDeposits === undefined || h.netDeposits <= 0) return null;
    if (h.equity === null || h.equity === undefined) return null;
    return (h.equity - h.netDeposits) / h.netDeposits;
  });
  const baseIdx = g.findIndex((v) => v !== null);
  if (baseIdx === -1) return series.map(() => null);
  const base = 1 + g[baseIdx];
  return g.map((v) => (v === null ? null : ((1 + v) / base - 1) * 100));
}

function updatePerfBadge(pctSeries, badgeId = "chart-perf-badge") {
  const badge = el(badgeId);
  let last = null;
  for (let i = pctSeries.length - 1; i >= 0; i--) {
    if (pctSeries[i] !== null && pctSeries[i] !== undefined) { last = pctSeries[i]; break; }
  }
  if (last === null) {
    badge.textContent = "";
    badge.className = "perf-badge";
    return;
  }
  const sign = last > 0 ? "+" : "";
  badge.textContent = `${sign}${last.toFixed(2)}%`;
  badge.className = "perf-badge " + (last > 0 ? "perf-pos" : last < 0 ? "perf-neg" : "");
}

// Chart.js' eingebauter "nice number"-Algorithmus fuer die Y-Achse rundet die
// Grenzen auf Vielfache einer Schrittweite, die aus dem GESAMTEN Datenbereich
// ueber alle Datasets hinweg abgeleitet wird - kombiniert man eine Kurve im
// Bereich von z.B. 43'000 mit einer nahe 0 (Cash-Linie), landet die Schrittweite
// bei z.B. 10'000 und die Achse wird bis 0 bzw. weit darueber hinaus aufgerundet,
// selbst wenn sich die eigentlichen Werte im gewaehlten Zeitraum kaum bewegen
// (z.B. 1 Woche). Deshalb werden min/max hier explizit aus den tatsaechlich
// sichtbaren Werten berechnet, mit etwas Rand oben/unten.
function computeYRange(valueArrays, paddingRatio = 0.08) {
  let min = Infinity;
  let max = -Infinity;
  for (const arr of valueArrays) {
    for (const v of arr) {
      if (v === null || v === undefined || Number.isNaN(v)) continue;
      if (v < min) min = v;
      if (v > max) max = v;
    }
  }
  if (!Number.isFinite(min) || !Number.isFinite(max)) return undefined;
  if (min === max) { min -= 1; max += 1; }
  const pad = (max - min) * paddingRatio;
  return { min: min - pad, max: max + pad };
}

// Formatiert ein "YYYY-MM-DD"-Datum fuer die X-Achse abhaengig vom gewaehlten
// Zeitraum - je kuerzer der Zeitraum, desto feiner die Aufloesung (Tag/Monat),
// je laenger, desto grober (Monat/Jahr), statt ueberall das volle ISO-Datum
// anzuzeigen.
function formatDateLabel(dateStr, range) {
  const d = new Date(dateStr + "T00:00:00Z");
  if (range === "1D" || range === "1W" || range === "1M") {
    return new Intl.DateTimeFormat("de-CH", { day: "2-digit", month: "2-digit", timeZone: "UTC" }).format(d);
  }
  if (range === "3M" || range === "6M") {
    return new Intl.DateTimeFormat("de-CH", { day: "2-digit", month: "short", timeZone: "UTC" }).format(d);
  }
  return new Intl.DateTimeFormat("de-CH", { month: "short", year: "numeric", timeZone: "UTC" }).format(d);
}

function xAxisOptions() {
  return {
    ticks: {
      maxTicksLimit: 8,
      autoSkip: true,
      callback: function (value) {
        return formatDateLabel(this.getLabelForValue(value), currentRange);
      },
    },
  };
}

function latestDefined(series, key) {
  for (let i = series.length - 1; i >= 0; i--) {
    const v = series[i][key];
    if (v !== null && v !== undefined) return v;
  }
  return null;
}

function renderChart(fullSeries) {
  el("panel-chart").classList.remove("hidden");
  // Unabhaengig vom gewaehlten Chart-Zeitraum: die aktuellsten bekannten
  // kumulierten Netto-Einzahlungen fuer die "Seit Einzahlung"-Ansicht des
  // Total-P/L-Kaestchens (siehe renderPlTotalStat).
  latestNetDeposits = latestDefined(fullSeries, "netDeposits");
  renderPlTotalStat();
  const series = filterByRange(fullSeries, currentRange);
  const ctx = el("chart-history").getContext("2d");
  const labels = series.map((h) => h.date);
  const rebasedPct = computeRebasedPerformance(series);
  updatePerfBadge(rebasedPct);

  if (historyChart) historyChart.destroy();

  if (currentMode === "percent") {
    el("chart-title").textContent = "Performance seit Range-Start, einzahlungsbereinigt (%)";
    const pct = rebasedPct;
    const yRange = computeYRange([pct]);
    historyChart = new Chart(ctx, {
      type: "line",
      data: {
        labels,
        datasets: [
          { label: "Performance (%)", data: pct, borderColor: "#4f8cff", backgroundColor: "rgba(79,140,255,0.15)", tension: 0.2, fill: true, pointRadius: series.length > 90 ? 0 : 2 },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { position: "bottom" } },
        scales: {
          x: xAxisOptions(),
          y: { ...yRange, ticks: { callback: (v) => v.toFixed(1) + "%" } },
        },
      },
    });
  } else {
    el("chart-title").textContent = "Performance über Zeit (CHF)";
    const equity = series.map((h) => h.equity);
    const cash = series.map((h) => h.cash);
    // Cash liegt meist nahe 0 und Gesamtwert im Tausenderbereich - auf einer
    // gemeinsamen Achse wuerde die Cash-Linie die Skala bis in die Naehe von 0
    // ziehen, wodurch die eigentlich interessanten (oft kleinen) Schwankungen
    // des Gesamtwerts kaum noch sichtbar waeren. Deshalb bekommt Cash eine
    // eigene, unsichtbare Sekundaerachse ("y1") - jede Linie wird auf ihrer
    // eigenen Skala eng an ihre tatsaechlichen Werte herangezoomt.
    const yRange = computeYRange([equity]);
    const yCashRange = computeYRange([cash]);
    historyChart = new Chart(ctx, {
      type: "line",
      data: {
        labels,
        datasets: [
          { label: "Gesamtwert (CHF)", data: equity, borderColor: "#4f8cff", backgroundColor: "rgba(79,140,255,0.15)", tension: 0.2, fill: true, pointRadius: series.length > 90 ? 0 : 2, yAxisID: "y" },
          { label: "Cash / Available to Trade (CHF)", data: cash, borderColor: "#35c98f", backgroundColor: "transparent", borderDash: [4, 3], tension: 0.2, pointRadius: 0, yAxisID: "y1" },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { position: "bottom" } },
        scales: {
          x: xAxisOptions(),
          y: { beginAtZero: false, ...yRange },
          y1: { display: false, beginAtZero: false, ...yCashRange },
        },
      },
    });
  }
}

function currentMergedHistory() {
  return mergeHistorySeries(loadLocal(LS_KEYS.historyBackfill, {}).series, loadLocal(LS_KEYS.history, []));
}

function buildHistoryNoteText(positions) {
  if (!positions || !positions.length) return "";
  const degiro = positions.filter((p) => p.used && p.source === "degiro").length;
  const yahoo = positions.filter((p) => p.used && p.source === "yahoo").length;
  const fmp = positions.filter((p) => p.used && p.source === "fmp").length;
  const none = positions.filter((p) => !p.used).length;
  const unverifiedUsed = positions.filter((p) => p.used && !p.verified).length;
  const parts = [];
  if (degiro) parts.push(`${degiro} von DEGIRO`);
  if (yahoo) parts.push(`${yahoo} von Yahoo Finance`);
  if (fmp) parts.push(`${fmp} von Financial Modeling Prep`);
  if (none) parts.push(`${none} ohne jegliche Kursdaten (Näherung, letzter bekannter Kurs, flach)`);
  let text = `Kursquellen: ${parts.join(", ")}.`;
  if (unverifiedUsed) {
    text += ` Davon ${unverifiedUsed} nicht exakt gegen DEGIROs Schlusskurs abgeglichen, trotzdem verwendet.`;
  }
  return text;
}

function setRangeButtons() {
  document.querySelectorAll("#range-buttons .range-btn").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.range === currentRange);
    btn.onclick = () => {
      currentRange = btn.dataset.range;
      setRangeButtons();
      renderChart(currentMergedHistory());
    };
  });
}

function setModeButtons() {
  document.querySelectorAll("#mode-buttons .range-btn").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.mode === currentMode);
    btn.onclick = () => {
      currentMode = btn.dataset.mode;
      setModeButtons();
      renderChart(currentMergedHistory());
    };
  });
}

// --- Summary / Positionen ----------------------------------------------------

function setPlStat(id, value, pct) {
  const target = el(id);
  const pctTxt = pct !== null && pct !== undefined && !Number.isNaN(pct) ? ` (${pct > 0 ? "+" : ""}${pct.toFixed(1)}%)` : "";
  target.textContent = value !== null && value !== undefined ? fmtChf(value) + pctTxt : "–";
  target.className = "stat-value " + (value > 0 ? "pl-pos" : value < 0 ? "pl-neg" : "");
}

// Total-P/L-Kaestchen ist umschaltbar zwischen dem unrealisierten G/V der
// offenen Positionen (aus /api/portfolio) und der Gesamtperformance seit
// der allerersten Einzahlung (Gesamtwert minus kumulierte Netto-Einzahlungen,
// dieselbe einzahlungsbereinigte Basis wie die Prozent-Performance im Chart -
// siehe latestNetDeposits in renderChart). Beide Werte werden unabhaengig
// voneinander aktualisiert (Portfolio vs. Verlauf/Backfill laden getrennt),
// daher zeigt renderPlTotalStat immer den zuletzt bekannten Stand beider an.
function renderPlTotalStat() {
  if (plTotalMode === "deposits") {
    // G(t) = (Depotwert - Netto-Einzahlungen) / Netto-Einzahlungen, siehe README -
    // derselbe einzahlungsbereinigte Prozentsatz wie im Performance-Chart, hier aber
    // nicht auf einen Range-Start rebasiert, sondern seit der allerersten Einzahlung.
    const value = (lastTotals && lastTotals.equityChf != null && latestNetDeposits != null)
      ? lastTotals.equityChf - latestNetDeposits
      : null;
    const pct = (value !== null && latestNetDeposits) ? (value / latestNetDeposits) * 100 : null;
    setPlStat("stat-pl-total", value, pct);
  } else {
    // Prozentsatz relativ zum Einstand (Kostenbasis) der offenen Positionen:
    // Kostenbasis = aktueller Positionswert - unrealisierter G/V.
    const value = lastTotals ? lastTotals.plTotalChf : null;
    const costBasis = (lastTotals && value !== null) ? lastTotals.positionsValueChf - value : null;
    const pct = (costBasis) ? (value / costBasis) * 100 : null;
    setPlStat("stat-pl-total", value, pct);
  }
}

document.querySelectorAll("#pl-total-toggle .stat-toggle-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    plTotalMode = btn.dataset.plMode;
    document.querySelectorAll("#pl-total-toggle .stat-toggle-btn").forEach((b) => b.classList.toggle("active", b === btn));
    renderPlTotalStat();
  });
});

function renderSummary(totals, fetchedAt) {
  el("panel-summary").classList.remove("hidden");
  lastTotals = totals;
  el("stat-equity").textContent = fmtChf(totals.equityChf);
  el("stat-cash").textContent = fmtChf(totals.cashChf);
  el("stat-positions").textContent = fmtChf(totals.positionsValueChf);
  renderPlTotalStat();
  setPlStat("stat-pl-day", totals.plDayChf);
  el("stat-updated").textContent = fetchedAt ? new Date(fetchedAt).toLocaleString("de-CH") : "–";
}

function transactionsForProduct(productId) {
  const cached = loadLocal(LS_KEYS.transactions, null);
  const all = (cached && cached.transactions) || [];
  return all
    .filter((t) => String(t.productId) === String(productId))
    .sort((a, b) => (b.date || "").localeCompare(a.date || ""));
}

function totalFeesChfForProduct(productId) {
  return transactionsForProduct(productId).reduce((sum, t) => sum + (t.feesChf || 0), 0);
}

const POSITION_SORT_ACCESSORS = {
  name: (p) => (p.name ?? String(p.productId ?? "")).toLowerCase(),
  symbol: (p) => (p.symbol ?? "").toLowerCase(),
  size: (p) => p.size ?? 0,
  // "Kurs" wird pro Position in Originalwaehrung angezeigt (fmtNative) - fuer
  // eine sinnvolle Sortierung ueber Positionen in verschiedenen Waehrungen
  // hinweg wird trotzdem der CHF-Gegenwert verglichen, nicht der rohe Kurs.
  price: (p) => p.priceChf ?? 0,
  value: (p) => p.valueChf ?? 0,
  // "Anteil" ist eine reine Funktion von valueChf (Anteil am Gesamtwert) -
  // sortiert daher zwangslaeufig identisch zu "Wert (CHF)".
  share: (p) => p.valueChf ?? 0,
  avgPrice: (p) => p.averagePriceChf ?? 0,
  fees: (p) => totalFeesChfForProduct(p.productId) ?? 0,
  pl: (p) => p.plUnrealizedChf ?? 0,
};

let positionsSortKey = null;
let positionsSortDir = "desc";

function sortedPositions(positions) {
  if (!positionsSortKey) return positions;
  const accessor = POSITION_SORT_ACCESSORS[positionsSortKey];
  const dir = positionsSortDir === "asc" ? 1 : -1;
  return [...positions].sort((a, b) => {
    const av = accessor(a);
    const bv = accessor(b);
    if (av < bv) return -1 * dir;
    if (av > bv) return 1 * dir;
    return 0;
  });
}

function updatePositionsSortIndicators() {
  document.querySelectorAll("#table-positions thead th[data-sort]").forEach((th) => {
    const active = th.dataset.sort === positionsSortKey;
    th.classList.toggle("sort-active", active);
    th.classList.remove("sort-asc", "sort-desc");
    if (active) th.classList.add(positionsSortDir === "asc" ? "sort-asc" : "sort-desc");
  });
}

document.querySelectorAll("#table-positions thead th[data-sort]").forEach((th) => {
  th.addEventListener("click", () => {
    const key = th.dataset.sort;
    positionsSortDir = positionsSortKey === key && positionsSortDir === "desc" ? "asc" : "desc";
    positionsSortKey = key;
    updatePositionsSortIndicators();
    renderPositions(lastPositions);
  });
});

function renderPositions(positions) {
  lastPositions = positions;
  el("panel-positions").classList.remove("hidden");
  const tbody = document.querySelector("#table-positions tbody");
  tbody.innerHTML = "";
  const totalValue = positions.reduce((sum, p) => sum + (p.valueChf || 0), 0);
  updatePositionsSortIndicators();

  for (const p of sortedPositions(positions)) {
    const tr = document.createElement("tr");
    const share = totalValue ? ((p.valueChf || 0) / totalValue) * 100 : 0;
    const pl = p.plUnrealizedChf;
    const plClass = pl > 0 ? "pl-pos" : pl < 0 ? "pl-neg" : "";
    const plPctTxt = p.plUnrealizedPct !== null && p.plUnrealizedPct !== undefined ? ` (${p.plUnrealizedPct.toFixed(1)}%)` : "";
    const feesChf = totalFeesChfForProduct(p.productId);

    tr.innerHTML = `
      <td>${p.name ?? p.productId}</td>
      <td>${p.symbol ?? "–"}</td>
      <td>${p.size ?? "–"}</td>
      <td>${fmtNative(p.price, p.currency)}</td>
      <td>${fmtChf(p.valueChf)}</td>
      <td>${share.toFixed(1)}%</td>
      <td>${fmtNative(p.averagePrice, p.currency)}</td>
      <td>${fmtChf(feesChf)}</td>
      <td class="${plClass}">${pl !== undefined && pl !== null ? fmtChf(pl) + plPctTxt : "–"}</td>
    `;
    tr.addEventListener("click", () => openPositionModal(p));
    tbody.appendChild(tr);
  }
}

function renderTransactions(transactions) {
  el("panel-transactions").classList.remove("hidden");
  const tbody = document.querySelector("#table-transactions tbody");
  tbody.innerHTML = "";
  const sorted = [...transactions].sort((a, b) => (b.date || "").localeCompare(a.date || "")).slice(0, 50);

  for (const t of sorted) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${t.date ? new Date(t.date).toLocaleDateString("de-CH") : "–"}</td>
      <td>${t.productId ?? "–"}</td>
      <td>${t.buysell ?? "–"}</td>
      <td>${t.quantity ?? "–"}</td>
      <td>${t.price ?? "–"}</td>
      <td>${fmtChf(t.feesChf)}</td>
      <td>${fmtChf(t.totalPlusFeesChf)}</td>
    `;
    tbody.appendChild(tr);
  }
}

// --- Geschlossene Positionen ---------------------------------------------------

function renderClosedPositions(positions) {
  const section = el("panel-closed");
  if (!positions || !positions.length) {
    section.classList.add("hidden");
    return;
  }
  section.classList.remove("hidden");

  const show = loadLocal(LS_KEYS.showClosed, true);
  el("toggle-closed").checked = show;
  el("closed-table-wrap").classList.toggle("hidden", !show);

  const tbody = document.querySelector("#table-closed tbody");
  tbody.innerHTML = "";
  for (const p of positions) {
    const tr = document.createElement("tr");
    const plClass = p.realizedPlChf > 0 ? "pl-pos" : p.realizedPlChf < 0 ? "pl-neg" : "";
    const plPctTxt = p.realizedPlPct !== null && p.realizedPlPct !== undefined ? ` (${p.realizedPlPct.toFixed(1)}%)` : "";
    tr.innerHTML = `
      <td>${p.name ?? p.productId} <span class="tag">geschlossen</span></td>
      <td>${p.symbol ?? "–"}</td>
      <td>${p.firstDate ?? "–"} – ${p.lastDate ?? "–"}</td>
      <td>${fmtChf(p.investedChf)}</td>
      <td>${fmtChf(p.proceedsChf)}</td>
      <td class="${plClass}">${fmtChf(p.realizedPlChf)}${plPctTxt}</td>
    `;
    tbody.appendChild(tr);
  }
}

el("toggle-closed").addEventListener("change", (e) => {
  saveLocal(LS_KEYS.showClosed, e.target.checked);
  el("closed-table-wrap").classList.toggle("hidden", !e.target.checked);
});

// --- Positions-Popup ----------------------------------------------------------

function appendPositionHistoryPoint(productId, point) {
  const all = loadLocal(LS_KEYS.positionHistory, {});
  const list = all[productId] || [];
  const idx = list.findIndex((h) => h.date === point.date);
  if (idx >= 0) list[idx] = point;
  else list.push(point);
  list.sort((a, b) => a.date.localeCompare(b.date));
  all[productId] = list;
  saveLocal(LS_KEYS.positionHistory, all);
  return list;
}

function mergePositionSeries(backendSeries, organic) {
  const byDate = new Map();
  for (const row of backendSeries || []) {
    byDate.set(row.date, { date: row.date, priceNative: row.priceNative, priceChf: row.priceChf });
  }
  for (const row of organic || []) {
    byDate.set(row.date, { date: row.date, priceNative: row.priceNative, priceChf: row.priceChf });
  }
  return [...byDate.values()].sort((a, b) => a.date.localeCompare(b.date));
}

let modalFullSeries = [];
let modalCurrency = null;
let modalMode = "value";
let modalRange = "MAX";

function renderModalChart() {
  const series = filterByRange(modalFullSeries, modalRange);
  const ctx = el("modal-chart").getContext("2d");
  if (modalChart) modalChart.destroy();

  const basePoint = series.find((s) => s.priceNative !== null && s.priceNative !== undefined && s.priceNative !== 0);
  const base = basePoint ? basePoint.priceNative : null;
  const pct = series.map((s) => (base && s.priceNative !== null && s.priceNative !== undefined ? ((s.priceNative / base) - 1) * 100 : null));
  updatePerfBadge(pct, "modal-perf-badge");

  if (modalMode === "percent") {
    modalChart = new Chart(ctx, {
      type: "line",
      data: { labels: series.map((s) => s.date), datasets: [{
        label: "Performance (%) seit Range-Start",
        data: pct, borderColor: "#4f8cff", backgroundColor: "rgba(79,140,255,0.15)",
        tension: 0.2, fill: true, pointRadius: series.length > 90 ? 0 : 2,
      }] },
      options: { responsive: true, plugins: { legend: { display: false } }, scales: { y: { ticks: { callback: (v) => v.toFixed(1) + "%" } } } },
    });
  } else {
    modalChart = new Chart(ctx, {
      type: "line",
      data: { labels: series.map((s) => s.date), datasets: [{
        label: `Kurs (${modalCurrency || "native"})`,
        data: series.map((s) => s.priceNative),
        borderColor: "#4f8cff", backgroundColor: "rgba(79,140,255,0.15)",
        tension: 0.2, fill: true, pointRadius: series.length > 90 ? 0 : 2,
      }] },
      options: { responsive: true, plugins: { legend: { display: false } }, scales: { y: { beginAtZero: false } } },
    });
  }
}

function setModalModeButtons() {
  document.querySelectorAll("#modal-mode-buttons .range-btn").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.mode === modalMode);
    btn.onclick = () => {
      modalMode = btn.dataset.mode;
      setModalModeButtons();
      renderModalChart();
    };
  });
}

function setModalRangeButtons() {
  document.querySelectorAll("#modal-range-buttons .range-btn").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.range === modalRange);
    btn.onclick = () => {
      modalRange = btn.dataset.range;
      setModalRangeButtons();
      renderModalChart();
    };
  });
}

function openModal() { el("modal-overlay").classList.remove("hidden"); }
function closeModal() { el("modal-overlay").classList.add("hidden"); }

function renderModalTransactions(productId) {
  const forPosition = transactionsForProduct(productId);

  const totalFeesChf = totalFeesChfForProduct(productId);
  el("modal-fees").textContent = forPosition.length ? fmtChf(totalFeesChf) : "–";

  const tbody = document.querySelector("#table-modal-transactions tbody");
  tbody.innerHTML = "";
  for (const t of forPosition) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${t.date ? new Date(t.date).toLocaleDateString("de-CH") : "–"}</td>
      <td>${t.buysell ?? "–"}</td>
      <td>${t.quantity ?? "–"}</td>
      <td>${t.price ?? "–"}</td>
      <td>${fmtChf(t.feesChf)}</td>
      <td>${fmtChf(t.totalPlusFeesChf)}</td>
    `;
    tbody.appendChild(tr);
  }
}

async function openPositionModal(position) {
  modalMode = "value";
  modalRange = "MAX";
  modalCurrency = position.currency;
  el("modal-title").textContent = position.name || position.productId;
  el("modal-subtitle").textContent = `${position.symbol || ""} · ${position.currency || ""} · ${position.productType || ""}`;
  el("modal-isin").textContent = position.isin || "–";
  el("modal-size").textContent = position.size ?? "–";
  el("modal-price").textContent = fmtNative(position.price, position.currency);
  el("modal-value").textContent = fmtChf(position.valueChf);
  el("modal-avg").textContent = fmtNative(position.averagePrice, position.currency);
  const plPctTxt = position.plUnrealizedPct !== null && position.plUnrealizedPct !== undefined ? ` (${position.plUnrealizedPct.toFixed(1)}%)` : "";
  const modalPlEl = el("modal-pl");
  modalPlEl.textContent = position.plUnrealizedChf !== null && position.plUnrealizedChf !== undefined ? fmtChf(position.plUnrealizedChf) + plPctTxt : "–";
  modalPlEl.className = "stat-value small " + (position.plUnrealizedChf > 0 ? "pl-pos" : position.plUnrealizedChf < 0 ? "pl-neg" : "");
  el("modal-verify-note").textContent = "Lade Kursverlauf...";
  renderModalTransactions(position.productId);
  setModalModeButtons();
  setModalRangeButtons();
  openModal();

  const today = new Date().toISOString().slice(0, 10);
  const organic = appendPositionHistoryPoint(position.productId, {
    date: today, priceNative: position.price, priceChf: position.priceChf,
  });

  try {
    const data = await proxyFetch(`/api/position-history?productId=${encodeURIComponent(position.productId)}`);
    modalFullSeries = mergePositionSeries(data.series, organic);
    renderModalChart();
    el("modal-verify-note").textContent = data.note || "";
  } catch (err) {
    modalFullSeries = organic;
    renderModalChart();
    el("modal-verify-note").textContent = "Historischer Verlauf nicht verfügbar (Proxy nicht erreichbar?). Zeige nur lokal erfasste Werte: " + err.message;
  }
}

el("modal-close").addEventListener("click", closeModal);
el("modal-overlay").addEventListener("click", (e) => { if (e.target.id === "modal-overlay") closeModal(); });

// --- Laden aus Cache / Verbindung --------------------------------------------

function renderAllFromCache() {
  const portfolio = loadLocal(LS_KEYS.portfolio, null);
  const transactions = loadLocal(LS_KEYS.transactions, null);
  const organic = loadLocal(LS_KEYS.history, []);
  const backfill = loadLocal(LS_KEYS.historyBackfill, {});

  if (portfolio) {
    renderSummary(portfolio.totals, portfolio.fetchedAt);
    renderPositions(portfolio.positions);
  }
  if (transactions) renderTransactions(transactions.transactions);
  const closed = loadLocal(LS_KEYS.closedPositions, null);
  if (closed) renderClosedPositions(closed.positions);

  const merged = mergeHistorySeries(backfill.series, organic);
  if (merged.length) {
    setRangeButtons();
    setModeButtons();
    renderChart(merged);
    if (backfill.positions) {
      el("history-note").textContent = buildHistoryNoteText(backfill.positions);
    }
  }
}

async function connectAndLoad() {
  const username = el("input-username").value.trim();
  const password = el("input-password").value;
  const totp = el("input-totp").value.trim();
  const fmpKey = el("input-fmp-key").value.trim();

  if (!proxyToken()) {
    setMessage("Bitte das Proxy-Token eintragen (wird beim Start von proxy.py angezeigt).", "error");
    return;
  }
  if (!username || !password) {
    setMessage("Benutzername und Passwort werden benoetigt (werden nicht gespeichert).", "error");
    return;
  }

  setMessage("Verbinde mit DEGIRO ueber den lokalen Proxy...", "");
  el("btn-connect").disabled = true;

  try {
    await proxyFetch("/api/login", {
      method: "POST",
      body: JSON.stringify({ username, password, totp_secret_key: totp || undefined, fmp_api_key: fmpKey || undefined }),
    });

    saveLocal(LS_KEYS.proxyUrl, proxyUrl());
    saveLocal(LS_KEYS.proxyToken, proxyToken());
    saveLocal(LS_KEYS.username, username);
    if (fmpKey) saveLocal(LS_KEYS.fmpKey, fmpKey);
    el("input-password").value = "";

    setOnlineBadge(true);
    setMessage("Verbunden. Lade Portfolio, Transaktionen und Verlauf...", "ok");

    await refreshData();
    setMessage("Daten aktualisiert.", "ok");
  } catch (err) {
    setOnlineBadge(false);
    setMessage(
      "Fehler: " + err.message + " (laeuft der lokale Proxy? Ist das Token korrekt? Falls DEGIRO eine " +
      "Bestaetigung in der App verlangt hat, bitte in der DEGIRO-App bestaetigen und erneut versuchen.)",
      "error"
    );
  } finally {
    el("btn-connect").disabled = false;
  }
}

async function refreshData() {
  const [portfolio, transactions, closed] = await Promise.all([
    proxyFetch("/api/portfolio"),
    proxyFetch("/api/transactions"),
    proxyFetch("/api/closed-positions"),
  ]);

  saveLocal(LS_KEYS.portfolio, portfolio);
  saveLocal(LS_KEYS.transactions, transactions);
  saveLocal(LS_KEYS.closedPositions, closed);
  const organic = appendHistoryPoint(portfolio.totals);

  renderSummary(portfolio.totals, portfolio.fetchedAt);
  renderPositions(portfolio.positions);
  renderTransactions(transactions.transactions);
  renderClosedPositions(closed.positions);

  // Backfill ist rechenintensiv (Kurscharts + FX) - im Hintergrund laden,
  // damit Portfolio/Positionen sofort sichtbar sind.
  setRangeButtons();
  setModeButtons();
  renderChart(mergeHistorySeries(loadLocal(LS_KEYS.historyBackfill, {}).series, organic));
  proxyFetch("/api/history/backfill")
    .then((backfill) => {
      saveLocal(LS_KEYS.historyBackfill, backfill);
      renderChart(mergeHistorySeries(backfill.series, organic));
      if (backfill.positions) {
        el("history-note").textContent = buildHistoryNoteText(backfill.positions);
      }
    })
    .catch((err) => {
      el("history-note").textContent = "Seit-Kauf-Rekonstruktion fehlgeschlagen, zeige nur lokal erfasste Tage: " + err.message;
    });
}

async function disconnect() {
  try {
    await proxyFetch("/api/logout", { method: "POST" });
  } catch {
    /* Proxy evtl. schon nicht mehr erreichbar - egal, wir trennen lokal. */
  }
  setOnlineBadge(false);
  setMessage("Getrennt. Lokal zwischengespeicherte Daten bleiben bis zum Loeschen erhalten.", "");
}

function clearLocalData() {
  if (!confirm("Alle lokal gespeicherten Daten (Proxy-Zugang, Portfolio, Transaktionen, Verlauf) in diesem Browser loeschen?")) return;
  Object.values(LS_KEYS).forEach((k) => localStorage.removeItem(k));
  location.reload();
}

function restoreInputsFromCache() {
  el("input-proxy-url").value = loadLocal(LS_KEYS.proxyUrl, "http://127.0.0.1:8765");
  el("input-proxy-token").value = loadLocal(LS_KEYS.proxyToken, "");
  el("input-username").value = loadLocal(LS_KEYS.username, "");
  el("input-fmp-key").value = loadLocal(LS_KEYS.fmpKey, "");
}

el("btn-connect").addEventListener("click", connectAndLoad);
el("btn-refresh").addEventListener("click", () => refreshData().catch((e) => setMessage("Fehler: " + e.message, "error")));
el("btn-disconnect").addEventListener("click", disconnect);
el("btn-clear").addEventListener("click", clearLocalData);

el("chart-info-btn").addEventListener("click", () => {
  const popover = el("chart-info-popover");
  const nowHidden = popover.classList.toggle("hidden");
  el("chart-info-btn").classList.toggle("active", !nowHidden);
  el("chart-info-btn").setAttribute("aria-expanded", String(!nowHidden));
});

restoreInputsFromCache();
renderAllFromCache();

// Falls schon einmal verbunden: Online-Status pruefen, ohne Passwort erneut abzufragen.
if (loadLocal(LS_KEYS.proxyToken, "")) {
  proxyFetch("/api/health")
    .then((res) => setOnlineBadge(!!res.logged_in))
    .catch(() => setOnlineBadge(false));
}
