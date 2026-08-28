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
};

const RANGE_ORDER = ["1D", "1W", "1M", "3M", "6M", "1Y", "YTD", "MAX"];

const el = (id) => document.getElementById(id);

let lastPositions = [];
let currentRange = "MAX";
let historyChart = null;
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
    byDate.set(row.date, { date: row.date, equity: row.equityChf, cash: row.cashChf });
  }
  for (const row of organic || []) {
    byDate.set(row.date, { date: row.date, equity: row.equity, cash: row.cash });
  }
  return [...byDate.values()].sort((a, b) => a.date.localeCompare(b.date));
}

function filterByRange(series, range) {
  if (!series.length || range === "MAX") return series;
  const end = new Date(series[series.length - 1].date + "T00:00:00");
  const cutoff = new Date(end);
  if (range === "1D") cutoff.setDate(cutoff.getDate() - 1);
  else if (range === "1W") cutoff.setDate(cutoff.getDate() - 7);
  else if (range === "1M") cutoff.setMonth(cutoff.getMonth() - 1);
  else if (range === "3M") cutoff.setMonth(cutoff.getMonth() - 3);
  else if (range === "6M") cutoff.setMonth(cutoff.getMonth() - 6);
  else if (range === "1Y") cutoff.setFullYear(cutoff.getFullYear() - 1);
  else if (range === "YTD") { cutoff.setMonth(0); cutoff.setDate(1); }
  const cutoffIso = cutoff.toISOString().slice(0, 10);
  return series.filter((p) => p.date >= cutoffIso);
}

function renderChart(fullSeries) {
  el("panel-chart").classList.remove("hidden");
  const series = filterByRange(fullSeries, currentRange);
  const ctx = el("chart-history").getContext("2d");
  const labels = series.map((h) => h.date);
  const equity = series.map((h) => h.equity);
  const cash = series.map((h) => h.cash);

  if (historyChart) historyChart.destroy();
  historyChart = new Chart(ctx, {
    type: "line",
    data: {
      labels,
      datasets: [
        { label: "Gesamtwert (CHF)", data: equity, borderColor: "#4f8cff", backgroundColor: "rgba(79,140,255,0.15)", tension: 0.2, fill: true, pointRadius: series.length > 90 ? 0 : 2 },
        { label: "Cash / Available to Trade (CHF)", data: cash, borderColor: "#35c98f", backgroundColor: "transparent", borderDash: [4, 3], tension: 0.2, pointRadius: 0 },
      ],
    },
    options: { responsive: true, plugins: { legend: { position: "bottom" } }, scales: { y: { beginAtZero: false } } },
  });
}

function setRangeButtons() {
  document.querySelectorAll(".range-btn").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.range === currentRange);
    btn.onclick = () => {
      currentRange = btn.dataset.range;
      setRangeButtons();
      const merged = mergeHistorySeries(loadLocal(LS_KEYS.historyBackfill, {}).series, loadLocal(LS_KEYS.history, []));
      renderChart(merged);
    };
  });
}

// --- Summary / Positionen ----------------------------------------------------

function renderSummary(totals, fetchedAt) {
  el("panel-summary").classList.remove("hidden");
  el("stat-equity").textContent = fmtChf(totals.equityChf);
  el("stat-cash").textContent = fmtChf(totals.cashChf);
  el("stat-positions").textContent = fmtChf(totals.positionsValueChf);
  el("stat-updated").textContent = fetchedAt ? new Date(fetchedAt).toLocaleString("de-CH") : "–";
}

function renderPositions(positions) {
  lastPositions = positions;
  el("panel-positions").classList.remove("hidden");
  const tbody = document.querySelector("#table-positions tbody");
  tbody.innerHTML = "";
  const totalValue = positions.reduce((sum, p) => sum + (p.valueChf || 0), 0);

  for (const p of positions) {
    const tr = document.createElement("tr");
    const share = totalValue ? ((p.valueChf || 0) / totalValue) * 100 : 0;
    const pl = p.plUnrealizedChf;
    const plClass = pl > 0 ? "pl-pos" : pl < 0 ? "pl-neg" : "";
    const plPctTxt = p.plUnrealizedPct !== null && p.plUnrealizedPct !== undefined ? ` (${p.plUnrealizedPct.toFixed(1)}%)` : "";

    tr.innerHTML = `
      <td>${p.name ?? p.productId}</td>
      <td>${p.symbol ?? "–"}</td>
      <td>${p.size ?? "–"}</td>
      <td>${fmtNative(p.price, p.currency)}</td>
      <td>${fmtChf(p.valueChf)}</td>
      <td>${share.toFixed(1)}%</td>
      <td>${fmtNative(p.averagePrice, p.currency)}</td>
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
      <td>${t.total ?? "–"}</td>
    `;
    tbody.appendChild(tr);
  }
}

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

function renderModalChart(series, currency) {
  const ctx = el("modal-chart").getContext("2d");
  if (modalChart) modalChart.destroy();
  modalChart = new Chart(ctx, {
    type: "line",
    data: {
      labels: series.map((s) => s.date),
      datasets: [{
        label: `Kurs (${currency || "native"})`,
        data: series.map((s) => s.priceNative),
        borderColor: "#4f8cff",
        backgroundColor: "rgba(79,140,255,0.15)",
        tension: 0.2,
        fill: true,
        pointRadius: series.length > 90 ? 0 : 2,
      }],
    },
    options: { responsive: true, plugins: { legend: { display: false } }, scales: { y: { beginAtZero: false } } },
  });
}

