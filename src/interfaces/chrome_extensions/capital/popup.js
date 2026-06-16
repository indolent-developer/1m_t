// popup.js — drives the extension UI

let currentEpic = null;
let toastTimer  = null;

// ── Messaging ──────────────────────────────────────────────────────────────────

function bg(action, extra = {}) {
  return new Promise((resolve, reject) => {
    chrome.runtime.sendMessage({ action, ...extra }, resp => {
      if (chrome.runtime.lastError) return reject(new Error(chrome.runtime.lastError.message));
      if (resp.ok) resolve(resp.result);
      else reject(new Error(resp.error));
    });
  });
}

// ── Toast ──────────────────────────────────────────────────────────────────────

function toast(msg, type = "info", ms = 3000) {
  const el = document.getElementById("toast");
  el.textContent = msg;
  el.className = `toast ${type}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.className = "toast hidden", ms);
}

// ── Status dot ────────────────────────────────────────────────────────────────

function setStatus(state) {
  const dot = document.getElementById("statusDot");
  dot.className = `status-dot ${state}`;
  dot.title = state === "connected" ? "Connected" : state === "connecting" ? "Connecting…" : "Disconnected";
}

// ── Instrument card ───────────────────────────────────────────────────────────

function showInstrument(epic, name, bid, ask) {
  currentEpic = epic;
  document.getElementById("instEpic").textContent = epic;
  document.getElementById("instName").textContent = name || epic;
  document.getElementById("instBid").textContent  = bid  ? bid.toFixed(2)  : "—";
  document.getElementById("instAsk").textContent  = ask  ? ask.toFixed(2)  : "—";
  document.getElementById("instrumentCard").style.display = "block";
  document.getElementById("btnCall").disabled = false;
  document.getElementById("btnPut").disabled  = false;
}

async function loadQuote(epic) {
  try {
    const q = await bg("getQuote", { epic });
    showInstrument(epic, q.name, q.bid, q.ask);
    loadSupertrend(epic);   // fire-and-forget alongside quote
  } catch (e) {
    toast(`Quote error: ${e.message}`, "error");
  }
}

async function loadSupertrend(epic) {
  const tf = document.getElementById("stTf").value;
  // Show loading state
  const badge = document.getElementById("stBadge");
  badge.className = "st-badge";
  document.getElementById("stArrow").textContent = "…";
  document.getElementById("stValue").textContent = "";
  document.getElementById("stDist").textContent  = "";
  document.getElementById("stRow").style.display = "flex";
  document.querySelector(".st-badge .st-label").textContent = `ST ${tf}`;

  try {
    const st = await bg("getSupertrend", { epic, tf });
    renderSupertrend(st);
  } catch (e) {
    document.getElementById("stArrow").textContent = "—";
    document.getElementById("stValue").textContent = `(${e.message.slice(0, 30)})`;
  }
}

function renderSupertrend(st) {
  if (!st || st.value === null) return;
  const isUp   = st.direction === 1;
  const badge  = document.getElementById("stBadge");
  badge.className = `st-badge ${isUp ? "up" : "down"}`;
  document.getElementById("stArrow").textContent = isUp ? "▲" : "▼";
  document.getElementById("stValue").textContent = st.value.toFixed(2);
  if (st.distancePct !== null) {
    const sign  = st.distancePct >= 0 ? "+" : "";
    document.getElementById("stDist").textContent = `${sign}${st.distancePct.toFixed(2)}%`;
  }
}

// ── Search ────────────────────────────────────────────────────────────────────

async function runSearch() {
  const q = document.getElementById("searchInput").value.trim();
  if (!q) return;
  const container = document.getElementById("searchResults");
  container.innerHTML = '<div style="padding:8px 10px;color:#7a8fa0"><span class="spinner"></span>Searching…</div>';
  container.style.display = "block";

  try {
    const { markets } = await bg("searchMarkets", { query: q });
    if (!markets.length) {
      container.innerHTML = '<div style="padding:8px 10px;color:#7a8fa0">No results</div>';
      return;
    }
    container.innerHTML = markets.slice(0, 12).map(m => `
      <div class="search-result-item" data-epic="${m.epic}">
        <div>
          <div class="result-name">${escHtml(m.instrumentName || m.epic)}</div>
          <div class="result-epic">${m.epic}</div>
        </div>
        <span class="result-type">${m.instrumentType || "CFD"}</span>
      </div>
    `).join("");

    container.querySelectorAll(".search-result-item").forEach(el => {
      el.addEventListener("click", () => {
        container.style.display = "none";
        document.getElementById("searchInput").value = el.dataset.epic;
        loadQuote(el.dataset.epic);
      });
    });
  } catch (e) {
    container.innerHTML = `<div style="padding:8px 10px;color:#f44336">${escHtml(e.message)}</div>`;
  }
}

// ── Place order ───────────────────────────────────────────────────────────────

async function placeOrder(direction) {
  if (!currentEpic) return toast("Select an instrument first", "error");

  const koLevel = parseFloat(document.getElementById("koLevel").value);
  const size    = parseFloat(document.getElementById("size").value);

  if (!koLevel || koLevel <= 0) return toast("Enter a valid KO level", "error");
  if (!size    || size    <= 0) return toast("Enter a valid size", "error");

  const btn = direction === "BUY"
    ? document.getElementById("btnCall")
    : document.getElementById("btnPut");

  btn.disabled = true;
  btn.innerHTML = `<span class="spinner"></span>${direction === "BUY" ? "Placing Call…" : "Placing Put…"}`;

  try {
    const result = await bg("placeKO", {
      epic: currentEpic, direction, size, koLevel,
    });
    toast(`Order placed — dealId: ${result.dealId}`, "success", 5000);
    await loadPositions();
  } catch (e) {
    toast(`Order failed: ${e.message}`, "error", 6000);
  } finally {
    btn.disabled = false;
    btn.innerHTML = direction === "BUY" ? "Call ▲" : "Put ▼";
  }
}

// ── Positions ─────────────────────────────────────────────────────────────────

async function loadPositions() {
  const list = document.getElementById("positionsList");
  list.innerHTML = '<div class="no-positions"><span class="spinner"></span>Loading…</div>';
  try {
    const { positions } = await bg("getPositions");
    renderPositions(positions);
  } catch (e) {
    list.innerHTML = `<div class="no-positions" style="color:#f44336">${escHtml(e.message)}</div>`;
  }
}

function renderPositions(positions) {
  const list = document.getElementById("positionsList");
  if (!positions.length) {
    list.innerHTML = '<div class="no-positions">No open positions</div>';
    return;
  }

  list.innerHTML = positions.map(raw => {
    const pos  = raw.position || raw;
    const mkt  = raw.market   || {};
    const name = mkt.instrumentName || mkt.epic || pos.epic || "—";
    const epic = mkt.epic || pos.epic || "—";
    const size = pos.size || 0;
    const dir  = pos.direction || (pos.size > 0 ? "BUY" : "SELL");
    const pnl  = parseFloat(pos.profit || 0);
    const pnlCls = pnl >= 0 ? "profit" : "loss";
    const pnlStr = (pnl >= 0 ? "+" : "") + pnl.toFixed(2);
    const dealId = pos.dealId || "";
    const level  = parseFloat(pos.level || 0).toFixed(2);
    const label  = dir === "BUY" ? "Call" : "Put";

    return `
      <div class="position-card" data-deal="${escAttr(dealId)}">
        <div class="pos-info">
          <div class="pos-name">${escHtml(name)}</div>
          <div class="pos-details">${label} × ${size} @ ${level} | <span class="epic-badge" style="font-size:10px">${escHtml(epic)}</span></div>
        </div>
        <div class="pos-pnl ${pnlCls}">${pnlStr}</div>
        <button class="btn-close" data-deal="${escAttr(dealId)}">Close</button>
      </div>
    `;
  }).join("");

  list.querySelectorAll(".btn-close").forEach(btn => {
    btn.addEventListener("click", () => closePosition(btn.dataset.deal, btn));
  });
}

async function closePosition(dealId, btn) {
  if (!dealId) return;
  if (!confirm("Close this position?")) return;

  btn.disabled = true;
  btn.textContent = "…";
  try {
    await bg("closePosition", { dealId });
    toast("Position closed", "success");
    await loadPositions();
  } catch (e) {
    toast(`Close failed: ${e.message}`, "error", 6000);
    btn.disabled = false;
    btn.textContent = "Close";
  }
}

// ── Detect market from active Capital.com tab ─────────────────────────────────

async function tryDetectFromTab() {
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab || !tab.url?.includes("capital.com")) return;

    const results = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: () => {
        // Inline detection — content.js may not have run yet
        const urlMatch = location.pathname.match(/\/(?:trading|chart|markets?)\/([A-Z0-9_-]{2,20})/i);
        return urlMatch ? urlMatch[1].toUpperCase() : null;
      },
    });
    const epic = results?.[0]?.result;
    if (epic) {
      document.getElementById("searchInput").value = epic;
      await loadQuote(epic);
    }
  } catch (_) { /* tab detection is best-effort */ }
}

// ── Utilities ─────────────────────────────────────────────────────────────────

function escHtml(s) {
  return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");
}
function escAttr(s) { return escHtml(s); }

// ── Init ──────────────────────────────────────────────────────────────────────

async function init() {
  setStatus("connecting");
  try {
    await bg("ping");
    setStatus("connected");
  } catch (e) {
    setStatus("error");
    toast(e.message.includes("credentials") ? "Open ⚙ Settings to configure API credentials" : e.message, "error", 8000);
  }

  // Wire up UI
  document.getElementById("optionsBtn").addEventListener("click", () => chrome.runtime.openOptionsPage());
  document.getElementById("searchBtn").addEventListener("click", runSearch);
  document.getElementById("searchInput").addEventListener("keydown", e => { if (e.key === "Enter") runSearch(); });
  document.getElementById("btnCall").addEventListener("click", () => placeOrder("BUY"));
  document.getElementById("btnPut").addEventListener("click",  () => placeOrder("SELL"));
  document.getElementById("refreshBtn").addEventListener("click", loadPositions);

  // Reload SuperTrend when timeframe changes
  document.getElementById("stTf").addEventListener("change", () => {
    if (currentEpic) loadSupertrend(currentEpic);
  });

  // Close search results on outside click
  document.addEventListener("click", e => {
    if (!e.target.closest("#searchResults") && !e.target.closest(".search-row")) {
      document.getElementById("searchResults").style.display = "none";
    }
  });

  // Try to auto-detect instrument from the active Capital.com tab
  await tryDetectFromTab();

  // Load positions
  await loadPositions();
}

document.addEventListener("DOMContentLoaded", init);
