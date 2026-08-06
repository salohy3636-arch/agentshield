"""
main.py
---------
AgentShield middleware API.

POST /v1/guard  -> the core endpoint an AI agent calls before executing an action.
GET  /v1/ledger/verify -> tamper-check the audit ledger.
POST /v1/approvals/{token}/decide -> human approver resolves a MEDIUM-risk hold.

Guardrails AI / NeMo Guardrails integration point is marked below — wire in your
actual rail configs where indicated; this file focuses on the orchestration logic
that's specific to AgentShield (scoring, routing, ledger, HITL).
"""

from __future__ import annotations

import os
import random
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
import stripe
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from database import SessionLocal, PendingApprovalRow, init_db
from risk_engine import AgentAction, RiskEngine, RiskTier
from crypto_logger import LedgerStore
from monetization_claims import (
    ClaimsEngine,
    SubscriptionTier,
    TIER_CONFIG,
    Account,
    handle_stripe_webhook,
)
import json

app = FastAPI(title="AgentShield AI Middleware", version="0.1.0")

# Allow the dashboard/marketing site to call the API from any origin by default.
# Lock this down to your real domain(s) in production via AGENTSHIELD_ALLOWED_ORIGINS
# (comma-separated), e.g. "https://app.agentshield.ai,https://agentshield.ai".
_allowed_origins = os.environ.get("AGENTSHIELD_ALLOWED_ORIGINS", "*")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if _allowed_origins == "*" else _allowed_origins.split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

risk_engine = RiskEngine()
ledger = LedgerStore()
claims_engine = ClaimsEngine()

SLACK_WEBHOOK_URL = os.environ.get("AGENTSHIELD_SLACK_WEBHOOK_URL", "")
WHATSAPP_WEBHOOK_URL = os.environ.get("AGENTSHIELD_WHATSAPP_WEBHOOK_URL", "")
DEMO_MODE = os.environ.get("AGENTSHIELD_DEMO_MODE", "true").lower() == "true"

stripe.api_key = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")

# Stripe Price IDs per tier/cycle -- create these in your Stripe Dashboard and
# set them as env vars before going live. The API returns a clear error if a
# price ID is missing rather than silently failing.
PRICE_IDS = {
    ("starter", "monthly"): os.environ.get("STRIPE_PRICE_STARTER_MONTHLY", ""),
    ("starter", "yearly"): os.environ.get("STRIPE_PRICE_STARTER_YEARLY", ""),
    ("pro", "monthly"): os.environ.get("STRIPE_PRICE_PRO_MONTHLY", ""),
    ("pro", "yearly"): os.environ.get("STRIPE_PRICE_PRO_YEARLY", ""),
    ("enterprise", "monthly"): os.environ.get("STRIPE_PRICE_ENTERPRISE_MONTHLY", ""),
    ("enterprise", "yearly"): os.environ.get("STRIPE_PRICE_ENTERPRISE_YEARLY", ""),
}

PUBLIC_BASE_URL = os.environ.get("AGENTSHIELD_PUBLIC_URL", "http://localhost:8000")
WEB_DIR = Path(__file__).resolve().parent.parent / "web"

# Pending human-approval holds now live in the `pending_approvals` table
# (see database.py) instead of an in-memory dict, so they survive restarts
# and work correctly if you ever run more than one server instance.
init_db()


def _save_pending_approval(token: str, agent_id: str, action_type: str, payload: Dict[str, Any], risk_score: float) -> None:
    with SessionLocal() as session:
        session.add(PendingApprovalRow(
            token=token, agent_id=agent_id, action_type=action_type,
            payload_json=json.dumps(payload), risk_score=risk_score, created_at=time.time(),
        ))
        session.commit()


def _pop_pending_approval(token: str) -> Optional[Dict[str, Any]]:
    with SessionLocal() as session:
        row = session.get(PendingApprovalRow, token)
        if not row:
            return None
        data = {
            "agent_id": row.agent_id, "action_type": row.action_type,
            "payload": json.loads(row.payload_json), "risk_score": row.risk_score,
        }
        session.delete(row)
        session.commit()
        return data


def _list_pending_approvals() -> List[Dict[str, Any]]:
    with SessionLocal() as session:
        rows = session.execute(select(PendingApprovalRow).order_by(PendingApprovalRow.created_at.asc())).scalars().all()
        return [
            {
                "token": r.token, "agent_id": r.agent_id, "action_type": r.action_type,
                "risk_score": r.risk_score, "created_at": r.created_at,
            }
            for r in rows
        ]


def _count_pending_approvals() -> int:
    with SessionLocal() as session:
        return session.query(PendingApprovalRow).count()


# Seed a demo account so the dashboard has something to show on first run.
if "demo" not in claims_engine.accounts:
    claims_engine.create_account("demo", SubscriptionTier.PRO, stripe_customer_id="demo_customer")