function openModal() { el("modal-overlay").classList.remove("hidden"); }
function closeModal() { el("modal-overlay").classList.add("hidden"); }

async function openPositionModal(position) {
  el("modal-title").textContent = position.name || position.productId;
  el("modal-subtitle").textContent = `${position.symbol || ""} · ${position.currency || ""} · ${position.productType || ""}`;
  el("modal-isin").textContent = position.isin || "–";
  el("modal-size").textContent = position.size ?? "–";
  el("modal-price").textContent = fmtNative(position.price, position.currency);
  el("modal-value").textContent = fmtChf(position.valueChf);
  el("modal-avg").textContent = fmtNative(position.averagePrice, position.currency);
  el("modal-pl").textContent = position.plUnrealizedChf !== null && position.plUnrealizedChf !== undefined ? fmtChf(position.plUnrealizedChf) : "–";
  el("modal-verify-note").textContent = "Lade Kursverlauf...";
  openModal();

  const today = new Date().toISOString().slice(0, 10);
  const organic = appendPositionHistoryPoint(position.productId, {
    date: today, priceNative: position.price, priceChf: position.priceChf,
  });

  try {
    const data = await proxyFetch(`/api/position-history?productId=${encodeURIComponent(position.productId)}`);
    const merged = mergePositionSeries(data.series, organic);
    renderModalChart(merged, position.currency);
    el("modal-verify-note").textContent = data.verified
      ? "Kursverlauf gegen DEGIRO validiert."
      : (data.note || "Kursverlauf konnte nicht validiert werden - ggf. unvollständig/ungenau. Ab heute wird er lokal exakt weitergeführt.");
  } catch (err) {
    renderModalChart(organic, position.currency);
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

  const merged = mergeHistorySeries(backfill.series, organic);
  if (merged.length) {
    setRangeButtons();
    renderChart(merged);
    if (backfill.positions) {
      const unverified = backfill.positions.filter((p) => !p.verified).length;
      el("history-note").textContent = unverified
        ? `Hinweis: bei ${unverified} von ${backfill.positions.length} Position(en) konnte der historische Kursverlauf nicht gegen DEGIRO validiert werden (Näherung, letzter bekannter Kurs).`
        : "Kursverlauf aller Positionen gegen DEGIRO validiert.";
    }
  }
}

async function connectAndLoad() {
  const username = el("input-username").value.trim();
  const password = el("input-password").value;
  const totp = el("input-totp").value.trim();

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
      body: JSON.stringify({ username, password, totp_secret_key: totp || undefined }),
    });

    saveLocal(LS_KEYS.proxyUrl, proxyUrl());
    saveLocal(LS_KEYS.proxyToken, proxyToken());
    saveLocal(LS_KEYS.username, username);
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
  const [portfolio, transactions] = await Promise.all([
    proxyFetch("/api/portfolio"),
    proxyFetch("/api/transactions"),
  ]);

  saveLocal(LS_KEYS.portfolio, portfolio);
  saveLocal(LS_KEYS.transactions, transactions);
  const organic = appendHistoryPoint(portfolio.totals);

  renderSummary(portfolio.totals, portfolio.fetchedAt);
  renderPositions(portfolio.positions);
  renderTransactions(transactions.transactions);

  // Backfill ist rechenintensiv (Kurscharts + FX) - im Hintergrund laden,
  // damit Portfolio/Positionen sofort sichtbar sind.
  setRangeButtons();
  renderChart(mergeHistorySeries(loadLocal(LS_KEYS.historyBackfill, {}).series, organic));
  proxyFetch("/api/history/backfill")
    .then((backfill) => {
      saveLocal(LS_KEYS.historyBackfill, backfill);
      renderChart(mergeHistorySeries(backfill.series, organic));
      if (backfill.positions) {
        const unverified = backfill.positions.filter((p) => !p.verified).length;
        el("history-note").textContent = unverified
          ? `Hinweis: bei ${unverified} von ${backfill.positions.length} Position(en) konnte der historische Kursverlauf nicht gegen DEGIRO validiert werden (Näherung, letzter bekannter Kurs).`
          : "Kursverlauf aller Positionen gegen DEGIRO validiert.";
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
}

el("btn-connect").addEventListener("click", connectAndLoad);
el("btn-refresh").addEventListener("click", () => refreshData().catch((e) => setMessage("Fehler: " + e.message, "error")));
el("btn-disconnect").addEventListener("click", disconnect);
el("btn-clear").addEventListener("click", clearLocalData);

restoreInputsFromCache();
renderAllFromCache();

// Falls schon einmal verbunden: Online-Status pruefen, ohne Passwort erneut abzufragen.
if (loadLocal(LS_KEYS.proxyToken, "")) {
  proxyFetch("/api/health")
    .then((res) => setOnlineBadge(!!res.logged_in))
    .catch(() => setOnlineBadge(false));
}
