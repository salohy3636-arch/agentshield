"""
database.py
-------------
Single source of persistent state for AgentShield: the audit ledger, accounts,
the pooled reserve/profit counters, and pending human-approval holds.

Everything used to live in-memory (Python dicts) or in a hand-rolled sqlite3
connection. That's fine for a demo but breaks on every restart and can't be
shared across more than one server process. This module replaces all of that
with SQLAlchemy models backed by a real database.

Configuration:
  DATABASE_URL env var controls the backend.
    - Not set (local dev, zero config):
        sqlite:///./agentshield.db  (a real file, persists across restarts)
    - Production (recommended):
        postgresql+psycopg2://user:password@host:5432/agentshield
      Any managed Postgres works (Render, Railway, Neon, RDS, Supabase, ...).

No separate Redis is required for correctness: Postgres alone gives you
durable, multi-instance-safe storage for the ledger, accounts, and pending
approvals. Add Redis later only if you need pub/sub push updates or
sub-millisecond caching at higher scale -- not needed to go live.
"""

from __future__ import annotations

import os
import time
from typing import Optional

from sqlalchemy import (
    create_engine, Column, Integer, String, Float, Boolean, Text, select
)
from sqlalchemy.orm import declarative_base, sessionmaker, Session

DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./agentshield.db")

# SQLite needs this flag for multi-threaded FastAPI access; Postgres ignores it.
_connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(DATABASE_URL, connect_args=_connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)

Base = declarative_base()


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class LedgerEntryRow(Base):
    __tablename__ = "ledger_entries"

    idx = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(Float, nullable=False)
    agent_id = Column(String(128), nullable=False, index=True)
    action_type = Column(String(128), nullable=False)
    risk_score = Column(Float, nullable=False)
    decision = Column(String(64), nullable=False)
    payload_hash = Column(String(64), nullable=False)
    prev_hash = Column(String(64), nullable=False)
    entry_hash = Column(String(64), nullable=False)


class AccountRow(Base):
    __tablename__ = "accounts"

    account_id = Column(String(128), primary_key=True)
    tier = Column(String(32), nullable=False)
    stripe_customer_id = Column(String(128), nullable=True)
    credit_balance_usd = Column(Float, nullable=False, default=0.0)
    reserve_contribution_usd = Column(Float, nullable=False, default=0.0)
    flagged_for_permission_downgrade = Column(Boolean, nullable=False, default=False)


class ReserveStateRow(Base):
    """Singleton row (id=1) holding the pooled reserve and net profit totals."""
    __tablename__ = "reserve_state"

    id = Column(Integer, primary_key=True, default=1)
    pooled_reserve_usd = Column(Float, nullable=False, default=0.0)
    net_profit_usd = Column(Float, nullable=False, default=0.0)


class PendingApprovalRow(Base):
    __tablename__ = "pending_approvals"

    token = Column(String(64), primary_key=True)
    agent_id = Column(String(128), nullable=False)
    action_type = Column(String(128), nullable=False)
    payload_json = Column(Text, nullable=False)  # JSON-encoded
    risk_score = Column(Float, nullable=False)
    created_at = Column(Float, nullable=False, default=time.time)


def init_db() -> None:
    """Create all tables if they don't exist, and seed the singleton reserve row."""
    Base.metadata.create_all(bind=engine)
    with SessionLocal() as session:
        if session.get(ReserveStateRow, 1) is None:
            session.add(ReserveStateRow(id=1, pooled_reserve_usd=0.0, net_profit_usd=0.0))
            session.commit()


def get_session() -> Session:
    """FastAPI dependency-style session getter. Callers are responsible for closing."""
    return SessionLocal()
