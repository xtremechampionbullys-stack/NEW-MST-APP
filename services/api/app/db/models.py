"""
SQLAlchemy models.

These mirror infra/schema.sql, with one deliberate difference: no Timescale
hypertable calls here. Hypertables are a Postgres-side concern applied by
`engine.py` after table creation, and only when the extension is present, so
the same models run unchanged against SQLite for local development and tests.

What is stored and what is not
------------------------------
Positions and saved strategies store their *legs*. Analytics -- POP, greeks,
max loss -- are a pure function of legs plus a market snapshot, so persisting
them would create a second source of truth that silently goes stale. They are
recomputed on read.

`position_marks` is the exception, and for a good reason: a mark is a
*measurement taken at a moment*, not a derivation. Reconstructing an equity
curve after the fact means guessing what implied volatility was on each day,
which is exactly the kind of retroactive assumption that makes backtests lie.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _uuid() -> str:
    return uuid.uuid4().hex


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class SavedStrategy(Base):
    __tablename__ = "saved_strategies"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    user_id: Mapped[str | None] = mapped_column(String(64), index=True, default=None)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    template_id: Mapped[str | None] = mapped_column(String(64), default=None)
    name: Mapped[str] = mapped_column(String(160))
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    legs: Mapped[list] = mapped_column(JSON, default=list)
    entry_spot: Mapped[float | None] = mapped_column(Float, default=None)
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "symbol": self.symbol, "template_id": self.template_id,
            "name": self.name, "params": self.params, "legs": self.legs,
            "entry_spot": self.entry_spot, "notes": self.notes,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class Position(Base):
    __tablename__ = "positions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    user_id: Mapped[str | None] = mapped_column(String(64), index=True, default=None)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    name: Mapped[str] = mapped_column(String(160))
    legs: Mapped[list] = mapped_column(JSON, default=list)
    opened_at: Mapped[dt.date] = mapped_column(Date, default=lambda: dt.date.today())
    closed_at: Mapped[dt.date | None] = mapped_column(Date, default=None)
    entry_debit: Mapped[float | None] = mapped_column(Float, default=None)
    exit_credit: Mapped[float | None] = mapped_column(Float, default=None)
    broker: Mapped[str | None] = mapped_column(String(32), default=None)
    broker_order_id: Mapped[str | None] = mapped_column(String(64), default=None)
    notes: Mapped[str] = mapped_column(Text, default="")

    # What the engine predicted at entry, frozen. Stored as its own column
    # rather than folded into `notes` because it is the input to the
    # calibration report, and a prediction that can be edited alongside free
    # text is a prediction that will eventually be edited.
    expectation: Mapped[dict | None] = mapped_column(JSON, default=None)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "symbol": self.symbol, "name": self.name,
            "legs": self.legs,
            "opened": self.opened_at.isoformat() if self.opened_at else None,
            "closed": self.closed_at.isoformat() if self.closed_at else None,
            "entry_debit": self.entry_debit, "exit_credit": self.exit_credit,
            "broker": self.broker, "notes": self.notes,
            "expectation": self.expectation,
        }


class PositionMark(Base):
    __tablename__ = "position_marks"

    position_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("positions.id", ondelete="CASCADE"), primary_key=True)
    ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), primary_key=True,
                                            default=_now)
    underlying: Mapped[float | None] = mapped_column(Float, default=None)
    mark_value: Mapped[float | None] = mapped_column(Float, default=None)
    unrealized: Mapped[float | None] = mapped_column(Float, default=None)
    delta: Mapped[float | None] = mapped_column(Float, default=None)
    gamma: Mapped[float | None] = mapped_column(Float, default=None)
    theta: Mapped[float | None] = mapped_column(Float, default=None)
    vega: Mapped[float | None] = mapped_column(Float, default=None)
    # True on the final mark of a position: the point where the equity curve
    # stops. The close route writes it; without it a closed position's series
    # has no record of where it ended.
    closing: Mapped[bool | None] = mapped_column(Boolean, default=None)

    def to_dict(self) -> dict:
        return {
            "ts": self.ts.isoformat() if self.ts else None,
            "underlying": self.underlying, "mark_value": self.mark_value,
            "unrealized": self.unrealized, "delta": self.delta,
            "gamma": self.gamma, "theta": self.theta, "vega": self.vega,
            "closing": self.closing,
        }


class IVHistory(Base):
    """
    One row per symbol per day.

    This table is what makes IV rank meaningful. Rank is defined against a
    trailing window of the name's *own* implied volatility -- typically 52
    weeks -- and without stored history the best you can do is a cross-sectional
    estimate from whatever the chain quotes today, which measures something
    quite different and is systematically too narrow.
    """

    __tablename__ = "iv_history"
    __table_args__ = (
        UniqueConstraint("symbol", "observed_on", name="uq_iv_symbol_day"),
        Index("ix_iv_symbol_date", "symbol", "observed_on"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(16))
    observed_on: Mapped[dt.date] = mapped_column(Date)
    ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    atm_iv_30d: Mapped[float] = mapped_column(Float)
    realized_20d: Mapped[float | None] = mapped_column(Float, default=None)
    spot: Mapped[float | None] = mapped_column(Float, default=None)
    source: Mapped[str] = mapped_column(String(32), default="")

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "date": self.observed_on.isoformat(),
            "atm_iv_30d": self.atm_iv_30d,
            "realized_20d": self.realized_20d,
            "spot": self.spot,
            "source": self.source,
        }


class BacktestRun(Base):
    """
    A completed backtest.

    The rules, metrics, trades and equity curve are stored as JSON rather than
    normalized into tables. They are written once and read whole -- there is no
    query that wants "all trades across all runs where..." that a report would
    not answer better. Normalizing here would buy nothing and cost a join.

    `quote_source` is stored because it is the single most important thing about
    a result: a modelled run and a historical run are not comparable, and six
    months later nobody remembers which was which.
    """

    __tablename__ = "backtest_runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    user_id: Mapped[str | None] = mapped_column(String(64), index=True, default=None)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    template_id: Mapped[str] = mapped_column(String(64))
    label: Mapped[str] = mapped_column(String(160), default="")
    rules: Mapped[dict] = mapped_column(JSON, default=dict)
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    trades: Mapped[list] = mapped_column(JSON, default=list)
    equity_curve: Mapped[list] = mapped_column(JSON, default=list)
    start_date: Mapped[str] = mapped_column(String(16), default="")
    end_date: Mapped[str] = mapped_column(String(16), default="")
    quote_source: Mapped[str] = mapped_column(String(32), default="modelled")
    is_historical_pricing: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)

    def summary(self) -> dict:
        return {
            "id": self.id, "symbol": self.symbol, "template_id": self.template_id,
            "label": self.label, "rules": self.rules, "metrics": self.metrics,
            "start": self.start_date, "end": self.end_date,
            "quote_source": self.quote_source,
            "is_historical_pricing": self.is_historical_pricing,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    def to_dict(self) -> dict:
        return {**self.summary(), "trades": self.trades,
                "equity_curve": self.equity_curve}


class EarningsEventRow(Base):
    """
    Cached earnings dates.

    Cached rather than fetched live because the good sources are metered
    (Benzinga is a paid add-on, Webull's fundamentals endpoints are 60/min) and
    an earnings date changes at most a few times a quarter. `date_status`
    carries whether the date is confirmed by the company or merely projected --
    a projected date is a materially weaker basis for sizing a trade, so the
    distinction is preserved rather than flattened.
    """

    __tablename__ = "earnings_events"
    __table_args__ = (
        UniqueConstraint("symbol", "event_date", name="uq_earnings_symbol_date"),
        Index("ix_earnings_date", "event_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    symbol: Mapped[str] = mapped_column(String(16), index=True)
    event_date: Mapped[dt.date] = mapped_column(Date)
    session: Mapped[str] = mapped_column(String(16), default="unknown")  # bmo/amc/dmh
    date_status: Mapped[str] = mapped_column(String(16), default="projected")
    fiscal_period: Mapped[str | None] = mapped_column(String(8), default=None)
    fiscal_year: Mapped[int | None] = mapped_column(Integer, default=None)
    eps_estimate: Mapped[float | None] = mapped_column(Float, default=None)
    eps_actual: Mapped[float | None] = mapped_column(Float, default=None)
    revenue_estimate: Mapped[float | None] = mapped_column(Float, default=None)
    revenue_actual: Mapped[float | None] = mapped_column(Float, default=None)
    source: Mapped[str] = mapped_column(String(32), default="")
    fetched_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_now)
    is_synthetic: Mapped[bool] = mapped_column(Boolean, default=False)
