// Service worker — all Capital.com API calls go through here so CORS is not a problem.

const LIVE_URL  = "https://api-capital.backend-capital.com";
const DEMO_URL  = "https://demo-api-capital.backend-capital.com";

let _session = null;   // { cst, token, baseUrl, expiry }

// ── Session ────────────────────────────────────────────────────────────────────

async function getCredentials() {
  return new Promise(resolve =>
    chrome.storage.sync.get(["apiKey", "username", "password", "isDemo"], resolve)
  );
}

async function ensureSession() {
  if (_session && Date.now() < _session.expiry) return _session;

  const creds = await getCredentials();
  if (!creds.apiKey || !creds.username || !creds.password) {
    throw new Error("No credentials — open Options to configure.");
  }

  const baseUrl = creds.isDemo ? DEMO_URL : LIVE_URL;
  const resp = await fetch(`${baseUrl}/api/v1/session`, {
    method: "POST",
    headers: {
      "X-CAP-API-KEY": creds.apiKey,
      "Content-Type":  "application/json",
    },
    body: JSON.stringify({
      identifier:      creds.username,
      password:        creds.password,
      encryptedPassword: false,
    }),
  });

  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(`Auth failed ${resp.status}: ${text}`);
  }

  _session = {
    cst:     resp.headers.get("CST"),
    token:   resp.headers.get("X-SECURITY-TOKEN"),
    baseUrl,
    expiry:  Date.now() + 540_000,  // 9 min
  };
  return _session;
}

function authHeaders(session) {
  return {
    "X-CAP-API-KEY":    "",           // not needed after session creation
    "CST":              session.cst,
    "X-SECURITY-TOKEN": session.token,
    "Content-Type":     "application/json",
  };
}

async function apiGet(path) {
  const s = await ensureSession();
  const resp = await fetch(`${s.baseUrl}${path}`, { headers: authHeaders(s) });
  if (resp.status === 401) { _session = null; return apiGet(path); }
  if (!resp.ok) { const t = await resp.text(); throw new Error(`GET ${path} → ${resp.status}: ${t}`); }
  return resp.json();
}

async function apiPost(path, body) {
  const s = await ensureSession();
  const resp = await fetch(`${s.baseUrl}${path}`, {
    method: "POST", headers: authHeaders(s), body: JSON.stringify(body),
  });
  if (resp.status === 401) { _session = null; return apiPost(path, body); }
  if (!resp.ok) { const t = await resp.text(); throw new Error(`POST ${path} → ${resp.status}: ${t}`); }
  return resp.json();
}

async function apiDelete(path) {
  const s = await ensureSession();
  const resp = await fetch(`${s.baseUrl}${path}`, {
    method: "DELETE", headers: authHeaders(s),
  });
  if (resp.status === 401) { _session = null; return apiDelete(path); }
  if (!resp.ok) { const t = await resp.text(); throw new Error(`DELETE ${path} → ${resp.status}: ${t}`); }
  return resp.json();
}

// ── SuperTrend (length=10, multiplier=2.0 — matches /indp) ───────────────────

// Wilder's RMA used by pandas-ta for ATR
function calcRma(values, length) {
  const result = new Array(values.length).fill(null);
  // Seed with SMA of first `length` values
  let sum = 0;
  for (let i = 0; i < length; i++) sum += values[i];
  result[length - 1] = sum / length;
  for (let i = length; i < values.length; i++) {
    result[i] = (result[i - 1] * (length - 1) + values[i]) / length;
  }
  return result;
}

function calcSupertrend(highs, lows, closes, length = 10, multiplier = 2.0) {
  const n = closes.length;

  // True Range
  const tr = new Array(n).fill(null);
  tr[0] = highs[0] - lows[0];
  for (let i = 1; i < n; i++) {
    tr[i] = Math.max(
      highs[i] - lows[i],
      Math.abs(highs[i] - closes[i - 1]),
      Math.abs(lows[i]  - closes[i - 1]),
    );
  }

  const atr = calcRma(tr, length);

  const upperBand = new Array(n).fill(null);
  const lowerBand = new Array(n).fill(null);
  const st        = new Array(n).fill(null);
  const dir       = new Array(n).fill(null);

  for (let i = length - 1; i < n; i++) {
    if (atr[i] === null) continue;
    const hl2  = (highs[i] + lows[i]) / 2;
    const bUp  = hl2 + multiplier * atr[i];
    const bLo  = hl2 - multiplier * atr[i];

    // Final bands: tighten only, never widen while price stays on same side
    if (i === length - 1) {
      upperBand[i] = bUp;
      lowerBand[i] = bLo;
    } else {
      upperBand[i] = (bUp < upperBand[i-1] || closes[i-1] > upperBand[i-1]) ? bUp : upperBand[i-1];
      lowerBand[i] = (bLo > lowerBand[i-1] || closes[i-1] < lowerBand[i-1]) ? bLo : lowerBand[i-1];
    }

    if (i === length - 1) {
      dir[i] = 1;
      st[i]  = lowerBand[i];
    } else {
      const prevSt = st[i - 1];
      if (prevSt === upperBand[i - 1]) {
        dir[i] = closes[i] > upperBand[i] ? 1 : -1;
      } else {
        dir[i] = closes[i] < lowerBand[i] ? -1 : 1;
      }
      st[i] = dir[i] === 1 ? lowerBand[i] : upperBand[i];
    }
  }

  return { value: st[n - 1], direction: dir[n - 1] };
}

