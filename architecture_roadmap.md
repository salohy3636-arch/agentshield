# AgentShield AI — Architecture & Roadmap

## 1. What this system actually is (and isn't)

AgentShield is a **runtime guardrail proxy** for autonomous AI agents: it inspects
outbound actions (API calls, payments, DB writes) before they execute, scores them
for risk, and blocks/escalates/logs accordingly. That's a real, valuable, and legally
uncomplicated product category (it sits next to tools like NeMo Guardrails, Guardrails AI,
Lakera, and various LLM firewalls).

The **compensation/claims engine** described in the spec — automatically paying out
cash or credits when a guarded action still causes financial loss — is where the
product starts to resemble a risk-transfer / indemnity product, which is what
insurance regulators care about. This doc treats those as two separable pieces:

- **Core product (build freely):** risk scoring, guardrail middleware, crypto audit
  ledger, behavioral fingerprinting, circuit breaker. No regulatory ambiguity.
- **Compensation layer (build carefully):** service credits are fine (you're just
  discounting your own SaaS). Cash payouts tied to "loss caused by a failure of your
  service" is legally an indemnity/warranty claim, and at meaningful scale can trigger
  insurance-like scrutiny **regardless of what the contract calls itself** — this
  depends on jurisdiction, payout size, and whether it's marketed as risk transfer.
  Get real legal review before scaling cash payouts; this doc gives you a
  conservative default (credits-first, small capped cash ceiling, opt-in) rather than
  a legal opinion.

## 2. System Diagram

```
┌─────────────────┐      ┌──────────────────────────────────────────┐      ┌────────────────────┐
│  Client AI Agent │─────▶│           AgentShield Middleware           │─────▶│  Destination:       │
│ (support bot,    │      │                                            │      │  Payment gateway,   │
│  trading bot,    │◀─────│  1. Payload ingestion (FastAPI)            │◀─────│  DB, internal API,  │
│  workflow agent) │      │  2. Guardrails AI / NeMo schema+policy     │      │  3rd-party service  │
└─────────────────┘      │     validation                             │      └────────────────────┘
                          │  3. Risk Scoring Engine (0-100)            │
                          │  4. Decision router:                       │
                          │       <40  -> auto-pass                    │
                          │       40-70-> human-in-the-loop (Slack/WA) │
                          │       >70  -> auto-block + incident log    │
                          │  5. Crypto proof-of-execution ledger       │
                          │  6. Behavioral fingerprint model (per-agent)│
                          │  7. Self-healing circuit breaker           │
                          └──────────────────────────────────────────┘
                                          │
                                          ▼
                          ┌──────────────────────────────────────────┐
                          │  Monetization / Claims service            │
                          │  - Stripe subscriptions (tiers)            │
                          │  - Profit / reserve split                  │
                          │  - Credit-relief automation (primary)       │
                          │  - Capped cash relief (secondary, gated)    │
                          └──────────────────────────────────────────┘
```

## 3. Data flow (per request)

1. Agent POSTs an intended action (`POST /v1/guard`) with a JSON payload describing
   the action type, parameters, and financial value if applicable.
2. `Guardrails AI` / `NeMo Guardrails` validate structure + apply policy rules
   (schema conformance, PII leakage, disallowed actions).
3. `risk_engine.py` computes a composite score from three vectors: financial
   exposure, semantic/behavioral anomaly, and rate/frequency anomaly.
4. Router applies the score thresholds and either passes the action through,
   parks it for human approval (webhook to Slack/WhatsApp), or blocks it.
5. Every decision is hashed and appended to an immutable ledger
   (`crypto_logger.py`) — this is your audit trail and the evidentiary basis for
   any later claim.
6. If a client is on a paid tier and a *validated, guardrail-approved* action still
   causes loss, `monetization_claims.py` computes relief per the SLA.

## 4. Scale plan

- **MVP (this deliverable):** single-region FastAPI service, SQLite/Postgres for
  ledger + subscriptions, synchronous Slack/WhatsApp webhook for HITL.
- **V1:** move ledger to an append-only store (e.g., Postgres with a Merkle-chained
  hash column, or a real ledger DB) for tamper-evidence; queue-based HITL
  (Redis/SQS) so scoring isn't blocked on human response; per-agent behavioral
  baseline model retrained nightly.
- **V2:** multi-tenant isolation, per-agent adaptive thresholds, anomaly model
  upgraded from statistical baseline to a lightweight online learner, SOC 2 prep.
- **Fundability notes:** the defensible IP is the behavioral fingerprinting +
  circuit breaker (this is genuinely closer to novel), not the claims engine (many
  competitors will avoid cash payouts for the regulatory reasons above) — so lead
  with the guardrail/audit story to investors and treat compensation as a
  differentiator you roll out cautiously, market-by-market, with counsel.

## 5. Feature expansion roadmap

| Phase | Feature |
|---|---|
| MVP | Risk scoring, HITL webhook, crypto ledger, Stripe subscriptions, service credits |
| V1 | Behavioral fingerprinting per agent, circuit breaker auto-throttle |
| V2 | Multi-agent fleet dashboards, anomaly explainability report per block |
| V3 | Insurance-partner integration (white-label underwriting via a licensed carrier, instead of self-underwriting) for clients who want real cash-indemnity coverage |
