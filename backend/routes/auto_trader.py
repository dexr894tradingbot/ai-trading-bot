from fastapi import APIRouter
from pydantic import BaseModel
from typing import Optional
import os
import requests

from utils.auto_trader import (
    get_public_status,
    update_auto_trade_settings,
)

router = APIRouter()


class AutoTradeSettings(BaseModel):
    enabled: Optional[bool] = None
    account: Optional[str] = None
    mode: Optional[str] = None
    emergency_stop: Optional[bool] = None

    screenshot_mode: Optional[bool] = None
    private_details: Optional[bool] = None

    risk_mode: Optional[str] = None
    risk_per_split: Optional[float] = None
    split_trades: Optional[int] = None

    min_confidence: Optional[int] = None
    min_rr: Optional[float] = None

    max_open_trades: Optional[int] = None
    max_trades_per_day: Optional[int] = None

    daily_max_loss: Optional[float] = None
    daily_profit_target: Optional[float] = None

    cooldown_after_losses: Optional[int] = None
    cooldown_minutes: Optional[int] = None


@router.get("/status")
async def auto_trade_status():
    return get_public_status()


@router.post("/settings")
async def auto_trade_settings(req: AutoTradeSettings):
    return update_auto_trade_settings(**req.dict())


@router.get("/health")
async def auto_trade_health():
    return {
        "ok": True,
        "system": "smart_auto_trader",
    }

def _load_deriv_env():
    for path in ["backend/.env", ".env"]:
        try:
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if "=" in line and not line.startswith("#"):
                        k, v = line.split("=", 1)
                        os.environ[k] = v
        except FileNotFoundError:
            pass


def _fetch_deriv_accounts():
    _load_deriv_env()

    token = os.getenv("DERIV_PAT_TOKEN")
    app_id = os.getenv("DERIV_APP_ID")

    if not token or not app_id:
        return {"success": False, "connected": False, "error": "Missing DERIV_PAT_TOKEN or DERIV_APP_ID"}

    r = requests.get(
        "https://api.derivws.com/trading/v1/options/accounts",
        headers={
            "Authorization": f"Bearer {token}",
            "Deriv-App-ID": app_id,
        },
        timeout=15,
    )

    if r.status_code != 200:
        return {
            "success": False,
            "connected": False,
            "status_code": r.status_code,
            "error": r.text[:500],
        }

    return {"success": True, "connected": True, "raw": r.json()}


@router.get("/account")
async def get_account(account: str = "demo"):
    data = _fetch_deriv_accounts()
    if not data.get("success"):
        return {
            "success": False,
            "connected": False,
            "account": account,
            "error": data.get("error"),
            "status_code": data.get("status_code"),
        }

    accounts = data["raw"].get("data", [])
    wanted_type = "demo" if account == "demo" else "real"

    found = None
    for a in accounts:
        if a.get("account_type") == wanted_type:
            found = a
            break

    if not found:
        return {
            "success": False,
            "connected": False,
            "account": account,
            "error": f"No {wanted_type} account found",
            "accounts": accounts,
        }

    balance = float(found.get("balance") or 0)

    return {
        "success": True,
        "connected": True,
        "account": account,
        "account_id": found.get("account_id"),
        "account_type": found.get("account_type"),
        "balance": balance,
        "currency": found.get("currency", "USD"),
        "equity": balance,
        "profit": 0,
        "status": found.get("status"),
    }


@router.get("/execution-account")
async def execution_account(account: str = "demo"):
    from utils.deriv_orders import get_deriv_account

    data = get_deriv_account(account)

    return {
        "success": data.get("success", False),
        "account": account,
        "execution_ready": bool(data.get("success") and data.get("account_id")),
        "account_id": data.get("account_id"),
        "account_type": data.get("account_type"),
        "balance": data.get("balance"),
        "currency": data.get("currency"),
        "status": data.get("status"),
        "error": data.get("error"),
    }
