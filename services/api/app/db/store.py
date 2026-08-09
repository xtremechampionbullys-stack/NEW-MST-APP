"""
Repository layer.

One interface, two backends. `SqlStore` talks to the database; `MemoryStore`
holds the same data in dicts. `get_store()` picks whichever is available, so
call sites never branch on it and the API behaves identically either way --
data just does not survive a restart without a database.

Keeping both implementations honest is the point: the in-memory version applies
the same uniqueness rules the SQL schema does (one IV observation per symbol per
day, one earnings row per symbol per date), because a fallback that quietly
accepts data the real backend would reject is worse than no fallback.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from typing import Any, Protocol

from sqlalchemy import delete, select

from .engine import get_database
from .models import (
    BacktestRun,
    EarningsEventRow,
    IVHistory,
    Position,
    PositionMark,
    SavedStrategy,
)

log = logging.getLogger(__name__)


class Store(Protocol):
    backend: str

    async def save_strategy(self, **kw) -> dict: ...
    async def list_strategies(self, symbol: str | None = None) -> list[dict]: ...
    async def get_strategy(self, sid: str) -> dict | None: ...
    async def delete_strategy(self, sid: str) -> bool: ...

    async def add_position(self, **kw) -> dict: ...
    async def list_positions(self, open_only: bool = True) -> list[dict]: ...
    async def close_position(self, pid: str, exit_credit: float | None) -> dict | None: ...
    async def delete_position(self, pid: str) -> bool: ...
    async def record_mark(self, position_id: str, **kw) -> None: ...
    async def position_marks(self, position_id: str, limit: int = 400) -> list[dict]: ...

    async def record_iv(self, symbol: str, atm_iv: float, **kw) -> dict: ...
    async def iv_history(self, symbol: str, days: int = 365) -> list[dict]: ...

    async def upsert_earnings(self, rows: list[dict]) -> int: ...
    async def get_earnings(self, symbol: str, since: dt.date | None = None,
                           until: dt.date | None = None) -> list[dict]: ...

    async def save_backtest(self, result: dict, label: str = "") -> dict: ...
    async def list_backtests(self, symbol: str | None = None,
                             limit: int = 50) -> list[dict]: ...
    async def get_backtest(self, run_id: str) -> dict | None: ...
    async def delete_backtest(self, run_id: str) -> bool: ...


# --------------------------------------------------------------------------
# In-memory
# --------------------------------------------------------------------------

class MemoryStore:
    backend = "memory"

    def __init__(self):
        self._strategies: dict[str, dict] = {}
        self._positions: dict[str, dict] = {}
        self._marks: dict[str, list[dict]] = {}
        self._iv: dict[tuple[str, dt.date], dict] = {}
        self._earnings: dict[tuple[str, dt.date], dict] = {}
        self._backtests: dict[str, dict] = {}

    # -- strategies --
    async def save_strategy(self, **kw) -> dict:
        sid = uuid.uuid4().hex
        row = {
            "id": sid, "symbol": kw["symbol"].upper(),
            "template_id": kw.get("template_id"), "name": kw["name"],
            "params": kw.get("params") or {}, "legs": kw.get("legs") or [],
            "entry_spot": kw.get("entry_spot"), "notes": kw.get("notes", ""),
            "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        }
        self._strategies[sid] = row
        return row

    async def list_strategies(self, symbol: str | None = None) -> list[dict]:
        rows = list(self._strategies.values())
        if symbol:
            rows = [r for r in rows if r["symbol"] == symbol.upper()]
        return sorted(rows, key=lambda r: r["created_at"], reverse=True)

    async def get_strategy(self, sid: str) -> dict | None:
        return self._strategies.get(sid)

    async def delete_strategy(self, sid: str) -> bool:
        return self._strategies.pop(sid, None) is not None

    # -- positions --
    async def add_position(self, **kw) -> dict:
        pid = uuid.uuid4().hex
        row = {
            "id": pid, "symbol": kw["symbol"].upper(), "name": kw.get("name", "Position"),
            "legs": kw.get("legs") or [],
            "opened": (kw.get("opened") or dt.date.today().isoformat()),
            "closed": None, "entry_debit": kw.get("entry_debit"),
            "exit_credit": None, "broker": kw.get("broker"),
            "notes": kw.get("notes", ""),
            "expectation": kw.get("expectation"),
        }
        self._positions[pid] = row
        return row

    async def list_positions(self, open_only: bool = True) -> list[dict]:
        rows = list(self._positions.values())
        if open_only:
            rows = [r for r in rows if not r["closed"]]
        return rows

    async def close_position(self, pid: str, exit_credit: float | None) -> dict | None:
        row = self._positions.get(pid)
        if not row:
            return None
        row["closed"] = dt.date.today().isoformat()
        row["exit_credit"] = exit_credit
        return row

    async def delete_position(self, pid: str) -> bool:
        self._marks.pop(pid, None)
        return self._positions.pop(pid, None) is not None

    async def record_mark(self, position_id: str, **kw) -> None:
        self._marks.setdefault(position_id, []).append({
            "ts": dt.datetime.now(dt.timezone.utc).isoformat(), **kw})

    async def position_marks(self, position_id: str, limit: int = 400) -> list[dict]:
        return self._marks.get(position_id, [])[-limit:]

    # -- iv history --
    async def record_iv(self, symbol: str, atm_iv: float, **kw) -> dict:
        day = kw.get("observed_on") or dt.date.today()
        key = (symbol.upper(), day)
        row = {
            "symbol": symbol.upper(), "date": day.isoformat(),
            "atm_iv_30d": atm_iv, "realized_20d": kw.get("realized_20d"),
            "spot": kw.get("spot"), "source": kw.get("source", ""),
        }
        self._iv[key] = row          # same-day re-observation overwrites
        return row

    async def iv_history(self, symbol: str, days: int = 365) -> list[dict]:
        cutoff = dt.date.today() - dt.timedelta(days=days)
        rows = [r for (sym, day), r in self._iv.items()
                if sym == symbol.upper() and day >= cutoff]
        return sorted(rows, key=lambda r: r["date"])

    # -- earnings --
    async def upsert_earnings(self, rows: list[dict]) -> int:
        n = 0
        for r in rows:
            day = r["event_date"]
            if isinstance(day, str):
                day = dt.date.fromisoformat(day[:10])
            self._earnings[(r["symbol"].upper(), day)] = {**r, "event_date": day.isoformat()}
            n += 1
        return n

    async def get_earnings(self, symbol: str, since=None, until=None) -> list[dict]:
        out = []
        for (sym, day), r in self._earnings.items():
            if sym != symbol.upper():
                continue
            if since and day < since:
                continue
            if until and day > until:
                continue
            out.append(r)
        return sorted(out, key=lambda r: r["event_date"])

    # -- backtests --
    async def save_backtest(self, result: dict, label: str = "") -> dict:
        rid = uuid.uuid4().hex
        row = {
            "id": rid, "symbol": result["symbol"],
            "template_id": result["template"], "label": label,
            "rules": result["rules"], "metrics": result["metrics"],
            "start": result["start"], "end": result["end"],
            "quote_source": result["quote_source"],
            "is_historical_pricing": result["is_historical_pricing"],
            "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "trades": result["trades"], "equity_curve": result["equity_curve"],
        }
        self._backtests[rid] = row
        return row

    async def list_backtests(self, symbol: str | None = None,
                             limit: int = 50) -> list[dict]:
        rows = list(self._backtests.values())
        if symbol:
            rows = [r for r in rows if r["symbol"] == symbol.upper()]
        rows.sort(key=lambda r: r["created_at"], reverse=True)
        return [{k: v for k, v in r.items() if k not in ("trades", "equity_curve")}
                for r in rows[:limit]]

    async def get_backtest(self, run_id: str) -> dict | None:
        return self._backtests.get(run_id)

    async def delete_backtest(self, run_id: str) -> bool:
        return self._backtests.pop(run_id, None) is not None


# --------------------------------------------------------------------------
# SQL
# --------------------------------------------------------------------------

class SqlStore:
    backend = "sql"

    def __init__(self, session_factory):
        self._sf = session_factory

    # -- strategies --
    async def save_strategy(self, **kw) -> dict:
        async with self._sf() as s:
            row = SavedStrategy(
                symbol=kw["symbol"].upper(), template_id=kw.get("template_id"),
                name=kw["name"], params=kw.get("params") or {},
                legs=kw.get("legs") or [], entry_spot=kw.get("entry_spot"),
                notes=kw.get("notes", ""))
            s.add(row)
            await s.commit()
            return row.to_dict()

    async def list_strategies(self, symbol: str | None = None) -> list[dict]:
        async with self._sf() as s:
            q = select(SavedStrategy).order_by(SavedStrategy.created_at.desc())
            if symbol:
                q = q.where(SavedStrategy.symbol == symbol.upper())
            return [r.to_dict() for r in (await s.execute(q)).scalars()]

    async def get_strategy(self, sid: str) -> dict | None:
        async with self._sf() as s:
            row = await s.get(SavedStrategy, sid)
            return row.to_dict() if row else None

    async def delete_strategy(self, sid: str) -> bool:
        async with self._sf() as s:
            row = await s.get(SavedStrategy, sid)
            if not row:
                return False
            await s.delete(row)
            await s.commit()
            return True

    # -- positions --
    async def add_position(self, **kw) -> dict:
        async with self._sf() as s:
            opened = kw.get("opened")
            row = Position(
                symbol=kw["symbol"].upper(), name=kw.get("name", "Position"),
                legs=kw.get("legs") or [],
                opened_at=(dt.date.fromisoformat(opened) if isinstance(opened, str)
                           else (opened or dt.date.today())),
                entry_debit=kw.get("entry_debit"), broker=kw.get("broker"),
                notes=kw.get("notes", ""),
                expectation=kw.get("expectation"))
            s.add(row)
            await s.commit()
            return row.to_dict()

    async def list_positions(self, open_only: bool = True) -> list[dict]:
        async with self._sf() as s:
            q = select(Position)
            if open_only:
                q = q.where(Position.closed_at.is_(None))
            return [r.to_dict() for r in (await s.execute(q)).scalars()]

    async def close_position(self, pid: str, exit_credit: float | None) -> dict | None:
        async with self._sf() as s:
            row = await s.get(Position, pid)
            if not row:
                return None
            row.closed_at = dt.date.today()
            row.exit_credit = exit_credit
            await s.commit()
            return row.to_dict()

    async def delete_position(self, pid: str) -> bool:
        async with self._sf() as s:
            row = await s.get(Position, pid)
            if not row:
                return False
            await s.execute(delete(PositionMark).where(PositionMark.position_id == pid))
            await s.delete(row)
            await s.commit()
            return True

    async def record_mark(self, position_id: str, **kw) -> None:
        async with self._sf() as s:
            s.add(PositionMark(position_id=position_id, **kw))
            await s.commit()

    async def position_marks(self, position_id: str, limit: int = 400) -> list[dict]:
        async with self._sf() as s:
            q = (select(PositionMark)
                 .where(PositionMark.position_id == position_id)
                 .order_by(PositionMark.ts.desc()).limit(limit))
            rows = [r.to_dict() for r in (await s.execute(q)).scalars()]
            return list(reversed(rows))

    # -- iv history --
    async def record_iv(self, symbol: str, atm_iv: float, **kw) -> dict:
        day = kw.get("observed_on") or dt.date.today()
        async with self._sf() as s:
            existing = (await s.execute(
                select(IVHistory).where(IVHistory.symbol == symbol.upper(),
                                        IVHistory.observed_on == day))).scalar_one_or_none()
            if existing:
                # Re-observing the same day updates rather than duplicating --
                # otherwise a service restart would double-weight that day in
                # every rank calculation that follows.
                existing.atm_iv_30d = atm_iv
                existing.realized_20d = kw.get("realized_20d")
                existing.spot = kw.get("spot")
                existing.source = kw.get("source", "")
                await s.commit()
                return existing.to_dict()
            row = IVHistory(
                symbol=symbol.upper(), observed_on=day, atm_iv_30d=atm_iv,
                realized_20d=kw.get("realized_20d"), spot=kw.get("spot"),
                source=kw.get("source", ""))
            s.add(row)
            await s.commit()
            return row.to_dict()

    async def iv_history(self, symbol: str, days: int = 365) -> list[dict]:
        cutoff = dt.date.today() - dt.timedelta(days=days)
        async with self._sf() as s:
            q = (select(IVHistory)
                 .where(IVHistory.symbol == symbol.upper(), IVHistory.observed_on >= cutoff)
                 .order_by(IVHistory.observed_on))
            return [r.to_dict() for r in (await s.execute(q)).scalars()]

    # -- earnings --
    async def upsert_earnings(self, rows: list[dict]) -> int:
        n = 0
        async with self._sf() as s:
            for r in rows:
                day = r["event_date"]
                if isinstance(day, str):
                    day = dt.date.fromisoformat(day[:10])
                existing = (await s.execute(select(EarningsEventRow).where(
                    EarningsEventRow.symbol == r["symbol"].upper(),
                    EarningsEventRow.event_date == day))).scalar_one_or_none()
                target = existing or EarningsEventRow(
                    symbol=r["symbol"].upper(), event_date=day)
                target.session = r.get("session", "unknown")
                target.date_status = r.get("date_status", "projected")
                target.fiscal_period = r.get("fiscal_period")
                target.fiscal_year = r.get("fiscal_year")
                target.eps_estimate = r.get("eps_estimate")
                target.eps_actual = r.get("eps_actual")
                target.revenue_estimate = r.get("revenue_estimate")
                target.revenue_actual = r.get("revenue_actual")
                target.source = r.get("source", "")
                target.is_synthetic = bool(r.get("is_synthetic"))
                target.fetched_at = dt.datetime.now(dt.timezone.utc)
                if existing is None:
                    s.add(target)
                n += 1
            await s.commit()
        return n

    async def get_earnings(self, symbol: str, since=None, until=None) -> list[dict]:
        async with self._sf() as s:
            q = select(EarningsEventRow).where(EarningsEventRow.symbol == symbol.upper())
            if since:
                q = q.where(EarningsEventRow.event_date >= since)
            if until:
                q = q.where(EarningsEventRow.event_date <= until)
            q = q.order_by(EarningsEventRow.event_date)
            out = []
            for r in (await s.execute(q)).scalars():
                out.append({
                    "symbol": r.symbol, "event_date": r.event_date.isoformat(),
                    "session": r.session, "date_status": r.date_status,
                    "fiscal_period": r.fiscal_period, "fiscal_year": r.fiscal_year,
                    "eps_estimate": r.eps_estimate, "eps_actual": r.eps_actual,
                    "revenue_estimate": r.revenue_estimate,
                    "revenue_actual": r.revenue_actual,
                    "source": r.source, "is_synthetic": r.is_synthetic,
                })
            return out

    # -- backtests --
    async def save_backtest(self, result: dict, label: str = "") -> dict:
        async with self._sf() as s:
            row = BacktestRun(
                symbol=result["symbol"], template_id=result["template"],
                label=label, rules=result["rules"], metrics=result["metrics"],
                trades=result["trades"], equity_curve=result["equity_curve"],
                start_date=result["start"], end_date=result["end"],
                quote_source=result["quote_source"],
                is_historical_pricing=result["is_historical_pricing"])
            s.add(row)
            await s.commit()
            return row.to_dict()

    async def list_backtests(self, symbol: str | None = None,
                             limit: int = 50) -> list[dict]:
        async with self._sf() as s:
            q = select(BacktestRun).order_by(BacktestRun.created_at.desc()).limit(limit)
            if symbol:
                q = q.where(BacktestRun.symbol == symbol.upper())
            return [r.summary() for r in (await s.execute(q)).scalars()]

    async def get_backtest(self, run_id: str) -> dict | None:
        async with self._sf() as s:
            row = await s.get(BacktestRun, run_id)
            return row.to_dict() if row else None

    async def delete_backtest(self, run_id: str) -> bool:
        async with self._sf() as s:
            row = await s.get(BacktestRun, run_id)
            if not row:
                return False
            await s.delete(row)
            await s.commit()
            return True


# --------------------------------------------------------------------------
# Selection
# --------------------------------------------------------------------------

_memory = MemoryStore()


def get_store() -> Any:
    db = get_database()
    if db.available and db.session_factory is not None:
        return SqlStore(db.session_factory)
    return _memory


def store_status() -> dict:
    db = get_database()
    return {
        "backend": "sql" if db.available else "memory",
        "dialect": db.dialect if db.available else None,
        "detail": db.reason,
        "persistent": db.available,
    }


def reset_memory_store() -> None:
    """Used by tests to isolate cases."""
    global _memory
    _memory = MemoryStore()
