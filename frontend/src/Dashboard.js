import React, { useEffect, useMemo, useState } from "react";

const API_BASE = "http://127.0.0.1:8000";
const SYMBOLS = ["R_10", "R_25", "R_50", "R_75", "R_100"];

export default function Dashboard() {
  const [selectedSymbol, setSelectedSymbol] = useState("R_10");

  const [loading, setLoading] = useState(false);
  const [scanLoading, setScanLoading] = useState(false);

  const [error, setError] = useState(null);
  const [scanError, setScanError] = useState(null);

  const [candles, setCandles] = useState([]);
  const [levels, setLevels] = useState({ supports: [], resistances: [] });

  const [signal, setSignal] = useState(null);
  const [actions, setActions] = useState([]);
  const [active, setActive] = useState(false);
  const [price, setPrice] = useState(null);

  const [dailyR, setDailyR] = useState(0);
  const [activeTotal, setActiveTotal] = useState(0);

  const [scanResults, setScanResults] = useState([]);

  const pretty = (n) => {
    if (n === null || n === undefined) return "-";
    const num = Number(n);
    if (!Number.isFinite(num)) return String(n);
    return num.toFixed(5).replace(/\.?0+$/, "");
  };

  const directionClass = (dir) => {
    if (dir === "BUY") return "buy";
    if (dir === "SELL") return "sell";
    return "hold";
  };

  const actionDotClass = (type) => {
    if (type === "MOVE_SL") return "dot dotYellow";
    if (type === "PARTIAL_TP") return "dot dotGreen";
    if (type === "TRAIL_SUGGESTION") return "dot dotBlue";
    return "dot dotRed";
  };

  const analyze = async (symbol = selectedSymbol) => {
    setLoading(true);
    setError(null);

    try {
      const res = await fetch(`${API_BASE}/analyze`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ symbol, timeframe: "5m" }),
      });

      if (!res.ok) {
        const txt = await res.text();
        throw new Error(`Analyze failed: ${res.status} ${txt}`);
      }

      const data = await res.json();
      if (data.error) throw new Error(data.error);

      setSelectedSymbol(data.symbol || symbol);
      setCandles(Array.isArray(data.candles) ? data.candles : []);
      setLevels(data.levels || { supports: [], resistances: [] });

      setSignal(data.signal || null);
      setActions(Array.isArray(data.actions) ? data.actions : []);
      setActive(Boolean(data.active));
      setPrice(typeof data.price === "number" ? data.price : null);

      if (typeof data.daily_R === "number") setDailyR(data.daily_R);
      if (typeof data.active_total === "number") setActiveTotal(data.active_total);
    } catch (e) {
      setError(e.message || "Analyze error");
    } finally {
      setLoading(false);
    }
  };

  const scanMarkets = async () => {
    setScanLoading(true);
    setScanError(null);

    try {
      const res = await fetch(`${API_BASE}/scan`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ symbols: SYMBOLS, timeframe: "5m" }),
      });

      if (!res.ok) {
        const txt = await res.text();
        throw new Error(`Scan failed: ${res.status} ${txt}`);
      }

      const data = await res.json();
      setScanResults(Array.isArray(data.ranked) ? data.ranked : []);

      if (typeof data.daily_R === "number") setDailyR(data.daily_R);
      if (typeof data.active_total === "number") setActiveTotal(data.active_total);
    } catch (e) {
      setScanError(e.message || "Scan error");
    } finally {
      setScanLoading(false);
    }
  };

  // Auto-analyze when market changes
  useEffect(() => {
    analyze(selectedSymbol);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedSymbol]);

  // ===== Live R Progress =====
  const currentR = useMemo(() => {
    if (!signal?.entry || !signal?.sl || price === null || price === undefined) return 0;
    const entry = Number(signal.entry);
    const sl = Number(signal.sl);
    const current = Number(price);
    const risk = Math.abs(entry - sl);
    if (!Number.isFinite(risk) || risk === 0) return 0;
    if (signal.direction === "BUY") return (current - entry) / risk;
    if (signal.direction === "SELL") return (entry - current) / risk;
    return 0;
  }, [signal, price]);

  const rPercent = Math.max(0, Math.min(100, (currentR / 5) * 100));
  const rMilestone = currentR >= 2 ? "r2" : currentR >= 1 ? "r1" : "r0";

  const showTradeLevels = signal && (signal.direction === "BUY" || signal.direction === "SELL");

  return (
    <div style={{ maxWidth: 1150, margin: "0 auto", padding: "18px 14px 40px" }}>
      {/* Header */}
      <div className="fxHeader">
        <div className="fxBrand">
          <div className="fxOrb" />
          <div>
            <div className="fxTitle">DEXRTRADEZBOT/div>
            <div className="fxSub">5m entries • 1h trend bias • scanner + trade plan</div>
          </div>
        </div>

        <div className="fxRight">
          <div className={`fxPill ${active ? "fxPillActive" : "fxPillIdle"}`}>
            {active ? "ACTIVE TRADE" : "IDLE"}
          </div>
          <div className="fxPill">Daily R: <strong style={{ marginLeft: 6 }}>{dailyR}</strong></div>
          <div className="fxPill">Active: <strong style={{ marginLeft: 6 }}>{activeTotal}</strong></div>
          <div className="fxPill">Price: <strong style={{ marginLeft: 6 }}>{price ?? "-"}</strong></div>
          <div className="fxPill">Market: <strong style={{ marginLeft: 6 }}>{selectedSymbol}</strong></div>
        </div>
      </div>

      {/* Controls */}
      <div style={{ display: "flex", gap: 10, flexWrap: "wrap", alignItems: "center", marginBottom: 12 }}>
        <select value={selectedSymbol} onChange={(e) => setSelectedSymbol(e.target.value)}>
          {SYMBOLS.map((s) => <option key={s} value={s}>{s}</option>)}
        </select>

        <button className="btnPrimary" onClick={() => analyze()} disabled={loading}>
          {loading ? "Analyzing..." : "Analyze"}
        </button>

        <button onClick={scanMarkets} disabled={scanLoading}>
          {scanLoading ? "Scanning..." : "Scan Markets"}
        </button>
      </div>

      {error && (
        <div className="card" style={{ borderColor: "rgba(255,77,109,0.55)", marginBottom: 12 }}>
          <strong style={{ color: "var(--bad)" }}>Error:</strong> {error}
        </div>
      )}

      {/* Layout */}
      <div style={{ display: "grid", gridTemplateColumns: "1.4fr 0.9fr", gap: 12 }}>
        {/* Chart Panel */}
        <div className="card">
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 10 }}>
            <strong>Chart</strong>
            <span className="muted">
              Candles: {candles?.length || 0} • S:{levels?.supports?.length || 0} R:{levels?.resistances?.length || 0}
            </span>
          </div>

          {/* IMPORTANT: This is only a placeholder box.
              Replace this with your real Chart component (lightweight-charts).
              Your Chart component should draw:
              - candles
              - 1 support, 1 resistance
              - entry/SL/TP boxes (if showTradeLevels)
           */}
          <div
            style={{
              height: 380,
              borderRadius: 14,
              border: "1px dashed rgba(255,255,255,0.16)",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              opacity: 0.85,
            }}
          >
            <div style={{ textAlign: "center" }}>
              <div style={{ fontWeight: 800, marginBottom: 6 }}>Chart Component Area</div>
              <div className="muted" style={{ fontSize: 13 }}>
                Plug your chart here (candles + support/resistance + entry/SL/TP boxes)
              </div>
            </div>
          </div>
        </div>

        {/* Signal Panel */}
        <div className="card">
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 10 }}>
            <strong>Signal</strong>
            <span className="fxPill">{active ? "ACTIVE" : "IDLE"}</span>
          </div>

          {!signal ? (
            <div className="muted">No signal yet. Click Analyze.</div>
          ) : (
            <>
              <div style={{ display: "flex", gap: 10, flexWrap: "wrap", alignItems: "center" }}>
                <div>
                  Direction:{" "}
                  <strong className={directionClass(signal.direction)}>
                    {signal.direction}
                  </strong>
                </div>
                <div className="fxPill">
                  Confidence: <strong style={{ marginLeft: 6 }}>{signal.confidence ?? 0}%</strong>
                </div>
              </div>

              <div style={{ marginTop: 10 }}>
                <div className="muted" style={{ fontSize: 12, marginBottom: 4 }}>Reason</div>
                <div style={{ fontSize: 13 }}>{signal.reason || "-"}</div>
              </div>

              <div className="hr" />

              {showTradeLevels ? (
                <>
                  <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10 }}>
                    <div className="kpiBox">
                      <div className="kpiLabel">Entry</div>
                      <div className="kpiValue">{pretty(signal.entry)}</div>
                    </div>
                    <div className="kpiBox">
                      <div className="kpiLabel">RR</div>
                      <div className="kpiValue">{signal.meta?.rr ?? 5}</div>
                    </div>
                    <div className="kpiBox">
                      <div className="kpiLabel">Stop Loss</div>
                      <div className="kpiValue">{pretty(signal.sl)}</div>
                    </div>
                    <div className="kpiBox">
                      <div className="kpiLabel">Take Profit</div>
                      <div className="kpiValue">{pretty(signal.tp)}</div>
                    </div>
                  </div>

                  {/* Live R Bar */}
                  <div style={{ marginTop: 16 }}>
                    <div className="muted" style={{ fontSize: 12, marginBottom: 6 }}>
                      R Progress: {currentR.toFixed(2)}R
                    </div>
                    <div className={`rBarContainer ${rMilestone}`}>
                      <div className="rBarFill" style={{ width: `${rPercent}%` }} />
                    </div>
                    <div className="rScale">
                      <span>0R</span><span>1R</span><span>2R</span><span>3R</span><span>5R</span>
                    </div>
                  </div>
                </>
              ) : (
                <div className="muted" style={{ fontSize: 13 }}>
                  HOLD = waiting for a clean setup (trend + pullback + confirm + BOS + RSI).
                </div>
              )}

              {/* Actions */}
              {Array.isArray(actions) && actions.length > 0 && (
                <div style={{ marginTop: 12 }}>
                  <div className="muted" style={{ fontSize: 12, marginBottom: 6 }}>
                    Actions (what to do now)
                  </div>

                  <div className="actionList">
                    {actions.map((a, i) => (
                      <div key={i} className="actionItem">
                        <span className={actionDotClass(a.type)} />
                        <div style={{ flex: 1 }}>
                          <div className="actionTitle">
                            <strong>{a.type}</strong>
                            {a.to !== undefined && (
                              <span className="muted" style={{ marginLeft: 8 }}>
                                → {a.to}
                              </span>
                            )}
                            {a.percent !== undefined && (
                              <span className="muted" style={{ marginLeft: 8 }}>
                                → {Math.round(a.percent * 100)}%
                              </span>
                            )}
                          </div>
                          <div className="actionWhy muted">{a.why || ""}</div>
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </>
          )}
        </div>
      </div>

      {/* Scanner */}
      <div style={{ marginTop: 12 }} className="card">
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 10 }}>
          <strong>Scanner</strong>
          <span className="muted" style={{ fontSize: 12 }}>Click a row to load that market</span>
        </div>

        {scanError && (
          <div className="fxPill" style={{ borderColor: "rgba(255,77,109,.55)", color: "var(--bad)" }}>
            {scanError}
          </div>
        )}

        {scanResults.length === 0 ? (
          <div className="muted">Click “Scan Markets” to rank all markets.</div>
        ) : (
          <div className="scanTable">
            <div className="scanHead">
              <div>Market</div><div>Score</div><div>Direction</div><div>Confidence</div><div>Setup</div><div>Status</div><div>Reason</div>
            </div>

            {scanResults.map((row, idx) => {
              const isTop = idx === 0;
              return (
                <div
                  key={row.symbol}
                  className={`scanRow ${isTop ? "scanTopPick" : ""}`}
                  onClick={() => setSelectedSymbol(row.symbol)}
                >
                  <div>
                    <div style={{ fontWeight: 900, letterSpacing: ".4px" }}>{row.symbol}</div>
                    <div className="muted" style={{ fontSize: 11 }}>5m entry • 1h bias</div>
                  </div>

                  <div>
                    <div style={{ fontWeight: 900 }}>{row.score_1_10}/10</div>
                    <div className="scoreBar">
                      <div className="scoreFill" style={{ width: `${(row.score_1_10 / 10) * 100}%` }} />
                    </div>
                  </div>

                  <div className={directionClass(row.direction)}>{row.direction}</div>
                  <div>{row.confidence}%</div>
                  <div>{row.setup_score}</div>

                  <div className="muted" style={{ fontSize: 12 }}>
                    {row.active_trade ? "ACTIVE" : row.cooldown > 0 ? `COOLDOWN ${row.cooldown}s` : "READY"}
                  </div>

                  <div className="muted" style={{ fontSize: 12, lineHeight: "1.2rem" }}>
                    {row.reason || "-"}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}