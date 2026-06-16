import os
import json
import asyncio
import requests
import websockets
from typing import Dict, Any


DERIV_BASE = "https://api.derivws.com"


def load_deriv_env() -> None:
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


def deriv_headers() -> Dict[str, str]:
    load_deriv_env()
    token = os.getenv("DERIV_PAT_TOKEN")
    app_id = os.getenv("DERIV_APP_ID")

    if not token or not app_id:
        raise RuntimeError("Missing DERIV_PAT_TOKEN or DERIV_APP_ID")

    return {
        "Authorization": f"Bearer {token}",
        "Deriv-App-ID": app_id,
        "Content-Type": "application/json",
    }


def fetch_deriv_accounts() -> Dict[str, Any]:
    r = requests.get(
        f"{DERIV_BASE}/trading/v1/options/accounts",
        headers=deriv_headers(),
        timeout=15,
    )

    if r.status_code != 200:
        return {"success": False, "status_code": r.status_code, "error": r.text[:500]}

    return {"success": True, "data": r.json().get("data", [])}


def get_deriv_account(account: str = "demo") -> Dict[str, Any]:
    account = "real" if account == "real" else "demo"
    accounts = fetch_deriv_accounts()

    if not accounts.get("success"):
        return accounts

    for a in accounts.get("data", []):
        if a.get("account_type") == account:
            return {
                "success": True,
                "account": account,
                "account_id": a.get("account_id"),
                "account_type": a.get("account_type"),
                "balance": float(a.get("balance") or 0),
                "currency": a.get("currency", "USD"),
                "status": a.get("status"),
            }

    return {"success": False, "error": f"No {account} account found"}


def get_deriv_ws_url(account: str = "demo") -> Dict[str, Any]:
    acct = get_deriv_account(account)
    if not acct.get("success"):
        return acct

    r = requests.post(
        f"{DERIV_BASE}/trading/v1/options/accounts/{acct['account_id']}/otp",
        headers=deriv_headers(),
        timeout=15,
    )

    if r.status_code != 200:
        return {"success": False, "status_code": r.status_code, "error": r.text[:500]}

    return {
        "success": True,
        "account": account,
        "account_id": acct["account_id"],
        "url": r.json()["data"]["url"],
    }


async def place_deriv_option_trade(
    account: str,
    symbol: str,
    direction: str,
    stake: float,
    duration: int = 5,
    duration_unit: str = "m",
) -> Dict[str, Any]:
    """
    Places one Deriv option contract using the new PAT + OTP WebSocket flow.
    BUY signal = CALL
    SELL signal = PUT
    """
    account = "real" if account == "real" else "demo"
    direction = str(direction or "").upper()
    contract_type = "CALL" if direction == "BUY" else "PUT"

    if stake <= 0:
        return {"success": False, "error": "stake must be greater than 0"}

    ws_data = get_deriv_ws_url(account)
    if not ws_data.get("success"):
        return ws_data

    proposal_req = {
        "proposal": 1,
        "amount": float(stake),
        "basis": "stake",
        "contract_type": contract_type,
        "currency": "USD",
        "duration": int(duration),
        "duration_unit": duration_unit,
        "underlying_symbol": symbol,
    }

    async with websockets.connect(ws_data["url"]) as ws:
        await ws.send(json.dumps(proposal_req))
        proposal_msg = json.loads(await ws.recv())

        if proposal_msg.get("error"):
            return {
                "success": False,
                "stage": "proposal",
                "error": proposal_msg.get("error"),
                "request": proposal_req,
                "response": proposal_msg,
            }

        proposal = proposal_msg.get("proposal") or {}
        proposal_id = proposal.get("id")
        ask_price = proposal.get("ask_price")

        if not proposal_id:
            return {
                "success": False,
                "stage": "proposal",
                "error": "No proposal id returned",
                "response": proposal_msg,
            }

        buy_req = {
            "buy": proposal_id,
            "price": ask_price,
        }

        await ws.send(json.dumps(buy_req))
        buy_msg = json.loads(await ws.recv())

        if buy_msg.get("error"):
            return {
                "success": False,
                "stage": "buy",
                "error": buy_msg.get("error"),
                "proposal": proposal,
                "response": buy_msg,
            }

        buy = buy_msg.get("buy") or {}

        return {
            "success": True,
            "account": account,
            "account_id": ws_data.get("account_id"),
            "symbol": symbol,
            "direction": direction,
            "contract_type": contract_type,
            "stake": float(stake),
            "duration": int(duration),
            "duration_unit": duration_unit,
            "proposal": proposal,
            "buy": buy,
            "contract_id": buy.get("contract_id"),
            "transaction_id": buy.get("transaction_id"),
            "balance_after": buy.get("balance_after"),
            "raw": buy_msg,
        }


def place_deriv_option_trade_sync(*args, **kwargs) -> Dict[str, Any]:
    return asyncio.run(place_deriv_option_trade(*args, **kwargs))
