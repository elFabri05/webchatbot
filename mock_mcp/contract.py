"""Tool contracts (names, inputs, outputs) the mock MCP server exposes.

!!  SIGNAL TRACKER TOOLS MUST MIRROR THE REAL MCP SERVER EXACTLY.            !!
!!  The real tool list/schemas were not provided yet, so the signal-tool      !!
!!  names, parameters and output models below are PROVISIONAL. When the real  !!
!!  schemas arrive, update this file (and the matching functions in           !!
!!  server.py) and run `uv run python scripts/compare_tools.py` against the   !!
!!  real server to confirm they match.                                        !!

The pydantic models below become each tool's MCP `outputSchema`.
"""
from typing import Literal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Signal tracker tools (PROVISIONAL – must match the real server)
# ---------------------------------------------------------------------------
LineStatus = Literal["online", "offline"]
SignalQuality = Literal["good", "weak", "none"]


class LineStatusResult(BaseModel):
    line_id: str
    node_id: str
    technology: Literal["FTTH", "VDSL"]
    status: LineStatus
    signal_quality: SignalQuality = Field(
        description="good/weak derived from rx power (FTTH) or SNR (VDSL); none when offline"
    )
    rx_power_dbm: float | None = Field(None, description="Optical receive power (FTTH only)")
    snr_db: float | None = Field(None, description="Signal-to-noise margin (VDSL only)")
    sync_down_mbps: float | None = None
    sync_up_mbps: float | None = None
    last_change_at: str = Field(description="UTC ISO-8601 time of last status transition")


class SignalSample(BaseModel):
    ts: str
    status: LineStatus
    rx_power_dbm: float | None = None
    snr_db: float | None = None
    latency_ms: float | None = None
    packet_loss_pct: float | None = None


class SignalHistorySummary(BaseModel):
    samples: int
    drops: int = Field(description="Number of online→offline transitions in the window")
    offline_minutes: int
    avg_latency_ms: float | None = None
    avg_packet_loss_pct: float | None = None


class SignalHistoryResult(BaseModel):
    line_id: str
    hours: int
    summary: SignalHistorySummary
    samples: list[SignalSample]


class Incident(BaseModel):
    incident_id: str
    node_id: str
    type: Literal["outage", "degradation", "maintenance"]
    status: Literal["active", "resolved"]
    description: str
    started_at: str
    eta: str | None = Field(None, description="Estimated resolution time, UTC ISO-8601")
    resolved_at: str | None = None


class NodeStatusResult(BaseModel):
    node_id: str
    name: str
    zone: str
    has_active_incident: bool
    active_incidents: list[Incident]
    affected_lines: int = Field(description="Lines on this node currently offline")
    total_lines: int


# ---------------------------------------------------------------------------
# Mock customer / workflow tools (mock-only; real equivalents TBD)
# ---------------------------------------------------------------------------
Severity = Literal["low", "medium", "high", "critical"]


class Customer(BaseModel):
    customer_id: str
    name: str
    phone: str
    account_id: str
    line_id: str
    node_id: str
    plan: str


class FindCustomerResult(BaseModel):
    found: bool
    customer: Customer | None = None


class TicketResult(BaseModel):
    ticket_id: int
    customer_id: str
    summary: str
    severity: Severity
    status: str
    created_at: str


class HandoffResult(BaseModel):
    handoff_id: int
    conversation_id: str
    reason: str
    status: str
    created_at: str
