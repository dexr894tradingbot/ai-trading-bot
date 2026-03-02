// frontend/src/api.js

function deriveBackendBaseUrl() {
  // BEST: set REACT_APP_BACKEND_URL in Codespaces env
  // Fallback: convert frontend port domain -> backend port domain
  const env = process.env.REACT_APP_BACKEND_URL;
  if (env && typeof env === "string" && env.startsWith("http")) {
    return env.replace(/\/$/, "");
  }

  const { protocol, hostname } = window.location;

  // Example:
  // frontend: probable-acorn-...-3005.app.github.dev
  // backend:  probable-acorn-...-8000.app.github.dev
  const backendHost = hostname.replace(
    /-\d+\.app\.github\.dev$/,
    "-8000.app.github.dev"
  );

  return `${protocol}//${backendHost}`;
}

const BACKEND = deriveBackendBaseUrl();

async function readErrorText(res) {
  try {
    return await res.text();
  } catch {
    return "";
  }
}

async function postJson(path, body) {
  const url = `${BACKEND}${path}`;
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body ?? {}),
  });

  if (!res.ok) {
    const txt = await readErrorText(res);
    throw new Error(
      `${res.status} ${res.statusText} — ${txt || "Request failed"}`
    );
  }
  return await res.json();
}

async function getJson(path) {
  const url = `${BACKEND}${path}`;
  const res = await fetch(url, { method: "GET" });

  if (!res.ok) {
    const txt = await readErrorText(res);
    throw new Error(
      `${res.status} ${res.statusText} — ${txt || "Request failed"}`
    );
  }
  return await res.json();
}

// ✅ Analyze one market
export async function analyzeMarket(symbol, timeframe) {
  return postJson("/analyze_market/analyze", { symbol, timeframe });
}

// ✅ Scan multiple markets
export async function scanMarkets(symbols, timeframe) {
  return postJson("/analyze_market/scan", { symbols, timeframe });
}

// ✅ Trade history
// Backend: GET /analyze_market/history?symbol=R_10&limit=50
export async function fetchHistory({ symbol = "", limit = 50 } = {}) {
  const safeLimit = Math.max(1, Math.min(Number(limit) || 50, 500));
  const qs = new URLSearchParams();
  if (symbol) qs.set("symbol", symbol);
  qs.set("limit", String(safeLimit));
  return getJson(`/analyze_market/history?${qs.toString()}`);
}

// ✅ Performance stats
// Backend: GET /analyze_market/performance?last_n=30
export async function fetchPerformance(lastN = 30) {
  const safe = Math.max(1, Math.min(Number(lastN) || 30, 200));
  return getJson(`/analyze_market/performance?last_n=${safe}`);
}

// Helper (optional)
export function getBackendUrl() {
  return BACKEND;
}