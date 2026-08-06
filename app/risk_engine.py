"""
risk_engine.py
----------------
Dynamic multi-vector risk scoring for inbound AI-agent actions.

Score range: 0-100
  - < 40            : SAFE       -> auto-pass
  - 40 <= x <= 70    : MEDIUM     -> human-in-the-loop approval
  - > 70            : CRITICAL   -> auto-block

The three vectors are intentionally simple, transparent, and tunable so you can
justify each score component in an audit (important for the claims/ledger story).
Swap the anomaly detector for a real model once you have production traffic.
"""

from __future__ import annotations

import math
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Deque, Dict, Optional


class RiskTier(str, Enum):
    SAFE = "safe"
    MEDIUM = "medium"
    CRITICAL = "critical"


@dataclass
class AgentAction:
    agent_id: str
    action_type: str
    payload: Dict[str, Any]
    financial_value: float = 0.0
    timestamp: float = field(default_factory=time.time)


@dataclass
class RiskResult:
    score: float
    tier: RiskTier
    components: Dict[str, float]
    reasons: list[str]


class BehavioralBaseline:
    """
    Tracks a per-agent rolling baseline of action types and financial values so we
    can flag sub-second deviations from an agent's normal behavior
    ("Predictive Behavioral Fingerprinting").

    This is a statistical baseline (mean/stddev over a sliding window), not a
    trained ML model — it's a reasonable, explainable MVP. Swap in a real online
    anomaly detector (e.g. river, or a small autoencoder) once you have volume.
    """

    def __init__(self, window_size: int = 200):
        self.window_size = window_size
        self._values: Dict[str, Deque[float]] = defaultdict(
            lambda: deque(maxlen=window_size)
        )
        self._action_counts: Dict[str, Dict[str, int]] = defaultdict(
            lambda: defaultdict(int)
        )
        self._timestamps: Dict[str, Deque[float]] = defaultdict(
            lambda: deque(maxlen=window_size)
        )

    def record_and_score(self, action: AgentAction) -> float:
        """Returns an anomaly score 0-100 for this action given agent history."""
        values = self._values[action.agent_id]
        timestamps = self._timestamps[action.agent_id]
        counts = self._action_counts[action.agent_id]

        anomaly = 0.0

        # Financial value deviation from this agent's own historical norm.
        if len(values) >= 5:
            mean = sum(values) / len(values)
            variance = sum((v - mean) ** 2 for v in values) / len(values)
            stddev = math.sqrt(variance) or 1.0
            z = abs(action.financial_value - mean) / stddev
            anomaly += min(z * 15, 60)  # cap this component's contribution

        # Novel action type for this agent.
        if counts and action.action_type not in counts:
            anomaly += 25

        # Burst detection: many actions in a very short window.
        now = action.timestamp
        recent = [t for t in timestamps if now - t < 2.0]
        if len(recent) >= 5:
            anomaly += 20

        # Update state after scoring.
        values.append(action.financial_value)
        timestamps.append(now)
        counts[action.action_type] += 1

        return min(anomaly, 100.0)


class RiskEngine:
    def __init__(
        self,
        financial_ceiling: float = 10_000.0,
        rate_window_seconds: float = 60.0,
        rate_limit_per_window: int = 30,
        weight_financial: float = 0.4,
        weight_behavioral: float = 0.4,
        weight_rate: float = 0.2,
    ):
        self.financial_ceiling = financial_ceiling
        self.rate_window_seconds = rate_window_seconds
        self.rate_limit_per_window = rate_limit_per_window
        self.weights = {
            "financial": weight_financial,
            "behavioral": weight_behavioral,
            "rate": weight_rate,
        }
        self.baseline = BehavioralBaseline()
        self._rate_log: Dict[str, Deque[float]] = defaultdict(deque)

    def _financial_score(self, action: AgentAction) -> float:
        if action.financial_value <= 0:
            return 0.0
        ratio = action.financial_value / self.financial_ceiling
        return min(ratio * 100, 100.0)

    def _rate_score(self, action: AgentAction) -> float:
        log = self._rate_log[action.agent_id]
        now = action.timestamp
        log.append(now)
        while log and now - log[0] > self.rate_window_seconds:
            log.popleft()
        overage = max(0, len(log) - self.rate_limit_per_window)
        return min(overage * 10, 100.0)

    def score(self, action: AgentAction) -> RiskResult:
        financial = self._financial_score(action)
        behavioral = self.baseline.record_and_score(action)
        rate = self._rate_score(action)

        composite = (
            financial * self.weights["financial"]
            + behavioral * self.weights["behavioral"]
            + rate * self.weights["rate"]
        )
        composite = round(min(composite, 100.0), 2)

        if composite < 40:
            tier = RiskTier.SAFE
        elif composite <= 70:
            tier = RiskTier.MEDIUM
        else:
            tier = RiskTier.CRITICAL

        reasons = []
        if financial > 50:
            reasons.append(f"High financial exposure ({action.financial_value})")
        if behavioral > 50:
            reasons.append("Behavioral deviation from agent baseline")
        if rate > 0:
            reasons.append("Request rate exceeds normal window")

        return RiskResult(
            score=composite,
            tier=tier,
            components={"financial": financial, "behavioral": behavioral, "rate": rate},
            reasons=reasons,
        )