// Resolution label → Capital.com resolution string
const TF_RESOLUTION = {
  "1m":  "MINUTE",
  "5m":  "MINUTE_5",
  "15m": "MINUTE_15",
  "30m": "MINUTE_30",
  "1h":  "HOUR",
  "4h":  "HOUR_4",
  "1d":  "DAY",
};

async function fetchSupertrend(epic, tf = "5m") {
  const resolution = TF_RESOLUTION[tf] || "MINUTE_5";
  const data = await apiGet(
    `/api/v1/prices/${encodeURIComponent(epic)}?resolution=${resolution}&max=80`
  );
  const prices = data.prices || [];
  if (prices.length < 12) throw new Error("Not enough OHLC bars");

  const highs  = prices.map(p => parseFloat(p.highPrice?.bid  || p.highPrice?.mid  || 0));
  const lows   = prices.map(p => parseFloat(p.lowPrice?.bid   || p.lowPrice?.mid   || 0));
  const closes = prices.map(p => parseFloat(p.closePrice?.bid || p.closePrice?.mid || 0));

  const { value, direction } = calcSupertrend(highs, lows, closes);
  const lastClose = closes[closes.length - 1];
  const distPct   = value && lastClose
    ? ((lastClose - value) / lastClose * 100)
    : null;

  return {
    value:       value     !== null ? parseFloat(value.toFixed(4))     : null,
    direction:   direction !== null ? (direction > 0 ? 1 : -1)        : null,
    distancePct: distPct   !== null ? parseFloat(distPct.toFixed(2))   : null,
  };
}

// ── Deal confirmation polling ──────────────────────────────────────────────────

async function confirmDeal(dealRef) {
  for (let i = 0; i < 10; i++) {
    await new Promise(r => setTimeout(r, 500));
    try {
      const data   = await apiGet(`/api/v1/confirms/${dealRef}`);
      const status = (data.status || "").toUpperCase();
      if (status === "REJECTED") throw new Error(`Rejected: ${data.reason}`);
      if (["ACCEPTED", "OPEN", "CLOSED", "WORKING"].includes(status)) return data;
    } catch (e) {
      if (e.message.startsWith("Rejected")) throw e;
    }
  }
  throw new Error("Deal not confirmed after retries");
}

// ── Handlers ──────────────────────────────────────────────────────────────────

async function handleMessage(msg) {
  switch (msg.action) {

    case "ping": {
      await ensureSession();
      return { ok: true };
    }

    case "getPositions": {
      const data = await apiGet("/api/v1/positions");
      return { positions: data.positions || [] };
    }

    case "searchMarkets": {
      const data = await apiGet(`/api/v1/markets?searchTerm=${encodeURIComponent(msg.query)}&limit=20`);
      return { markets: data.markets || [] };
    }

    case "getQuote": {
      const data = await apiGet(`/api/v1/markets/${encodeURIComponent(msg.epic)}`);
      const snap = data.snapshot || {};
      return {
        bid:  parseFloat(snap.bid   || 0),
        ask:  parseFloat(snap.offer || 0),
        name: data.instrumentName || msg.epic,
      };
    }

    case "placeKO": {
      // msg: { epic, direction:"BUY"|"SELL", size, koLevel, stopLoss?, takeProfit? }
      const body = {
        epic:           msg.epic,
        direction:      msg.direction,
        size:           msg.size,
        guaranteedStop: true,
        stopLevel:      msg.koLevel,
        trailingStop:   false,
      };
      if (msg.stopLoss)   body.stopLevel   = msg.stopLoss;   // user override
      if (msg.takeProfit) body.profitLevel = msg.takeProfit;

      // Re-apply koLevel as guaranteedStop level (it IS the knock-out level)
      body.stopLevel = msg.koLevel;

      const resp = await apiPost("/api/v1/positions", body);
      const conf = await confirmDeal(resp.dealReference);
      return { dealId: conf.dealId, dealRef: resp.dealReference, status: conf.status };
    }

    case "getSupertrend": {
      // msg: { epic, tf? }  tf defaults to "5m"
      const st = await fetchSupertrend(msg.epic, msg.tf || "5m");
      return st;
    }

    case "closePosition": {
      const resp = await apiDelete(`/api/v1/positions/${msg.dealId}`);
      if (resp.dealReference) {
        await confirmDeal(resp.dealReference);
      }
      return { ok: true };
    }

    default:
      throw new Error(`Unknown action: ${msg.action}`);
  }
}

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  handleMessage(msg)
    .then(result  => sendResponse({ ok: true,  result }))
    .catch(error  => sendResponse({ ok: false, error: error.message }));
  return true;  // keep channel open for async response
});
