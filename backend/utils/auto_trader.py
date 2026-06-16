import time
from typing import Dict, Any, Optional

try:
    from db import save_json_state, load_json_state
except Exception:
    save_json_state = None
    load_json_state = None


DEFAULT_STATE = {
    "enabled": False,
    "account": "demo",
    "account_label_demo": "Practice",
    "account_label_real": "Primary",
    "mode": "deriv_api",
    "emergency_stop": False,

    "screenshot_mode": False,
    "private_details": True,

    "risk_mode": "fixed_usd",
    "risk_per_split": 1.0,
    "split_trades": 2,
    "total_planned_risk": 2.0,

    "min_confidence": 74,
    "min_quality": "B+",
    "min_rr": 2.0,

    "max_open_trades": 2,
    "max_trades_per_day": 6,
    "daily_max_loss": 10.0,
    "daily_profit_target": 20.0,

    "loss_streak": 0,
    "cooldown_until": 0,
    "cooldown_after_losses": 2,
    "cooldown_minutes": 120,

    "only_enter_now": True,
    "require_sl_tp": True,
    "block_duplicates": True,

    "executions": [],
    "rejections": [],
    "executed_trade_ids": [],
}


AUTO_TRADE_STATE = DEFAULT_STATE.copy()


def _now() -> int:
    return int(time.time())


def load_auto_trade_state() -> Dict[str, Any]:
    global AUTO_TRADE_STATE

    if load_json_state:
        try:
            saved = load_json_state("auto_trade_state")
            if saved:
                AUTO_TRADE_STATE.update(saved)
        except Exception as e:
            print("load_auto_trade_state error:", e)

    return AUTO_TRADE_STATE


def persist_auto_trade_state() -> None:
    if save_json_state:
        try:
            save_json_state("auto_trade_state", AUTO_TRADE_STATE)
        except Exception as e:
            print("persist_auto_trade_state error:", e)


def get_public_status() -> Dict[str, Any]:
    state = load_auto_trade_state()

    account = state.get("account", "demo")
    account_label = (
        state.get("account_label_real", "Primary")
        if account == "real"
        else state.get("account_label_demo", "Practice")
    )

    return {
        "enabled": state["enabled"],
        "account": account,
        "account_label": account_label,
        "mode": state["mode"],
        "emergency_stop": state["emergency_stop"],
        "screenshot_mode": state["screenshot_mode"],
        "private_details": state["private_details"],

        "risk_mode": state["risk_mode"],
        "risk_per_split": state["risk_per_split"],
        "split_trades": state["split_trades"],
        "total_planned_risk": float(state["risk_per_split"]) * int(state["split_trades"]),

        "min_confidence": state["min_confidence"],
        "min_quality": state["min_quality"],
        "min_rr": state["min_rr"],

        "max_open_trades": state["max_open_trades"],
        "max_trades_per_day": state["max_trades_per_day"],
        "daily_max_loss": state["daily_max_loss"],
        "daily_profit_target": state["daily_profit_target"],

        "loss_streak": state["loss_streak"],
        "cooldown_until": state["cooldown_until"],
        "cooldown_active": _now() < int(state.get("cooldown_until", 0) or 0),
        "cooldown_after_losses": state["cooldown_after_losses"],
        "cooldown_minutes": state["cooldown_minutes"],

        "only_enter_now": state["only_enter_now"],
        "require_sl_tp": state["require_sl_tp"],
        "block_duplicates": state["block_duplicates"],

        "recent_executions": state["executions"][-20:],
        "recent_rejections": state["rejections"][-20:],
    }


def update_auto_trade_settings(**kwargs) -> Dict[str, Any]:
    load_auto_trade_state()

    allowed = set(DEFAULT_STATE.keys())

    for key, value in kwargs.items():
        if value is None:
            continue
        if key not in allowed:
            continue

        if key == "account" and value not in ("demo", "real"):
            continue
        if key == "mode" and value not in ("deriv_api", "mt5"):
            continue
        if key == "risk_mode" and value not in ("fixed_usd", "percent_balance", "fixed_lot"):
            continue

        AUTO_TRADE_STATE[key] = value

    AUTO_TRADE_STATE["total_planned_risk"] = (
        float(AUTO_TRADE_STATE.get("risk_per_split", 1.0))
        * int(AUTO_TRADE_STATE.get("split_trades", 2))
    )

    if AUTO_TRADE_STATE.get("emergency_stop"):
        AUTO_TRADE_STATE["enabled"] = False

    persist_auto_trade_state()
    return get_public_status()


def estimate_rr(signal: Dict[str, Any]) -> Optional[float]:
    try:
        entry = float(signal.get("entry"))
        sl = float(signal.get("sl"))
        tp2 = float(signal.get("tp2") or signal.get("tp"))
        risk = abs(entry - sl)
        reward = abs(tp2 - entry)
        if risk <= 0:
            return None
        return reward / risk
    except Exception:
        return None


