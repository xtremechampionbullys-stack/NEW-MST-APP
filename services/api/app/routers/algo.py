"""
The algo gateway API — OpenAlgo's order surface, routed to the paper book.

Endpoint shapes follow OpenAlgo's vocabulary (placeorder / placesmartorder /
basketorder / splitorder, analyzer mode, webhook strategies) so anything
written against that pattern — a TradingView alert, a Python script, a
scheduler — points here with minimal translation. Two deliberate differences:

* Orders authenticate with an `X-API-Key` header rather than a key inside the
  JSON body. TradingView webhooks cannot set headers, which is exactly why the
  webhook route authenticates differently (a per-strategy secret in the body).
* Every fill is a paper fill and every response says so. The gateway's job is
  the part in front of the venue: authentication, the risk gate, approval,
  logging. Swapping the venue is a routing change.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from ..db.store import get_store
from ..providers.base import ProviderError
from ..providers.registry import get_registry
from ..services import algo_gateway as gw

router = APIRouter(prefix="/api/algo", tags=["algo"])


# ------------------------------------------------------------------ auth

async def _require_key(x_api_key: str | None) -> gw.ApiKey:
    key = gw.keyring.verify(x_api_key)
    if not key:
        detail = ("Missing or invalid X-API-Key header. Create a key with "
                  "POST /api/algo/keys (the plaintext is shown once). ")
        if gw.keyring.active_count == 0:
            detail += "No active keys exist yet."
        raise HTTPException(401, detail)
    return key


# ------------------------------------------------------------- execution

async def _stock_qty(symbol: str) -> float:
    """Net stock quantity currently held in `symbol` across open positions."""
    rows = await get_store().list_positions(open_only=True)
    qty = 0.0
    for r in rows:
        if r["symbol"] != symbol.upper():
            continue
        for leg in r["legs"]:
            if leg.get("kind") == "stock":
                qty += leg.get("quantity", 0)
    return qty


async def _fill(symbol: str, side: str, quantity: int, *, source: str,
                commit: bool = True) -> dict:
    """
    Quote, gate, fill. The single execution path every gateway order goes
    through — order, smart order, basket leg, split chunk, webhook signal and
    approved pending order all end here, so the gate cannot be side-stepped by
    picking a different endpoint.
    """
    try:
        quote = await get_registry().quote(symbol)
    except ProviderError as exc:
        raise HTTPException(502, f"No quote for {symbol}: {exc}") from exc

    both = quote.bid is not None and quote.ask is not None and quote.ask >= quote.bid > 0
    price = quote.mid if both else quote.last
    method = "mid of the live quote" if both else "last trade (no two-sided quote)"

    signed = quantity if side == "BUY" else -quantity
    order_value = abs(signed) * price
    held = await _stock_qty(symbol)
    position_after = (held + signed) * price

    verdict = gw.gate.check(symbol=symbol.upper(), order_value=order_value,
                            position_value_after=position_after,
                            source=source, commit=commit)
    if not verdict["allowed"]:
        if commit:
            gw.log_event("rejected", verdict["reason"], symbol=symbol.upper(),
                         source=source)
        return {"status": "rejected", "reason": verdict["reason"],
                "gate": gw.gate.config.__dict__, "venue": "paper"}

    if not commit:
        return {"status": "validated", "would_fill": round(price, 4),
                "method": method, "order_value": round(order_value, 2),
                "position_value_after": round(position_after, 2),
                "venue": "paper",
                "note": "Validate mode: nothing was booked and no budget consumed."}

    store = get_store()
    row = await store.add_position(
        symbol=symbol, name=f"{side} {quantity} {symbol.upper()} · {source}",
        legs=[{"kind": "stock", "quantity": signed, "entry_price": round(price, 4)}],
        notes=f"algo gateway ({source}) · paper fill at {method}")
    try:
        await store.record_mark(row["id"], underlying=quote.last,
                                mark_value=0.0, unrealized=0.0, entry=True)
    except Exception:                                              # noqa: BLE001
        pass
    gw.log_event("filled", f"{side} {quantity} {symbol.upper()} @ {price:.2f}",
                 symbol=symbol.upper(), source=source, position_id=row["id"])
    return {"status": "filled", "position_id": row["id"],
            "fill": {"price": round(price, 4), "method": method,
                     "source": quote.source},
            "venue": "paper",
            "note": ("Paper fill — no queue, partial fills, slippage or fees.")}


async def _execute_or_queue(symbol: str, side: str, quantity: int, *,
                            source: str, mode: str) -> dict:
    if mode == "validate":
        return await _fill(symbol, side, quantity, source=source, commit=False)
    if gw.gate.config.require_approval:
        import uuid as _uuid                                       # noqa: PLC0415
        oid = _uuid.uuid4().hex[:10]
        gw.pending_orders[oid] = {"id": oid, "symbol": symbol.upper(),
                                  "side": side, "quantity": quantity,
                                  "source": source, "queued_at": gw._now()}
        gw.log_event("queued", f"{side} {quantity} {symbol.upper()} awaiting approval",
                     symbol=symbol.upper(), source=source)
        return {"status": "pending_approval", "order_id": oid,
                "note": ("Semi-auto mode: queued for manual approval. The fill "
                         "price will be the quote at approval time, not now — "
                         "a price frozen while a human decides would be a "
                         "backdated fill.")}
    return await _fill(symbol, side, quantity, source=source)


# ---------------------------------------------------------------- orders

class OrderReq(BaseModel):
    symbol: str
    side: Literal["BUY", "SELL"]
    quantity: int = Field(ge=1, le=100000)
    mode: Literal["auto", "validate"] = "auto"


@router.post("/order")
async def place_order(req: OrderReq, x_api_key: str | None = Header(None)):
    key = await _require_key(x_api_key)
    return await _execute_or_queue(req.symbol, req.side, req.quantity,
                                   source=f"api:{key.name}", mode=req.mode)


class SmartOrderReq(BaseModel):
    symbol: str
    target_quantity: int = Field(ge=-100000, le=100000)
    mode: Literal["auto", "validate"] = "auto"


@router.post("/smart-order")
async def smart_order(req: SmartOrderReq, x_api_key: str | None = Header(None)):
    """
    Position-sizing semantics: state the position you want, the gateway
    computes the order. Held 300, target 100 → SELL 200. Held -50, target 50 →
    BUY 100. Already there → no order, and that is a success, not an error —
    idempotent signals are the entire point of smart orders.
    """
    key = await _require_key(x_api_key)
    held = await _stock_qty(req.symbol)
    delta = req.target_quantity - held
    if abs(delta) < 1:
        return {"status": "noop", "held": held, "target": req.target_quantity,
                "note": "Already at the target position. Nothing to do."}
    side = "BUY" if delta > 0 else "SELL"
    result = await _execute_or_queue(req.symbol, side, int(abs(delta)),
                                     source=f"smart:{key.name}", mode=req.mode)
    return {**result, "held_before": held, "target": req.target_quantity,
            "computed_order": f"{side} {int(abs(delta))}"}


class BasketReq(BaseModel):
    orders: list[OrderReq] = Field(min_length=1, max_length=20)


@router.post("/basket")
async def basket_order(req: BasketReq, x_api_key: str | None = Header(None)):
    """
    Orders execute independently: one rejection does not roll back the rest,
    and the response reports each leg. All-or-nothing baskets need a venue
    that supports atomic multi-leg execution, which a paper book can fake and
    therefore must not.
    """
    key = await _require_key(x_api_key)
    results = []
    for order in req.orders:
        results.append(await _execute_or_queue(
            order.symbol, order.side, order.quantity,
            source=f"basket:{key.name}", mode=order.mode))
    filled = sum(1 for r in results if r["status"] == "filled")
    return {"orders": results, "filled": filled,
            "rejected": sum(1 for r in results if r["status"] == "rejected"),
            "note": ("Legs execute independently — this is not atomic, and the "
                     "per-leg statuses are the real result.")}


class SplitReq(BaseModel):
    symbol: str
    side: Literal["BUY", "SELL"]
    quantity: int = Field(ge=1, le=100000)
    chunk: int = Field(ge=1, le=10000)


@router.post("/split")
async def split_order(req: SplitReq, x_api_key: str | None = Header(None)):
    """
    A large order cut into chunks. On paper this changes nothing — every chunk
    fills at the same mid, because there is no book to walk. The endpoint
    exists so strategies written against it are exercised end-to-end, and the
    response says the fills are identical for exactly that reason.
    """
    key = await _require_key(x_api_key)
    sizes = [req.chunk] * (req.quantity // req.chunk)
    if req.quantity % req.chunk:
        sizes.append(req.quantity % req.chunk)
    results = []
    for size in sizes:
        results.append(await _execute_or_queue(
            req.symbol, req.side, size, source=f"split:{key.name}", mode="auto"))
        if results[-1]["status"] == "rejected":
            break        # a gate rejection stops the ladder; blasting on is how caps get breached in spirit
    return {"chunks": results,
            "note": ("Paper fills have no market impact, so every chunk fills "
                     "at the same price — splitting matters on a live book, "
                     "not here. A gate rejection stops remaining chunks.")}


# -------------------------------------------------------------- approvals

@router.get("/pending")
async def pending():
    return {"pending": list(gw.pending_orders.values())}


@router.post("/pending/{order_id}/approve")
async def approve(order_id: str):
    order = gw.pending_orders.pop(order_id, None)
    if not order:
        raise HTTPException(404, "no such pending order")
    result = await _fill(order["symbol"], order["side"], order["quantity"],
                         source=order["source"] + " (approved)")
    return {**result, "approved_order": order}


@router.post("/pending/{order_id}/reject")
async def reject(order_id: str):
    order = gw.pending_orders.pop(order_id, None)
    if not order:
        raise HTTPException(404, "no such pending order")
    gw.log_event("approval_rejected", f"{order['side']} {order['quantity']} "
                 f"{order['symbol']} rejected by operator", **{"source": order["source"]})
    return {"rejected": order}


# --------------------------------------------------------------- api keys

class KeyReq(BaseModel):
    name: str = Field(min_length=1, max_length=64)


@router.post("/keys")
async def create_key(req: KeyReq):
    key, plaintext = gw.keyring.create(req.name)
    return {"id": key.id, "name": key.name, "key": plaintext,
            "note": ("Store this now — it is shown once and kept only as a "
                     "hash."), **gw.persistence_note()}


@router.get("/keys")
async def list_keys():
    return {"keys": gw.keyring.list(), **gw.persistence_note()}


@router.delete("/keys/{key_id}")
async def revoke_key(key_id: str):
    if not gw.keyring.revoke(key_id):
        raise HTTPException(404, "no such key")
    return {"revoked": key_id}


# -------------------------------------------------------------- risk gate

class GateReq(BaseModel):
    max_order_value: float = Field(gt=0)
    max_position_value: float = Field(gt=0)
    max_orders_per_day: int = Field(ge=1, le=10000)
    daily_loss_limit: float = Field(gt=0)
    require_approval: bool


@router.get("/gate")
async def get_gate():
    return {"config": gw.gate.config.__dict__,
            "orders_today": gw.gate.orders_today,
            "realized_today": round(gw.gate.realized_today, 2),
            "recent_rejections": list(gw.gate.rejections)[-20:],
            **gw.persistence_note()}


@router.put("/gate")
async def set_gate(req: GateReq):
    gw.gate.config = gw.GateConfig(**req.model_dump())
    gw.log_event("gate_changed", f"limits updated: {req.model_dump()}")
    return await get_gate()


# ------------------------------------------------------------- strategies

class StrategyReq(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    symbol: str
    quantity: int = Field(ge=1, le=100000)
    mode: Literal["long", "short", "both"] = "both"


@router.post("/strategies")
async def create_strategy(req: StrategyReq):
    s = gw.strategies.create(name=req.name, symbol=req.symbol,
                             quantity=req.quantity, mode=req.mode)
    return {**s.to_dict(include_secret=True),
            "webhook_path": f"/api/algo/webhook/{s.id}",
            "tradingview_alert_example": {
                "secret": s.secret, "action": "BUY",
                "symbol": s.symbol, "quantity": s.quantity,
            },
            "note": ("Point a TradingView alert (or anything that can POST "
                     "JSON) at the webhook path with this body. The secret "
                     "authenticates the strategy — webhooks cannot set "
                     "headers, so it travels in the payload."),
            **gw.persistence_note()}


@router.get("/strategies")
async def list_strategies():
    return {"strategies": [s.to_dict() for s in gw.strategies.list()],
            **gw.persistence_note()}


@router.patch("/strategies/{sid}")
async def toggle_strategy(sid: str, active: bool):
    s = gw.strategies.get(sid)
    if not s:
        raise HTTPException(404, "no such strategy")
    s.active = active
    gw.log_event("strategy_toggled", f"{s.name} -> {'active' if active else 'paused'}")
    return s.to_dict()


@router.delete("/strategies/{sid}")
async def delete_strategy(sid: str):
    if not gw.strategies.delete(sid):
        raise HTTPException(404, "no such strategy")
    return {"deleted": sid}


class WebhookSignal(BaseModel):
    secret: str
    action: Literal["BUY", "SELL", "EXIT"]
    symbol: str | None = None
    quantity: int | None = Field(None, ge=1, le=100000)


@router.post("/webhook/{sid}")
async def webhook(sid: str, sig: WebhookSignal):
    """
    The TradingView-alert entry point. No API key — the per-strategy secret in
    the body authenticates, and a wrong secret is a 403 that reveals nothing
    about whether the strategy id exists.
    """
    s = gw.strategies.get(sid)
    if not s or sig.secret != s.secret:
        raise HTTPException(403, "unknown strategy or bad secret")
    if not s.active:
        s.signals.append({"at": gw._now(), "action": sig.action,
                          "result": "ignored", "reason": "strategy paused"})
        return {"status": "ignored", "reason": "strategy is paused"}

    symbol = (sig.symbol or s.symbol).upper()
    quantity = sig.quantity or s.quantity

    if sig.action == "EXIT":
        held = await _stock_qty(symbol)
        if abs(held) < 1:
            s.signals.append({"at": gw._now(), "action": "EXIT",
                              "result": "noop", "reason": "no position"})
            return {"status": "noop", "note": f"No {symbol} position to exit."}
        side, quantity = ("SELL", int(held)) if held > 0 else ("BUY", int(-held))
    else:
        side = sig.action
        if s.mode == "long" and side == "SELL" and (await _stock_qty(symbol)) <= 0:
            s.signals.append({"at": gw._now(), "action": side,
                              "result": "blocked", "reason": "long-only strategy"})
            return {"status": "blocked",
                    "reason": "Strategy is long-only and holds nothing to sell."}
        if s.mode == "short" and side == "BUY" and (await _stock_qty(symbol)) >= 0:
            s.signals.append({"at": gw._now(), "action": side,
                              "result": "blocked", "reason": "short-only strategy"})
            return {"status": "blocked",
                    "reason": "Strategy is short-only and holds nothing to cover."}

    result = await _execute_or_queue(symbol, side, quantity,
                                     source=f"webhook:{s.name}", mode="auto")
    s.signals.append({"at": gw._now(), "action": sig.action, "symbol": symbol,
                      "quantity": quantity, "result": result["status"],
                      "reason": result.get("reason")})
    return result


# -------------------------------------------------------------- telemetry

@router.get("/traffic")
async def traffic_summary():
    return gw.traffic.summary()


@router.get("/status")
async def status():
    return {
        "keys_active": gw.keyring.active_count,
        "strategies": len(gw.strategies.list()),
        "pending_approvals": len(gw.pending_orders),
        "orders_today": gw.gate.orders_today,
        "realized_today": round(gw.gate.realized_today, 2),
        "gate": gw.gate.config.__dict__,
        "events": list(gw.events)[-40:],
        "venue": "paper",
        **gw.persistence_note(),
    }