class GuardRequest(BaseModel):
    agent_id: str
    action_type: str
    payload: Dict[str, Any] = Field(default_factory=dict)
    financial_value: float = 0.0


class GuardResponse(BaseModel):
    decision: str  # "pass" | "hold" | "block"
    risk_score: float
    risk_tier: str
    reasons: list[str]
    ledger_entry_hash: str
    approval_token: Optional[str] = None


class ApprovalDecision(BaseModel):
    approved: bool
    approver: str


def _run_guardrails_checks(payload: Dict[str, Any]) -> Optional[str]:
    """
    Integration point for Guardrails AI / NeMo Guardrails.

    Example (Guardrails AI):
        from guardrails import Guard
        guard = Guard.from_rail("rails/agent_action.rail")
        validated, error = guard.parse(llm_output=json.dumps(payload))
        if error:
            return str(error)

    Example (NeMo Guardrails):
        from nemoguardrails import LLMRails, RailsConfig
        config = RailsConfig.from_path("./rails_config")
        rails = LLMRails(config)
        result = rails.generate(messages=[{"role": "user", "content": json.dumps(payload)}])
        if result.get("blocked"):
            return "nemo_guardrails_block"

    Returns an error string if the payload fails schema/policy validation, else None.
    """
    if not isinstance(payload, dict):
        return "payload_not_object"
    return None


async def _notify_human(agent_id: str, action_type: str, risk_score: float, token: str) -> None:
    message = (
        f":warning: AgentShield HOLD — agent `{agent_id}` wants to run "
        f"`{action_type}` (risk {risk_score}). Approve/deny via token `{token}`."
    )
    async with httpx.AsyncClient(timeout=5.0) as client:
        if SLACK_WEBHOOK_URL:
            try:
                await client.post(SLACK_WEBHOOK_URL, json={"text": message})
            except httpx.HTTPError:
                pass  # notification failure shouldn't crash the guard flow
        if WHATSAPP_WEBHOOK_URL:
            try:
                await client.post(WHATSAPP_WEBHOOK_URL, json={"message": message})
            except httpx.HTTPError:
                pass


@app.post("/v1/guard", response_model=GuardResponse)
async def guard_action(req: GuardRequest):
    guardrails_error = _run_guardrails_checks(req.payload)
    if guardrails_error:
        entry = ledger.append(
            agent_id=req.agent_id, action_type=req.action_type,
            risk_score=100.0, decision="block", payload=req.payload,
        )
        return GuardResponse(
            decision="block", risk_score=100.0, risk_tier=RiskTier.CRITICAL.value,
            reasons=[f"guardrails_validation_failed: {guardrails_error}"],
            ledger_entry_hash=entry.entry_hash,
        )

    action = AgentAction(
        agent_id=req.agent_id, action_type=req.action_type,
        payload=req.payload, financial_value=req.financial_value,
    )
    result = risk_engine.score(action)

    if result.tier == RiskTier.SAFE:
        decision = "pass"
        approval_token = None
    elif result.tier == RiskTier.MEDIUM:
        decision = "hold"
        approval_token = str(uuid.uuid4())
        _save_pending_approval(approval_token, req.agent_id, req.action_type, req.payload, result.score)
        await _notify_human(req.agent_id, req.action_type, result.score, approval_token)
    else:
        decision = "block"
        approval_token = None
        # Self-healing circuit breaker: if this agent is repeatedly critical-risk,
        # downgrade its permitted action set (e.g. execute -> draft-only).
        claims_engine.flag_agent_for_permission_downgrade(req.agent_id)

    entry = ledger.append(
        agent_id=req.agent_id, action_type=req.action_type,
        risk_score=result.score, decision=decision, payload=req.payload,
    )

    return GuardResponse(
        decision=decision, risk_score=result.score, risk_tier=result.tier.value,
        reasons=result.reasons, ledger_entry_hash=entry.entry_hash,
        approval_token=approval_token,
    )


@app.post("/v1/approvals/{token}/decide")
async def decide_approval(token: str, decision: ApprovalDecision):
    pending = _pop_pending_approval(token)
    if not pending:
        raise HTTPException(status_code=404, detail="Approval token not found or already resolved")

    final_decision = "pass" if decision.approved else "block"
    entry = ledger.append(
        agent_id=pending["agent_id"], action_type=pending["action_type"],
        risk_score=pending["risk_score"], decision=f"human_{final_decision}:{decision.approver}",
        payload=pending["payload"],
    )
    return {"resolved": final_decision, "ledger_entry_hash": entry.entry_hash}


@app.get("/v1/ledger/verify")
async def verify_ledger():
    return ledger.verify_chain()


