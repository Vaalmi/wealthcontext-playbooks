"""The closed values this layer shares with WealthContext, each named once."""

from typing import Literal

from pydantic import BaseModel, ConfigDict

WarningCode = Literal[
    "AMBIGUOUS",
    "AMBIGUOUS_ENTITY",
    "PRICE_STALE",
    "SOURCE_UNAVAILABLE",
    "FX_UNAVAILABLE",
    "HOUSEHOLD_MEMBERSHIP_CONFLICT",
    "REPORTING_HOUSEHOLD_CONFLICT",
    "OWNERSHIP_CONFLICT",
    "OWNERSHIP_UNAVAILABLE",
    "PENDING_SETTLEMENT",
    "QUANTITY_BREAK",
    "CASH_BREAK",
    "RECONCILIATION_UNAVAILABLE",
    "PRICE_SOURCE_MISMATCH",
    "CLOSED_ACCOUNT_WITH_BALANCE",
    "HELD_AWAY_EXCLUDED",
]
SourceFamily = Literal["PAS", "CUSTODY"]
Custodian = Literal["A", "B"]
FeedStatus = Literal["FAILED", "PARTIAL", "TIMEOUT"]
# In a step's condition: matches any value, and an exception that has none.
Any_ = Literal["any"]
ANY: Any_ = "any"
# Who a log says made a move.
ANALYST, OWNER, AGENT, BACKEND = "analyst", "playbook_owner", "triage_agent", "backend"
CURATOR = "curator"


class FeedFacts(BaseModel):
    """What the backend knows of an exception it tied to a failed feed run."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source_family: SourceFamily
    custodian: Custodian
    feed_status: FeedStatus
