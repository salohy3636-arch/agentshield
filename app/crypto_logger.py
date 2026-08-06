"""
crypto_logger.py
------------------
Append-only, hash-chained audit ledger ("Proof-of-Execution").

Each entry is SHA-256 hashed together with the hash of the previous entry
(Merkle/blockchain-style chaining), so any retroactive tampering with an entry
breaks the chain and is detectable by `verify_chain()`.

Storage is delegated to database.py (SQLAlchemy), so this works unchanged
against SQLite locally or Postgres in production -- just set DATABASE_URL.
For extra tamper-resistance in production, also revoke UPDATE/DELETE grants
on the `ledger_entries` table for your app's DB role, so even a compromised
app process can only INSERT, never rewrite history.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any, Dict, List

from sqlalchemy import select, func

from database import SessionLocal, LedgerEntryRow, init_db


@dataclass
class LedgerEntry:
    index: int
    timestamp: float
    agent_id: str
    action_type: str
    risk_score: float
    decision: str  # "pass" | "hold" | "block" | "human_pass:*" | "human_block:*"
    payload_hash: str
    prev_hash: str
    entry_hash: str


class LedgerStore:
    def __init__(self):
        init_db()

    @staticmethod
    def _hash_payload(payload: Dict[str, Any]) -> str:
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _row_to_entry(row: LedgerEntryRow) -> LedgerEntry:
        return LedgerEntry(
            index=row.idx, timestamp=row.timestamp, agent_id=row.agent_id,
            action_type=row.action_type, risk_score=row.risk_score, decision=row.decision,
            payload_hash=row.payload_hash, prev_hash=row.prev_hash, entry_hash=row.entry_hash,
        )

    def append(
        self,
        agent_id: str,
        action_type: str,
        risk_score: float,
        decision: str,
        payload: Dict[str, Any],
    ) -> LedgerEntry:
        payload_hash = self._hash_payload(payload)
        timestamp = time.time()

        with SessionLocal() as session:
            last = session.execute(
                select(LedgerEntryRow).order_by(LedgerEntryRow.idx.desc()).limit(1)
            ).scalar_one_or_none()
            prev_hash = last.entry_hash if last else "0" * 64
            next_index = (last.idx + 1) if last else 0

            entry_material = (
                f"{next_index}|{timestamp}|{agent_id}|{action_type}|{risk_score}|"
                f"{decision}|{payload_hash}|{prev_hash}"
            )
            entry_hash = hashlib.sha256(entry_material.encode("utf-8")).hexdigest()

            row = LedgerEntryRow(
                timestamp=timestamp, agent_id=agent_id, action_type=action_type,
                risk_score=risk_score, decision=decision, payload_hash=payload_hash,
                prev_hash=prev_hash, entry_hash=entry_hash,
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return self._row_to_entry(row)

    def all_entries(self) -> List[LedgerEntry]:
        with SessionLocal() as session:
            rows = session.execute(select(LedgerEntryRow).order_by(LedgerEntryRow.idx.asc())).scalars().all()
            return [self._row_to_entry(r) for r in rows]

    def recent_entries(self, limit: int = 50) -> List[LedgerEntry]:
        with SessionLocal() as session:
            rows = session.execute(
                select(LedgerEntryRow).order_by(LedgerEntryRow.idx.desc()).limit(limit)
            ).scalars().all()
            return [self._row_to_entry(r) for r in rows]

    def count(self) -> int:
        with SessionLocal() as session:
            return session.execute(select(func.count()).select_from(LedgerEntryRow)).scalar_one()

    def verify_chain(self) -> Dict[str, Any]:
        """Recomputes every hash and confirms the chain hasn't been tampered with."""
        entries = self.all_entries()
        expected_prev = "0" * 64
        for e in entries:
            if e.prev_hash != expected_prev:
                return {"valid": False, "broken_at_index": e.index, "reason": "prev_hash mismatch"}
            material = (
                f"{e.index}|{e.timestamp}|{e.agent_id}|{e.action_type}|{e.risk_score}|"
                f"{e.decision}|{e.payload_hash}|{e.prev_hash}"
            )
            recomputed = hashlib.sha256(material.encode("utf-8")).hexdigest()
            if recomputed != e.entry_hash:
                return {"valid": False, "broken_at_index": e.index, "reason": "entry_hash mismatch"}
            expected_prev = e.entry_hash
        return {"valid": True, "entries_checked": len(entries)}
