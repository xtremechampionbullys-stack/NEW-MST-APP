"""
The algo gateway: programmatic order flow with a real risk gate in front.

Modelled on OpenAlgo's architecture — the open-source algo platform whose core
idea is a unified, authenticated order API that external strategies (TradingView
alerts, scripts, schedulers) talk to, with monitoring and a safety layer between
the signal and the broker. What ports here and what does not:

* **Ports directly**: API keys, the risk gate, smart orders (target-position
  semantics), basket and split orders, semi-auto approval, validate mode,
  webhook strategy execution with a signal log, traffic/latency monitoring.
* **Adapted**: OpenAlgo routes to 30+ live brokers; this gateway routes to the
  paper book, because that is the execution venue this app actually has. The
  interface is the same shape, so pointing it at a live adapter later is a
  routing change, not a redesign.
* **Deliberately absent**: an invented "sandbox capital" figure. OpenAlgo's
  analyzer grants ₹1 crore of pretend margin; this paper book tracks positions
  and P&L without a cash ledger, and granting imaginary buying power is how
  paper strategies learn position sizes their owner cannot fund.

The risk gate exists in this codebase as a *specification* (the agents manifest
calls it "deliberately the least clever component in the system"). This module
is that component, implemented: hard limits that no other code path can raise,
checked before every fill, with every rejection logged. Its state is
process-local and resets on restart — reported in every response the same way
the store reports memory-vs-postgres, because a safety limit that silently
reset is worse than none.

On hashing: API keys are stored as SHA-256 digests. OpenAlgo uses Argon2 for
*passwords*, which is right for low-entropy secrets; these keys are 32 random
bytes, where a slow hash adds cost but no security. The plaintext is shown
exactly once, at creation.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import secrets
import statistics
import time
import uuid
from collections import deque
from dataclasses import dataclass, field


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _today() -> str:
    return dt.date.today().isoformat()


# --------------------------------------------------------------------------
# API keys
# --------------------------------------------------------------------------

@dataclass
class ApiKey:
    id: str
    name: str
    digest: str
    prefix: str                    # first 8 chars, for recognisability in lists
    created_at: str = field(default_factory=_now)
    last_used: str | None = None
    revoked: bool = False


class KeyRing:
    def __init__(self):
        self._keys: dict[str, ApiKey] = {}

    def create(self, name: str) -> tuple[ApiKey, str]:
        plaintext = "msk_" + secrets.token_urlsafe(32)
        digest = hashlib.sha256(plaintext.encode()).hexdigest()
        key = ApiKey(id=uuid.uuid4().hex[:12], name=name or "unnamed",
                     digest=digest, prefix=plaintext[:12])
        self._keys[key.id] = key
        return key, plaintext

    def verify(self, plaintext: str | None) -> ApiKey | None:
        if not plaintext:
            return None
        digest = hashlib.sha256(plaintext.encode()).hexdigest()
        for key in self._keys.values():
            if key.digest == digest and not key.revoked:
                key.last_used = _now()
                return key
        return None

    def revoke(self, key_id: str) -> bool:
        key = self._keys.get(key_id)
        if not key:
            return False
        key.revoked = True
        return True

    def list(self) -> list[dict]:
        return [{
            "id": k.id, "name": k.name, "prefix": k.prefix,
            "created_at": k.created_at, "last_used": k.last_used,
            "revoked": k.revoked,
        } for k in self._keys.values()]

    @property
    def active_count(self) -> int:
        return sum(1 for k in self._keys.values() if not k.revoked)


# --------------------------------------------------------------------------
# Risk gate
# --------------------------------------------------------------------------

@dataclass
class GateConfig:
    """
    The limits. Defaults are deliberately conservative — a gate that ships
    wide open is decoration.
    """
    max_order_value: float = 25_000.0        # $ notional per order
    max_position_value: float = 50_000.0     # $ notional per symbol, post-fill
    max_orders_per_day: int = 50
    daily_loss_limit: float = 1_000.0        # realized $ loss that halts the day
    require_approval: bool = False           # semi-auto: queue instead of fill


class RiskGate:
    """
    Every programmatic order passes through here. There is no bypass parameter,
    no admin flag and no code path around it — an override switch is the first
    thing a losing strategy reaches for.
    """

    def __init__(self):
        self.config = GateConfig()
        self._orders_today: dict[str, int] = {}
        self._realized_today: dict[str, float] = {}
        self.rejections: deque[dict] = deque(maxlen=200)

    def _bump(self, day: str):
        self._orders_today[day] = self._orders_today.get(day, 0) + 1

    def record_realized(self, amount: float):
        day = _today()
        self._realized_today[day] = self._realized_today.get(day, 0.0) + amount

    @property
    def realized_today(self) -> float:
        return self._realized_today.get(_today(), 0.0)

    @property
    def orders_today(self) -> int:
        return self._orders_today.get(_today(), 0)

    def check(self, *, symbol: str, order_value: float,
              position_value_after: float, source: str,
              commit: bool = True) -> dict:
        """
        Returns {"allowed": bool, "reason": ...}. Order counting happens on
        allow — a rejected order must not consume the day's budget, or a
        misconfigured strategy locks out a correct one. `commit=False` is
        validate mode: the same verdict, no side effects, so a dry run neither
        consumes the day's order budget nor logs a rejection as if it were real.
        """
        cfg = self.config
        reason = None
        if self.realized_today <= -abs(cfg.daily_loss_limit):
            reason = (f"daily loss circuit breaker: realized "
                      f"{self.realized_today:+.2f} today, limit "
                      f"-{abs(cfg.daily_loss_limit):.2f}. No further orders today.")
        elif self.orders_today >= cfg.max_orders_per_day:
            reason = (f"order count limit: {self.orders_today} orders today, "
                      f"cap {cfg.max_orders_per_day}.")
        elif order_value > cfg.max_order_value:
            reason = (f"order value {order_value:,.2f} exceeds the per-order "
                      f"cap {cfg.max_order_value:,.2f}.")
        elif abs(position_value_after) > cfg.max_position_value:
            reason = (f"position in {symbol} would be {position_value_after:,.2f} "
                      f"after this fill, over the per-symbol cap "
                      f"{cfg.max_position_value:,.2f}.")

        if reason:
            if commit:
                self.rejections.append({"at": _now(), "symbol": symbol,
                                        "source": source, "reason": reason,
                                        "order_value": round(order_value, 2)})
            return {"allowed": False, "reason": reason}
        if commit:
            self._bump(_today())
        return {"allowed": True, "reason": None}


# --------------------------------------------------------------------------
# Webhook strategies
# --------------------------------------------------------------------------

@dataclass
class WebhookStrategy:
    id: str
    name: str
    symbol: str
    quantity: int
    secret: str
    mode: str = "both"             # long | short | both
    active: bool = True
    created_at: str = field(default_factory=_now)
    signals: deque = field(default_factory=lambda: deque(maxlen=100))

    def to_dict(self, *, include_secret: bool = False) -> dict:
        d = {
            "id": self.id, "name": self.name, "symbol": self.symbol,
            "quantity": self.quantity, "mode": self.mode, "active": self.active,
            "created_at": self.created_at,
            "signals": list(self.signals)[-20:],
            "signal_count": len(self.signals),
        }
        if include_secret:
            d["secret"] = self.secret
        return d


class StrategyBook:
    def __init__(self):
        self._strategies: dict[str, WebhookStrategy] = {}

    def create(self, *, name: str, symbol: str, quantity: int,
               mode: str = "both") -> WebhookStrategy:
        s = WebhookStrategy(
            id=uuid.uuid4().hex[:10], name=name, symbol=symbol.upper(),
            quantity=quantity, mode=mode,
            secret=secrets.token_urlsafe(18))
        self._strategies[s.id] = s
        return s

    def get(self, sid: str) -> WebhookStrategy | None:
        return self._strategies.get(sid)

    def list(self) -> list[WebhookStrategy]:
        return list(self._strategies.values())

    def delete(self, sid: str) -> bool:
        return self._strategies.pop(sid, None) is not None


# --------------------------------------------------------------------------
# Traffic / latency monitor
# --------------------------------------------------------------------------

class TrafficMonitor:
    """
    A ring buffer of recent API calls with latency, aggregated on read.

    In-process on purpose: this monitors the API from inside the same event
    loop, so what it measures is handler time, not network time. That is the
    number a strategy author can act on — and the caveat is in the payload
    because a latency figure with an unstated denominator gets compared
    against broker round-trips it does not include.
    """

    def __init__(self, maxlen: int = 2000):
        self.calls: deque[dict] = deque(maxlen=maxlen)

    def record(self, *, path: str, method: str, status: int, ms: float):
        self.calls.append({"at": time.time(), "path": path, "method": method,
                           "status": status, "ms": round(ms, 2)})

    def summary(self) -> dict:
        by_path: dict[str, list[float]] = {}
        errors = 0
        for c in self.calls:
            by_path.setdefault(c["path"], []).append(c["ms"])
            if c["status"] >= 400:
                errors += 1

        def pct(vals: list[float], p: float) -> float:
            if not vals:
                return 0.0
            vals = sorted(vals)
            idx = min(len(vals) - 1, int(round(p * (len(vals) - 1))))
            return vals[idx]

        endpoints = [{
            "path": path,
            "calls": len(vals),
            "p50_ms": round(statistics.median(vals), 2),
            "p95_ms": round(pct(vals, 0.95), 2),
            "max_ms": round(max(vals), 2),
        } for path, vals in by_path.items()]
        endpoints.sort(key=lambda e: -e["calls"])

        return {
            "window": len(self.calls),
            "errors": errors,
            "endpoints": endpoints[:25],
            "note": ("Handler time measured inside the API process — request "
                     "parsing to response start. Network transit and any broker "
                     "round-trip are not in these numbers."),
        }


# --------------------------------------------------------------------------
# Singletons — process-local, like the in-memory store
# --------------------------------------------------------------------------

keyring = KeyRing()
gate = RiskGate()
strategies = StrategyBook()
traffic = TrafficMonitor()
pending_orders: dict[str, dict] = {}      # semi-auto approval queue
events: deque[dict] = deque(maxlen=300)   # order/gate/webhook event log


def log_event(kind: str, detail: str, **extra):
    events.append({"at": _now(), "kind": kind, "detail": detail, **extra})


def persistence_note() -> dict:
    return {
        "persistent": False,
        "note": ("Gateway state — keys, gate counters, strategies, logs — is "
                 "process-local and resets when the API restarts. Postgres "
                 "persistence for it is schema work, not a redesign."),
    }
