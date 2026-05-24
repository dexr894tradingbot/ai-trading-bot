from fastapi import APIRouter
from pydantic import BaseModel
from typing import Optional

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

@router.get("/account")
async def get_account(account: str = "demo"):
    return {
        "success": True,
        "account": account,
        "balance": 10000 if account == "demo" else 2500,
        "currency": "USD",
        "equity": 10000 if account == "demo" else 2500,
        "profit": 0,
        "connected": True
    }

