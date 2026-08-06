"""
monetization_claims.py
------------------------
Subscription billing (Stripe), profit/reserve routing, and automated relief
for validated guardrail failures.

All account and reserve state is now persisted via database.py (SQLAlchemy),
so it survives restarts and is safe to run behind multiple server instances --
this used to be a plain Python dict that reset on every deploy.

Design choice, deliberately conservative on the legal front:
  - "Service credit" relief (free usage / subscription months) is issued fully
    automatically -- this is just a SaaS discount, not risk transfer, and needs
    no special licensing anywhere.
  - Cash relief from the pooled reserve is capped, opt-in per plan tier, and
    gated behind a manual review flag by default (`require_manual_review_for_cash`).
    Treat the numbers here as placeholders: get jurisdiction-specific legal sign-off
    before enabling large or automatic cash payouts, since at volume that can shade
    into activity regulators treat as insurance regardless of contract labeling.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from typing import Dict, Optional

import stripe
from sqlalchemy import select

from database import SessionLocal, AccountRow, ReserveStateRow, init_db

stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")


class SubscriptionTier(str, Enum):
    FREE_TRIAL = "free_trial"
    STARTER = "starter"
    PRO = "pro"
    ENTERPRISE = "enterprise"


TIER_CONFIG = {
    SubscriptionTier.FREE_TRIAL: {"price_usd": 0, "cash_relief_cap_usd": 0, "credit_relief_cap_usd": 25},
    SubscriptionTier.STARTER: {"price_usd": 29, "cash_relief_cap_usd": 0, "credit_relief_cap_usd": 100},
    SubscriptionTier.PRO: {"price_usd": 99, "cash_relief_cap_usd": 100, "credit_relief_cap_usd": 300},
    SubscriptionTier.ENTERPRISE: {"price_usd": 299, "cash_relief_cap_usd": 500, "credit_relief_cap_usd": 1000},
}

PROFIT_SPLIT_RATIO = 0.85   # to net revenue
RESERVE_SPLIT_RATIO = 0.15  # to pooled credit/cash reserve


@dataclass
class Account:
    account_id: str
    tier: SubscriptionTier
    stripe_customer_id: Optional[str]
    credit_balance_usd: float = 0.0
    reserve_contribution_usd: float = 0.0
    flagged_for_permission_downgrade: bool = False


@dataclass
class ReliefDecision:
    account_id: str
    validated_loss_usd: float
    credit_issued_usd: float
    cash_issued_usd: float
    requires_manual_review: bool
    notes: str = ""


def _row_to_account(row: AccountRow) -> Account:
    return Account(
        account_id=row.account_id, tier=SubscriptionTier(row.tier),
        stripe_customer_id=row.stripe_customer_id, credit_balance_usd=row.credit_balance_usd,
        reserve_contribution_usd=row.reserve_contribution_usd,
        flagged_for_permission_downgrade=row.flagged_for_permission_downgrade,
    )


class ClaimsEngine:
    """
    A thin, dict-like facade is kept via `self.accounts[...]`-style access
    (through `AccountsView` below) so existing call sites (main.py) don't need
    to change, but every read/write now goes to the database.
    """

    def __init__(self, require_manual_review_for_cash: bool = True):
        init_db()
        self.require_manual_review_for_cash = require_manual_review_for_cash
        self.accounts = _AccountsView()

    # ---- Account / subscription lifecycle -----------------------------------

    def create_account(self, account_id: str, tier: SubscriptionTier, stripe_customer_id: str) -> Account:
        with SessionLocal() as session:
            existing = session.get(AccountRow, account_id)
            if existing:
                return _row_to_account(existing)
            row = AccountRow(account_id=account_id, tier=tier.value, stripe_customer_id=stripe_customer_id)
            session.add(row)
            session.commit()
            session.refresh(row)
            return _row_to_account(row)

    def record_subscription_payment(self, account_id: str, amount_usd: float) -> None:
        """Call this from the Stripe webhook handler on invoice.payment_succeeded."""
        profit_share = amount_usd * PROFIT_SPLIT_RATIO
        reserve_share = amount_usd * RESERVE_SPLIT_RATIO

        with SessionLocal() as session:
            reserve = session.get(ReserveStateRow, 1)
            reserve.net_profit_usd += profit_share
            reserve.pooled_reserve_usd += reserve_share

            acct = session.get(AccountRow, account_id)
            if acct:
                acct.reserve_contribution_usd += reserve_share

            session.commit()

    # ---- Circuit breaker hook -------------------------------------------------

    def flag_agent_for_permission_downgrade(self, agent_id: str) -> None:
        """
        Called by the risk engine when an agent repeatedly triggers CRITICAL risk.
        In production this should look up which account owns `agent_id` and flip
        its permission set (e.g. "execute_payment" -> "draft_payment_only") in your
        permissions store. Left as a hook so it's wired at the account layer you use.
        """
        pass

    # ---- Claims / relief --------------------------------------------------------

    def evaluate_claim(
        self,
        account_id: str,
        validated_loss_usd: float,
        guardrail_approved_action: bool,
        user_override_present: bool,
    ) -> ReliefDecision:
        """
        Eligibility (per spec): the ledger must show the guardrail approved the
        action (i.e. AgentShield did not block it) AND there was no user-side
        override of the guardrail's decision. Both must hold for any relief.
        """
        with SessionLocal() as session:
            acct = session.get(AccountRow, account_id)
            if not acct:
                raise ValueError(f"Unknown account: {account_id}")

            if not guardrail_approved_action or user_override_present:
                return ReliefDecision(
                    account_id=account_id, validated_loss_usd=validated_loss_usd,
                    credit_issued_usd=0.0, cash_issued_usd=0.0, requires_manual_review=False,
                    notes="Not eligible: action was not a guardrail-approved pass, or was user-overridden.",
                )

            config = TIER_CONFIG[SubscriptionTier(acct.tier)]
            credit_cap = config["credit_relief_cap_usd"]
            cash_cap = config["cash_relief_cap_usd"]

            # Primary relief: service credits, up to this tier's cap, issued automatically.
            credit_issued = min(validated_loss_usd, credit_cap)
            acct.credit_balance_usd += credit_issued
            remaining_loss = max(0.0, validated_loss_usd - credit_issued)

            # Secondary relief: capped cash from the pooled reserve, only if the
            # plan allows it and the reserve can cover it.
            cash_issued = 0.0
            notes = ""
            needs_review = False
            if remaining_loss > 0 and cash_cap > 0:
                eligible_cash = min(remaining_loss, cash_cap)
                reserve = session.get(ReserveStateRow, 1)
                if self.require_manual_review_for_cash:
                    needs_review = True
                    notes = f"Cash relief of up to ${eligible_cash:.2f} pending manual review before payout."
                elif eligible_cash <= reserve.pooled_reserve_usd:
                    reserve.pooled_reserve_usd -= eligible_cash
                    cash_issued = eligible_cash
                else:
                    needs_review = True
                    notes = "Requested cash relief exceeds current pooled reserve balance; escalate manually."

            session.commit()

        return ReliefDecision(
            account_id=account_id, validated_loss_usd=validated_loss_usd,
            credit_issued_usd=credit_issued, cash_issued_usd=cash_issued,
            requires_manual_review=needs_review, notes=notes,
        )


class _AccountsView:
    """Dict-like read access to accounts, e.g. `claims_engine.accounts.get(id)`
    and `"demo" in claims_engine.accounts`, backed by the database."""

    def get(self, account_id: str) -> Optional[Account]:
        with SessionLocal() as session:
            row = session.get(AccountRow, account_id)
            return _row_to_account(row) if row else None

    def __contains__(self, account_id: str) -> bool:
        return self.get(account_id) is not None

    def __getitem__(self, account_id: str) -> Account:
        acct = self.get(account_id)
        if acct is None:
            raise KeyError(account_id)
        return acct


# ---- Stripe webhook handler (wire into your FastAPI/Flask route) ----------------

def handle_stripe_webhook(payload: bytes, sig_header: str, webhook_secret: str, claims_engine: ClaimsEngine) -> dict:
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
    except (ValueError, stripe.error.SignatureVerificationError) as e:
        return {"status": "error", "detail": str(e)}

    if event["type"] == "invoice.payment_succeeded":
        invoice = event["data"]["object"]
        account_id = invoice.get("metadata", {}).get("account_id")
        amount_usd = invoice.get("amount_paid", 0) / 100.0
        if account_id:
            claims_engine.record_subscription_payment(account_id, amount_usd)

    return {"status": "ok"}