def reject(reason: str, signal: Dict[str, Any], extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    row = {
        "ok": False,
        "reason": reason,
        "symbol": signal.get("symbol"),
        "direction": signal.get("direction"),
        "trade_action": signal.get("trade_action"),
        "confidence": signal.get("confidence"),
        "time": _now(),
    }

    if extra:
        row.update(extra)

    AUTO_TRADE_STATE["rejections"].append(row)
    AUTO_TRADE_STATE["rejections"] = AUTO_TRADE_STATE["rejections"][-100:]
    persist_auto_trade_state()
    return row


def can_auto_trade(signal: Dict[str, Any]) -> Dict[str, Any]:
    state = load_auto_trade_state()

    if state["emergency_stop"]:
        return reject("emergency_stop_active", signal)

    if not state["enabled"]:
        return reject("auto_trade_disabled", signal)

    if _now() < int(state.get("cooldown_until", 0) or 0):
        return reject("cooldown_active", signal, {"cooldown_until": state["cooldown_until"]})

    if signal.get("direction") not in ("BUY", "SELL"):
        return reject("no_trade_direction", signal)

    if state["only_enter_now"] and signal.get("trade_action") != "ENTER_NOW":
        return reject("not_enter_now", signal)

    confidence = int(signal.get("confidence", 0) or 0)
    if confidence < int(state["min_confidence"]):
        return reject("confidence_too_low", signal, {"required": state["min_confidence"]})

    if state["require_sl_tp"]:
        if signal.get("entry") is None or signal.get("sl") is None or signal.get("tp1") is None or signal.get("tp2") is None:
            return reject("missing_entry_sl_tp", signal)

    rr = estimate_rr(signal)
    if rr is None or rr < float(state["min_rr"]):
        return reject("rr_too_low", signal, {"rr": rr, "required": state["min_rr"]})

    trade_id = signal.get("trade_id") or signal.get("public_trade_id")
    if state["block_duplicates"] and trade_id and trade_id in state["executed_trade_ids"]:
        return reject("duplicate_trade_blocked", signal)

    return {"ok": True, "reason": "allowed", "rr": rr}


def build_split_trade_plan(symbol: str, timeframe: str, signal: Dict[str, Any]) -> Dict[str, Any]:
    state = load_auto_trade_state()

    risk_each = float(state["risk_per_split"])
    split_count = int(state["split_trades"])

    legs = []

    if split_count >= 1:
        legs.append({
            "leg": "A",
            "risk_amount": risk_each,
            "target": "TP1",
            "tp": signal.get("tp1"),
            "close_rule": "close_full_at_tp1",
        })

    if split_count >= 2:
        legs.append({
            "leg": "B",
            "risk_amount": risk_each,
            "target": "TP2",
            "tp": signal.get("tp2"),
            "breakeven_rule": "move_sl_to_entry_after_tp1",
        })

    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "direction": signal.get("direction"),
        "entry": signal.get("entry"),
        "sl": signal.get("sl"),
        "tp1": signal.get("tp1"),
        "tp2": signal.get("tp2"),
        "risk_per_split": risk_each,
        "total_risk": risk_each * split_count,
        "rr": estimate_rr(signal),
        "legs": legs,
    }


async def execute_auto_trade(symbol: str, timeframe: str, signal: Dict[str, Any]) -> Dict[str, Any]:
    signal = dict(signal or {})
    signal["symbol"] = symbol
    signal["timeframe"] = timeframe

    check = can_auto_trade(signal)
    if not check["ok"]:
        return check

    plan = build_split_trade_plan(symbol, timeframe, signal)

    from utils.deriv_orders import place_deriv_option_trade

    account = AUTO_TRADE_STATE.get("account", "demo")
    stake = float(AUTO_TRADE_STATE.get("risk_per_split") or 1.0)

    execution = await place_deriv_option_trade(
        account=account,
        symbol=symbol,
        direction=signal.get("direction"),
        stake=stake,
        duration=5,
        duration_unit="m",
    )

    result = {
        "ok": bool(execution.get("success")),
        "paper_only": False,
        "note": "Real Deriv execution attempted.",
        "account": account,
        "mode": AUTO_TRADE_STATE["mode"],
        "trade_id": signal.get("public_trade_id") or signal.get("trade_id"),
        "executed_at": _now(),
        "plan": plan,
        "execution": execution,
    }

    trade_id = result.get("trade_id")
    if trade_id:
        AUTO_TRADE_STATE["executed_trade_ids"].append(trade_id)
        AUTO_TRADE_STATE["executed_trade_ids"] = AUTO_TRADE_STATE["executed_trade_ids"][-500:]

    AUTO_TRADE_STATE["executions"].append(result)
    AUTO_TRADE_STATE["executions"] = AUTO_TRADE_STATE["executions"][-100:]

    persist_auto_trade_state()
    return result


def register_trade_result(outcome: str) -> Dict[str, Any]:
    load_auto_trade_state()

    if outcome == "SL":
        AUTO_TRADE_STATE["loss_streak"] = int(AUTO_TRADE_STATE.get("loss_streak", 0)) + 1
    elif outcome in ("TP1", "TP2", "TP1_ONLY", "BE"):
        AUTO_TRADE_STATE["loss_streak"] = 0

    if AUTO_TRADE_STATE["loss_streak"] >= int(AUTO_TRADE_STATE["cooldown_after_losses"]):
        AUTO_TRADE_STATE["cooldown_until"] = _now() + int(AUTO_TRADE_STATE["cooldown_minutes"]) * 60
        AUTO_TRADE_STATE["enabled"] = False

    persist_auto_trade_state()
    return get_public_status()
