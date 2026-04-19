from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

import psycopg2
import psycopg2.extras


DATABASE_URL = os.getenv("DATABASE_URL")


def get_connection():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is missing")
    return psycopg2.connect(DATABASE_URL)


def init_db() -> None:
    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS bot_trades (
            trade_id TEXT PRIMARY KEY,
            symbol TEXT NOT NULL,
            timeframe TEXT NOT NULL,
            direction TEXT,
            entry DOUBLE PRECISION,
            sl DOUBLE PRECISION,
            tp DOUBLE PRECISION,
            tp1 DOUBLE PRECISION,
            tp2 DOUBLE PRECISION,
            confidence INTEGER,
            reason TEXT,
            entry_type TEXT,
            mode TEXT,
            r_multiple DOUBLE PRECISION,
            tp1_r_multiple DOUBLE PRECISION,
            quality_score INTEGER,
            quality_grade TEXT,
            quality_stars TEXT,
            status TEXT NOT NULL,
            outcome TEXT,
            opened_at BIGINT,
            closed_at BIGINT,
            closed_price DOUBLE PRECISION,
            tp1_hit BOOLEAN DEFAULT FALSE,
            tp1_hit_at BIGINT,
            tp1_price DOUBLE PRECISION,
            progress_pct DOUBLE PRECISION DEFAULT 0,
            week_key TEXT,
            day_key TEXT,
            zone_used_json JSONB,
            meta_json JSONB,
            raw_json JSONB,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS bot_state (
            state_key TEXT PRIMARY KEY,
            state_value JSONB NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        """
    )

    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_bot_trades_status
        ON bot_trades(status);
        """
    )

    cur.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_bot_trades_symbol_timeframe
        ON bot_trades(symbol, timeframe);
        """
    )

    conn.commit()
    cur.close()
    conn.close()


def upsert_trade(trade: Dict[str, Any]) -> None:
    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        """
        INSERT INTO bot_trades (
            trade_id, symbol, timeframe, direction, entry, sl, tp, tp1, tp2,
            confidence, reason, entry_type, mode, r_multiple, tp1_r_multiple,
            quality_score, quality_grade, quality_stars, status, outcome,
            opened_at, closed_at, closed_price, tp1_hit, tp1_hit_at, tp1_price,
            progress_pct, week_key, day_key, zone_used_json, meta_json, raw_json,
            updated_at
        )
        VALUES (
            %(trade_id)s, %(symbol)s, %(timeframe)s, %(direction)s, %(entry)s, %(sl)s,
            %(tp)s, %(tp1)s, %(tp2)s, %(confidence)s, %(reason)s, %(entry_type)s,
            %(mode)s, %(r_multiple)s, %(tp1_r_multiple)s, %(quality_score)s,
            %(quality_grade)s, %(quality_stars)s, %(status)s, %(outcome)s,
            %(opened_at)s, %(closed_at)s, %(closed_price)s, %(tp1_hit)s,
            %(tp1_hit_at)s, %(tp1_price)s, %(progress_pct)s, %(week_key)s,
            %(day_key)s, %(zone_used_json)s, %(meta_json)s, %(raw_json)s,
            CURRENT_TIMESTAMP
        )
        ON CONFLICT (trade_id)
        DO UPDATE SET
            symbol = EXCLUDED.symbol,
            timeframe = EXCLUDED.timeframe,
            direction = EXCLUDED.direction,
            entry = EXCLUDED.entry,
            sl = EXCLUDED.sl,
            tp = EXCLUDED.tp,
            tp1 = EXCLUDED.tp1,
            tp2 = EXCLUDED.tp2,
            confidence = EXCLUDED.confidence,
            reason = EXCLUDED.reason,
            entry_type = EXCLUDED.entry_type,
            mode = EXCLUDED.mode,
            r_multiple = EXCLUDED.r_multiple,
            tp1_r_multiple = EXCLUDED.tp1_r_multiple,
            quality_score = EXCLUDED.quality_score,
            quality_grade = EXCLUDED.quality_grade,
            quality_stars = EXCLUDED.quality_stars,
            status = EXCLUDED.status,
            outcome = EXCLUDED.outcome,
            opened_at = EXCLUDED.opened_at,
            closed_at = EXCLUDED.closed_at,
            closed_price = EXCLUDED.closed_price,
            tp1_hit = EXCLUDED.tp1_hit,
            tp1_hit_at = EXCLUDED.tp1_hit_at,
            tp1_price = EXCLUDED.tp1_price,
            progress_pct = EXCLUDED.progress_pct,
            week_key = EXCLUDED.week_key,
            day_key = EXCLUDED.day_key,
            zone_used_json = EXCLUDED.zone_used_json,
            meta_json = EXCLUDED.meta_json,
            raw_json = EXCLUDED.raw_json,
            updated_at = CURRENT_TIMESTAMP;
        """,
        {
            "trade_id": trade.get("trade_id"),
            "symbol": trade.get("symbol"),
            "timeframe": trade.get("timeframe"),
            "direction": trade.get("direction"),
            "entry": trade.get("entry"),
            "sl": trade.get("sl"),
            "tp": trade.get("tp"),
            "tp1": trade.get("tp1"),
            "tp2": trade.get("tp2"),
            "confidence": trade.get("confidence"),
            "reason": trade.get("reason"),
            "entry_type": trade.get("entry_type"),
            "mode": trade.get("mode"),
            "r_multiple": trade.get("r_multiple"),
            "tp1_r_multiple": trade.get("tp1_r_multiple"),
            "quality_score": trade.get("quality_score"),
            "quality_grade": trade.get("quality_grade"),
            "quality_stars": trade.get("quality_stars"),
            "status": trade.get("status"),
            "outcome": trade.get("outcome"),
            "opened_at": trade.get("opened_at"),
            "closed_at": trade.get("closed_at"),
            "closed_price": trade.get("closed_price"),
            "tp1_hit": bool(trade.get("tp1_hit", False)),
            "tp1_hit_at": trade.get("tp1_hit_at"),
            "tp1_price": trade.get("tp1_price"),
            "progress_pct": trade.get("progress_pct", 0),
            "week_key": trade.get("week_key"),
            "day_key": trade.get("day_key"),
            "zone_used_json": psycopg2.extras.Json(trade.get("zone_used")),
            "meta_json": psycopg2.extras.Json(trade.get("meta", {})),
            "raw_json": psycopg2.extras.Json(trade),
        },
    )

    conn.commit()
    cur.close()
    conn.close()


def load_active_trades() -> List[Dict[str, Any]]:
    conn = get_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute(
        """
        SELECT *
        FROM bot_trades
        WHERE status = 'OPEN'
        ORDER BY opened_at ASC;
        """
    )

    rows = cur.fetchall()

    cur.close()
    conn.close()

    results: List[Dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["zone_used"] = item.pop("zone_used_json", None)
        item["meta"] = item.pop("meta_json", None) or {}
        item.pop("raw_json", None)
        item.pop("created_at", None)
        item.pop("updated_at", None)
        results.append(item)
    return results


def load_recent_history(limit: int = 500) -> List[Dict[str, Any]]:
    conn = get_connection()
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute(
        """
        SELECT *
        FROM bot_trades
        ORDER BY COALESCE(closed_at, opened_at) DESC
        LIMIT %s;
        """,
        (limit,),
    )

    rows = cur.fetchall()

    cur.close()
    conn.close()

    results: List[Dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        item["zone_used"] = item.pop("zone_used_json", None)
        item["meta"] = item.pop("meta_json", None) or {}
        item.pop("raw_json", None)
        item.pop("created_at", None)
        item.pop("updated_at", None)
        results.append(item)
    return list(reversed(results))


def save_json_state(state_key: str, state_value: Dict[str, Any]) -> None:
    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        """
        INSERT INTO bot_state (state_key, state_value, updated_at)
        VALUES (%s, %s, CURRENT_TIMESTAMP)
        ON CONFLICT (state_key)
        DO UPDATE SET
            state_value = EXCLUDED.state_value,
            updated_at = CURRENT_TIMESTAMP;
        """,
        (state_key, psycopg2.extras.Json(state_value)),
    )

    conn.commit()
    cur.close()
    conn.close()


def load_json_state(state_key: str) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    cur = conn.cursor()

    cur.execute(
        """
        SELECT state_value
        FROM bot_state
        WHERE state_key = %s;
        """,
        (state_key,),
    )

    row = cur.fetchone()

    cur.close()
    conn.close()

    if not row:
        return None
    return row[0]