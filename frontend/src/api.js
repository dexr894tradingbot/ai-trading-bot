// frontend/src/api.js

function deriveBackendBaseUrl() {
  const env = process.env.REACT_APP_BACKEND_URL;
  if (env && typeof env === "string" && env.startsWith("http")) {
    return env.replace(/\/$/, "");
  }

  const { protocol, hostname } = window.location;

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

export async function analyzeMarket(symbol, timeframe) {
  return postJson("/analyze_market/analyze", { symbol, timeframe });
}

export async function scanMarkets(symbols, timeframe) {
  return postJson("/analyze_market/scan", { symbols, timeframe });
}

export async function fetchLiveState(symbol, timeframe) {
  const qs = new URLSearchParams({ symbol, timeframe });
  return getJson(`/analyze_market/live?${qs.toString()}`);
}

export async function fetchHistory({ symbol = "", limit = 50 } = {}) {
  const safeLimit = Math.max(1, Math.min(Number(limit) || 50, 500));
  const qs = new URLSearchParams();
  if (symbol) qs.set("symbol", symbol);
  qs.set("limit", String(safeLimit));
  return getJson(`/analyze_market/history?${qs.toString()}`);
}

export async function fetchPerformance(lastN = 30) {
  const safe = Math.max(1, Math.min(Number(lastN) || 30, 200));
  return getJson(`/analyze_market/performance?last_n=${safe}`);
}

export function getBackendUrl() {
  return BACKEND;
}

export function getBackendWebSocketUrl() {
  const env = process.env.REACT_APP_BACKEND_URL;
  if (env && typeof env === "string" && env.startsWith("http")) {
    return env.replace(/^http/, "ws").replace(/\/$/, "") + "/analyze_market/ws";
  }

  const { protocol, hostname } = window.location;
  const wsProtocol = protocol === "https:" ? "wss:" : "ws:";

  const backendHost = hostname.replace(
    /-\d+\.app\.github\.dev$/,
    "-8000.app.github.dev"
  );

  return `${wsProtocol}//${backendHost}/analyze_market/ws`;
}