@app.get("/v1/ledger/recent")
async def recent_ledger(limit: int = 50):
    """Powers the dashboard's live decision feed."""
    entries = ledger.recent_entries(limit)
    return {
        "entries": [
            {
                "index": e.index,
                "timestamp": e.timestamp,
                "agent_id": e.agent_id,
                "action_type": e.action_type,
                "risk_score": e.risk_score,
                "decision": e.decision,
                "entry_hash": e.entry_hash,
            }
            for e in entries
        ]
    }


@app.get("/v1/approvals/pending")
async def list_pending_approvals():
    return {"pending": _list_pending_approvals()}


@app.get("/v1/stats")
async def stats():
    """Aggregate counters the dashboard header cards use."""
    entries = ledger.all_entries()
    total = len(entries)
    passed = sum(1 for e in entries if e.decision == "pass")
    held = sum(1 for e in entries if e.decision == "hold")
    blocked = sum(1 for e in entries if e.decision == "block")
    avg_score = round(sum(e.risk_score for e in entries) / total, 1) if total else 0.0
    return {
        "total_actions": total,
        "passed": passed,
        "held": held,
        "blocked": blocked,
        "pending_approvals": _count_pending_approvals(),
        "avg_risk_score": avg_score,
        "agents_seen": len({e.agent_id for e in entries}),
    }


@app.get("/v1/accounts/{account_id}")
async def get_account(account_id: str):
    acct = claims_engine.accounts.get(account_id)
    if not acct:
        raise HTTPException(status_code=404, detail="Account not found")
    config = TIER_CONFIG[acct.tier]
    return {
        "account_id": acct.account_id,
        "tier": acct.tier.value,
        "credit_balance_usd": acct.credit_balance_usd,
        "credit_relief_cap_usd": config["credit_relief_cap_usd"],
        "cash_relief_cap_usd": config["cash_relief_cap_usd"],
    }


class CheckoutRequest(BaseModel):
    tier: str  # "starter" | "pro" | "enterprise"
    cycle: str  # "monthly" | "yearly"
    account_id: str = "demo"
    customer_email: Optional[str] = None


@app.post("/v1/billing/checkout-session")
async def create_checkout_session(req: CheckoutRequest):
    """Creates a Stripe Checkout session for a monthly or yearly subscription
    and returns the URL to redirect the customer to. Wire the pricing page's
    'Subscribe' buttons to call this endpoint."""
    key = (req.tier.lower(), req.cycle.lower())
    price_id = PRICE_IDS.get(key)
    if not price_id:
        raise HTTPException(
            status_code=400,
            detail=(
                f"No Stripe price configured for tier='{req.tier}' cycle='{req.cycle}'. "
                f"Set the matching STRIPE_PRICE_* environment variable."
            ),
        )
    if not stripe.api_key:
        raise HTTPException(status_code=500, detail="STRIPE_SECRET_KEY is not configured on the server.")

    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            line_items=[{"price": price_id, "quantity": 1}],
            success_url=f"{PUBLIC_BASE_URL}/dashboard.html?checkout=success",
            cancel_url=f"{PUBLIC_BASE_URL}/index.html?checkout=cancelled",
            customer_email=req.customer_email,
            metadata={"account_id": req.account_id, "tier": req.tier, "cycle": req.cycle},
        )
    except stripe.error.StripeError as e:
        raise HTTPException(status_code=502, detail=f"Stripe error: {e.user_message or str(e)}")

    return {"checkout_url": session.url}


@app.post("/v1/webhooks/stripe")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")
    return handle_stripe_webhook(payload, sig_header, STRIPE_WEBHOOK_SECRET, claims_engine)


@app.post("/v1/demo/seed")
async def seed_demo_data():
    """Populates the ledger with realistic sample traffic so the dashboard is
    populated on first run. Disabled automatically when AGENTSHIELD_DEMO_MODE=false."""
    if not DEMO_MODE:
        raise HTTPException(status_code=403, detail="Demo mode is disabled on this server.")

    agents = ["billing-bot-01", "support-agent-eu", "trading-bot-alpha", "workflow-agent-03"]
    action_types = ["issue_refund", "send_email", "update_ticket", "execute_trade", "modify_billing"]

    for _ in range(25):
        agent_id = random.choice(agents)
        action_type = random.choice(action_types)
        financial_value = round(random.choice([0, 0, 50, 250, 1200, 8000]) * random.uniform(0.8, 1.3), 2)
        req = GuardRequest(agent_id=agent_id, action_type=action_type, payload={"note": "demo"}, financial_value=financial_value)
        await guard_action(req)

    return {"status": "seeded"}


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


# --- Serve the marketing site + dashboard as static files -----------------------
# In production you can instead deploy /web on a CDN (Vercel/Netlify/S3+CloudFront)
# and point it at this API's URL. Mounting it here keeps a single-service deploy
# (e.g. one Render/Fly/Railway app) simple for launch.
if WEB_DIR.exists():
    app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
