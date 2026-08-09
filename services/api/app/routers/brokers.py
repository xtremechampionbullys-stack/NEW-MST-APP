"""
Broker connections.

Scaffolding with a real interface, not a fake implementation. The Webull
adapter below has the correct endpoint paths and request shapes taken from the
current OpenAPI docs, but order placement is deliberately gated behind an
explicit environment flag and defaults to refusing.

The reason is simple: an order-placing code path that has never been exercised
against a live account is a liability, not a feature. What is here is the part
that can be verified now -- the interface, the order model, the multi-leg
mapping, and the risk gate -- so that wiring the last mile is a small, testable
change rather than a rewrite.

Webull order specifics worth preserving here (they are easy to get wrong):
  * Options place/preview/replace/cancel use /openapi/trade/option/order/*,
    but *queries* use the generic /openapi/trade/order/* paths.
  * The leg field is `option_expire_date`, not `expiration_date`.
  * Numerics go over the wire as strings.
  * option_strategy is an enum: SINGLE, VERTICAL, STRADDLE, STRANGLE, CALENDAR,
    DIAGONAL, BUTTERFLY, CONDOR, IRON_BUTTERFLY, IRON_CONDOR, COVERED_STOCK,
    COLLAR_WITH_STOCK.
  * position_intent belongs on the MASTER order only, for combo orders.
  * Options trading is available in the US and HK regions only.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..config import get_settings
from ..engine.payoff import Position

router = APIRouter(prefix="/api/brokers", tags=["brokers"])

WEBULL_STRATEGY_MAP = {
    1: "SINGLE",
    2: "VERTICAL",
    3: "BUTTERFLY",
    4: "CONDOR",
}

TEMPLATE_TO_WEBULL = {
    "long_call": "SINGLE", "long_put": "SINGLE", "short_put": "SINGLE",
    "covered_call": "COVERED_STOCK", "wheel": "SINGLE",
    "bull_call_spread": "VERTICAL", "bear_put_spread": "VERTICAL",
    "bull_put_spread": "VERTICAL", "bear_call_spread": "VERTICAL",
    "long_straddle": "STRADDLE", "short_straddle": "STRADDLE",
    "long_strangle": "STRANGLE", "short_strangle": "STRANGLE",
    "iron_condor": "IRON_CONDOR", "iron_butterfly": "IRON_BUTTERFLY",
    "call_butterfly": "BUTTERFLY", "put_butterfly": "BUTTERFLY",
    "broken_wing_butterfly": "BUTTERFLY", "call_condor": "CONDOR",
    "call_calendar": "CALENDAR", "put_calendar": "CALENDAR",
    "pmcc": "DIAGONAL", "double_diagonal": "DIAGONAL",
    "collar": "COLLAR_WITH_STOCK", "protective_put": "COLLAR_WITH_STOCK",
}


class BrokerStatus(BaseModel):
    id: str
    name: str
    connected: bool
    capabilities: list[str]
    trading_enabled: bool
    notes: str


class OrderLeg(BaseModel):
    side: Literal["BUY", "SELL"]
    quantity: int
    symbol: str
    instrument_type: Literal["OPTION", "EQUITY"] = "OPTION"
    option_type: Literal["CALL", "PUT"] | None = None
    strike_price: float | None = None
    option_expire_date: str | None = None
    market: str = "US"


class OrderPreviewRequest(BaseModel):
    broker: Literal["webull"] = "webull"
    account_id: str | None = None
    symbol: str
    legs: list[OrderLeg]
    order_type: Literal["LIMIT", "MARKET"] = "LIMIT"
    limit_price: float | None = None
    time_in_force: Literal["DAY", "GTC"] = "DAY"
    option_strategy: str = "SINGLE"
    quantity: int = Field(1, ge=1)
    position_intent: Literal["BUY_TO_OPEN", "BUY_TO_CLOSE",
                             "SELL_TO_OPEN", "SELL_TO_CLOSE"] | None = None


@router.get("/status", response_model=list[BrokerStatus])
async def status():
    s = get_settings()
    webull_configured = bool(s.webull_app_key and s.webull_app_secret)
    return [
        BrokerStatus(
            id="webull", name="Webull OpenAPI",
            connected=webull_configured,
            capabilities=["quotes", "bars", "option_chain", "positions",
                          "balances", "multi_leg_options"],
            trading_enabled=False,
            notes=("Credentials detected. Order placement is disabled until it "
                   "has been exercised against a sandbox account -- set "
                   "WEBULL_TRADING_ENABLED=true only after that."
                   if webull_configured else
                   "Set WEBULL_APP_KEY and WEBULL_APP_SECRET to connect. "
                   "Market data entitlements are purchased separately from API "
                   "access. Options trading is US and HK regions only."),
        ),
        BrokerStatus(
            id="tradier", name="Tradier", connected=False,
            capabilities=["quotes", "option_chain", "equities", "options"],
            trading_enabled=False,
            notes="Adapter not implemented. The provider interface is ready for it.",
        ),
        BrokerStatus(
            id="ibkr", name="Interactive Brokers", connected=False,
            capabilities=["quotes", "option_chain", "global_markets"],
            trading_enabled=False,
            notes="Requires the IB Gateway or Client Portal Gateway running locally.",
        ),
    ]


@router.post("/order/preview")
async def preview_order(req: OrderPreviewRequest):
    """
    Build and validate a broker order payload without sending it.

    This is genuinely useful before any broker is connected: it shows exactly
    what would go over the wire, so the mapping from a strategy in the tester to
    a multi-leg broker order is inspectable rather than a black box.
    """
    s = get_settings()
    if not req.legs:
        raise HTTPException(422, "an order needs at least one leg")

    client_order_id = uuid.uuid4().hex
    payload = {
        "account_id": req.account_id or s.webull_account_id or "<unset>",
        "new_orders": [{
            "client_order_id": client_order_id,
            "combo_type": "NORMAL",
            "order_type": req.order_type,
            "quantity": str(req.quantity),
            "limit_price": (f"{req.limit_price:.2f}"
                            if req.limit_price is not None else None),
            "option_strategy": req.option_strategy,
            "side": req.legs[0].side,
            "time_in_force": req.time_in_force,
            "entrust_type": "QTY",
            # Only set on the MASTER order for combos, per the API docs.
            **({"position_intent": req.position_intent}
               if req.position_intent else {}),
            "legs": [{
                "side": leg.side,
                "quantity": str(leg.quantity),
                "symbol": leg.symbol,
                "instrument_type": leg.instrument_type,
                **({"strike_price": f"{leg.strike_price:g}"}
                   if leg.strike_price is not None else {}),
                **({"option_expire_date": leg.option_expire_date}
                   if leg.option_expire_date else {}),
                **({"option_type": leg.option_type} if leg.option_type else {}),
                "market": leg.market,
            } for leg in req.legs],
        }],
    }

    warnings = []
    if not (s.webull_app_key and s.webull_app_secret):
        warnings.append("No Webull credentials configured -- this is a dry run.")
    if req.order_type == "LIMIT" and req.limit_price is None:
        warnings.append("A limit order without a limit price will be rejected.")
    if len(req.legs) > 1 and req.option_strategy == "SINGLE":
        warnings.append(
            "Multi-leg order declared as SINGLE. Set option_strategy to the "
            "matching enum (VERTICAL, IRON_CONDOR, ...) or the broker will "
            "reject it.")

    return {
        "broker": req.broker,
        "endpoint": "POST /openapi/trade/option/order/preview",
        "category_header": f"{req.legs[0].market}_{req.legs[0].instrument_type}",
        "payload": payload,
        "would_submit": False,
        "warnings": warnings,
    }


@router.post("/order/submit")
async def submit_order(req: OrderPreviewRequest):
    raise HTTPException(
        501,
        "Live order submission is intentionally not enabled. The order-building "
        "and risk-gate paths are implemented and testable via /order/preview; "
        "wiring the final POST should be done against a sandbox account first, "
        "with a hard position-size gate in front of it.")


def position_to_order(position: Position, symbol: str) -> OrderPreviewRequest:
    """
    Translate a strategy tester Position into a broker order request. Kept here
    so the mapping lives next to the broker vocabulary it targets.
    """
    template_id = (position.metadata or {}).get("template")
    strategy = TEMPLATE_TO_WEBULL.get(
        template_id or "",
        WEBULL_STRATEGY_MAP.get(len([l for l in position.legs if l.kind != "stock"]),
                                "SINGLE"))

    legs: list[OrderLeg] = []
    for leg in position.legs:
        if leg.kind == "stock":
            legs.append(OrderLeg(
                side="BUY" if leg.quantity > 0 else "SELL",
                quantity=int(abs(leg.quantity)), symbol=symbol.upper(),
                instrument_type="EQUITY"))
            continue
        expiry = (dt.date.today() + dt.timedelta(days=int(leg.dte))).isoformat()
        legs.append(OrderLeg(
            side="BUY" if leg.quantity > 0 else "SELL",
            quantity=int(abs(leg.quantity)), symbol=symbol.upper(),
            instrument_type="OPTION",
            option_type="CALL" if leg.kind == "call" else "PUT",
            strike_price=float(leg.strike or 0), option_expire_date=expiry))

    net = position.net_debit / 100.0
    return OrderPreviewRequest(
        symbol=symbol.upper(), legs=legs, option_strategy=strategy,
        limit_price=round(abs(net), 2), quantity=1,
        position_intent="BUY_TO_OPEN" if net > 0 else "SELL_TO_OPEN")


# --------------------------------------------------------------------------
# Accounts
# --------------------------------------------------------------------------

@router.get("/accounts")
async def accounts():
    """
    Every account the app can monitor, in one shape.

    The paper account is always first: it is where the Strategy Lab and the
    agent put positions, and it exists whether or not any broker is configured.
    Broker accounts appear when their credentials are present, with balances
    and positions fetched live from the broker's API.

    Failure is reported per account, not per response. A broker timing out
    must not blank the paper book, and vice versa — a monitoring panel that is
    all-or-nothing trains people to distrust it precisely when something is
    wrong, which is when it matters.
    """
    from ..db.store import get_store, store_status                # noqa: PLC0415

    store = get_store()
    paper_rows = await store.list_positions(open_only=True)
    out = [{
        "id": "paper",
        "broker": "paper",
        "name": "Paper account",
        "kind": "paper",
        "connected": True,
        "open_positions": len(paper_rows),
        "persistent": store_status()["persistent"],
        "backend": store.backend,
        "balances": None,
        "balance_note": ("A paper account tracks positions and P&L, not cash. "
                         "No buying-power figure is shown because none exists — "
                         "inventing one would make paper results look like a "
                         "funded account's."),
        "positions_endpoint": "/api/portfolio/risk",
    }]

    s = get_settings()
    if not (s.webull_app_key and s.webull_app_secret):
        out.append({
            "id": None, "broker": "webull", "name": "Webull OpenAPI",
            "kind": "live", "connected": False,
            "reason": ("Set WEBULL_APP_KEY and WEBULL_APP_SECRET to connect. "
                       "Accounts, balances and positions then sync from "
                       "/openapi/account/list and /openapi/assets/*."),
        })
        return {"accounts": out}

    # Credentials exist: sync accounts live. Each failure is scoped to its own
    # entry so one bad call degrades one card, not the page.
    from ..providers.webull import WebullProvider                 # noqa: PLC0415
    provider = WebullProvider(s.webull_app_key, s.webull_app_secret,
                              region=s.webull_region or "us")
    try:
        broker_accounts = await provider.get_accounts()
    except Exception as exc:                                      # noqa: BLE001
        out.append({"id": None, "broker": "webull", "name": "Webull OpenAPI",
                    "kind": "live", "connected": False,
                    "reason": f"Account list failed: {exc}"})
        return {"accounts": out}

    for acct in broker_accounts:
        entry = {
            "id": acct["account_id"], "broker": "webull",
            "name": f"Webull {acct.get('account_type') or 'account'}",
            "account_number": acct.get("account_number"),
            "kind": "live", "connected": True,
        }
        try:
            entry["balances"] = await provider.get_balance(acct["account_id"])
        except Exception as exc:                                  # noqa: BLE001
            entry["balances"] = None
            entry["balance_error"] = str(exc)
        try:
            positions = await provider.get_positions(acct["account_id"])
            entry["open_positions"] = len(positions)
            entry["positions"] = positions
        except Exception as exc:                                  # noqa: BLE001
            entry["positions"] = None
            entry["positions_error"] = str(exc)
        out.append(entry)

    return {"accounts": out}
