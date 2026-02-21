// src/api.js
const BACKEND_URL = "http://10.0.0.130:8000"; // Update with your backend URL

// fetch one symbol
export async function analyzeSymbol(symbol, timeframe) {
  const res = await fetch(`${BACKEND_URL}/analyze_market/analyze`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ symbol, timeframe }),
  });

  if (!res.ok) {
    const txt = await res.text();
    throw new Error(`Backend error ${res.status}: ${txt}`);
  }

  return await res.json();
}

// fetch all symbols in parallel (shared dashboard call)
export async function fetchAllVolatilityData(timeframe = "1m") {
  const symbols = ["R_10", "R_25", "R_50", "R_75", "R_100"];

  const results = {};
  await Promise.all(
    symbols.map(async (symbol) => {
      const data = await analyzeSymbol(symbol, timeframe);
      results[symbol] = data;
    })
  );

  return results; // { R_10: {candles, signal}, ... }
}
