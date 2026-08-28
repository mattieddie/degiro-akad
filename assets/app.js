"use strict";

/*
 * Alle Daten (Proxy-Adresse/-Token, Portfolio, Transaktionen, Verlauf) werden
 * ausschliesslich im localStorage dieses Browsers gehalten. Nichts davon wird
 * jemals an GitHub oder einen anderen Server als den lokalen Proxy gesendet.
 * Das DEGIRO-Passwort selbst wird NIE gespeichert - es wird bei jedem
 * "Verbinden" neu eingegeben und nur an den lokalen Proxy uebermittelt.
 */

const LS_KEYS = {
  proxyUrl: "degiro_proxy_url",
  proxyToken: "degiro_proxy_token",
  username: "degiro_username",
  portfolio: "degiro_portfolio",
  transactions: "degiro_transactions",
  history: "degiro_history",
};

const el = (id) => document.getElementById(id);

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

function fmtMoney(value, currency) {
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

function appendHistoryPoint(totals) {
  const history = loadLocal(LS_KEYS.history, []);
  const today = new Date().toISOString().slice(0, 10);
  const point = { date: today, equity: totals.equity, cash: totals.cash };
  const idx = history.findIndex((h) => h.date === today);
  if (idx >= 0) history[idx] = point;
  else history.push(point);
  history.sort((a, b) => a.date.localeCompare(b.date));
  saveLocal(LS_KEYS.history, history);
  return history;
}

let chartInstance = null;

function renderChart(history) {
  el("panel-chart").classList.remove("hidden");
  const ctx = el("chart-history").getContext("2d");
  const labels = history.map((h) => h.date);
  const equity = history.map((h) => h.equity);
  const cash = history.map((h) => h.cash);

  if (chartInstance) chartInstance.destroy();
  chartInstance = new Chart(ctx, {
    type: "line",
    data: {
      labels,
      datasets: [
        {
          label: "Gesamtwert (Equity)",
          data: equity,
          borderColor: "#4f8cff",
          backgroundColor: "rgba(79,140,255,0.15)",
          tension: 0.25,
          fill: true,
        },
        {
          label: "Cash",
          data: cash,
          borderColor: "#35c98f",
          backgroundColor: "transparent",
          borderDash: [4, 3],
          tension: 0.25,
        },
      ],
    },
    options: {
      responsive: true,
      plugins: { legend: { position: "bottom" } },
      scales: { y: { beginAtZero: false } },
    },
  });
}

function renderSummary(totals, fetchedAt) {
  el("panel-summary").classList.remove("hidden");
  el("stat-equity").textContent = fmtMoney(totals.equity);
  el("stat-cash").textContent = fmtMoney(totals.cash);
  el("stat-positions").textContent = fmtMoney(totals.positionsValue);
  el("stat-updated").textContent = fetchedAt ? new Date(fetchedAt).toLocaleString("de-CH") : "–";
}

function renderPositions(positions) {
  el("panel-positions").classList.remove("hidden");
  const tbody = document.querySelector("#table-positions tbody");
  tbody.innerHTML = "";
  const totalValue = positions.reduce((sum, p) => sum + (p.value || 0), 0);

  for (const p of positions) {
    const tr = document.createElement("tr");
    const share = totalValue ? ((p.value || 0) / totalValue) * 100 : 0;
    const pl = typeof p.plUnrealized === "object" && p.plUnrealized
      ? Object.values(p.plUnrealized)[0]
      : p.plUnrealized;
    const plClass = pl > 0 ? "pl-pos" : pl < 0 ? "pl-neg" : "";

    tr.innerHTML = `
      <td>${p.name ?? p.productId}</td>
      <td>${p.symbol ?? "–"}</td>
      <td>${p.size ?? "–"}</td>
      <td>${fmtMoney(p.price, p.currency)}</td>
      <td>${fmtMoney(p.value, p.currency)}</td>
      <td>${share.toFixed(1)}%</td>
      <td>${fmtMoney(p.averagePrice, p.currency)}</td>
      <td class="${plClass}">${pl !== undefined ? fmtMoney(pl, p.currency) : "–"}</td>
    `;
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
      <td>${fmtMoney(t.price)}</td>
      <td>${fmtMoney(t.total)}</td>
    `;
    tbody.appendChild(tr);
  }
}

function renderAllFromCache() {
  const portfolio = loadLocal(LS_KEYS.portfolio, null);
  const transactions = loadLocal(LS_KEYS.transactions, null);
  const history = loadLocal(LS_KEYS.history, []);

  if (portfolio) {
    renderSummary(portfolio.totals, portfolio.fetchedAt);
    renderPositions(portfolio.positions);
  }
  if (transactions) renderTransactions(transactions.transactions);
  if (history.length) renderChart(history);
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
      body: JSON.stringify({
        username,
        password,
        totp_secret_key: totp || undefined,
      }),
    });

    saveLocal(LS_KEYS.proxyUrl, proxyUrl());
    saveLocal(LS_KEYS.proxyToken, proxyToken());
    saveLocal(LS_KEYS.username, username);
    el("input-password").value = "";

    setOnlineBadge(true);
    setMessage("Verbunden. Lade Portfolio und Transaktionen...", "ok");

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
    proxyFetch("/api/transactions?days=730"),
  ]);

  saveLocal(LS_KEYS.portfolio, portfolio);
  saveLocal(LS_KEYS.transactions, transactions);
  const history = appendHistoryPoint(portfolio.totals);

  renderSummary(portfolio.totals, portfolio.fetchedAt);
  renderPositions(portfolio.positions);
  renderTransactions(transactions.transactions);
  renderChart(history);
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
