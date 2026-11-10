"""Mock signal-tracker MCP server backed by SQLite.

Run (stdio, default):   python -m mock_mcp.server
Run (HTTP):             python -m mock_mcp.server --http --host 0.0.0.0 --port 8001
                        -> endpoint http://HOST:PORT/mcp
Env: MOCK_DB_PATH (default data/isp.db), MOCK_MCP_ALLOWED_HOSTS (comma list, HTTP only)
"""
import argparse
import os
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Iterator

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field

from mock_mcp.contract import (
    Customer,
    FindCustomerResult,
    HandoffResult,
    Incident,
    LineStatusResult,
    NodeStatusResult,
    Severity,
    SignalHistoryResult,
    SignalHistorySummary,
    SignalSample,
    TicketResult,
)

ROOT = Path(__file__).resolve().parent.parent
SAMPLE_MINUTES = 15  # seed granularity

# Thresholds for signal_quality (FTTH rx power in dBm, VDSL SNR margin in dB)
FTTH_WEAK_BELOW_DBM = -25.0
VDSL_WEAK_BELOW_DB = 8.0


def _db_path() -> Path:
    p = Path(os.getenv("MOCK_DB_PATH", "data/isp.db"))
    return p if p.is_absolute() else ROOT / p


@contextmanager
def db() -> Iterator[sqlite3.Connection]:
    path = _db_path()
    if not path.exists():
        raise RuntimeError(f"Mock DB not found at {path}. Run scripts/reset_db.py first.")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _quality(row: sqlite3.Row) -> str:
    if row["status"] != "online":
        return "none"
    if row["rx_power_dbm"] is not None and row["rx_power_dbm"] < FTTH_WEAK_BELOW_DBM:
        return "weak"
    if row["snr_db"] is not None and row["snr_db"] < VDSL_WEAK_BELOW_DB:
        return "weak"
    return "good"


mcp = MCPServer(
    name="signal-tracker-mock",
    instructions="Mock of the ISP signal tracker. Data is synthetic (SQLite seed).",
)

# ---------------------------------------------------------------------------
# Signal tracker tools (PROVISIONAL contract – see contract.py)
# ---------------------------------------------------------------------------
LineId = Annotated[str, Field(description="Line identifier, e.g. LN-1001")]
NodeId = Annotated[str, Field(description="Network node identifier, e.g. NODO-NORTE-01")]


@mcp.tool()
def get_line_status(line_id: LineId) -> LineStatusResult:
    """Current status and signal levels of a customer's line."""
    with db() as conn:
        row = conn.execute("SELECT * FROM lines WHERE line_id = ?", (line_id,)).fetchone()
    if row is None:
        raise ToolError(f"Unknown line_id: {line_id}")
    return LineStatusResult(**dict(row), signal_quality=_quality(row))


@mcp.tool()
def get_signal_history(
    line_id: LineId,
    hours: Annotated[int, Field(ge=1, le=24, description="Look-back window in hours")] = 24,
) -> SignalHistoryResult:
    """Signal samples for a line over the last N hours (max 24), with a drop summary."""
    with db() as conn:
        if conn.execute("SELECT 1 FROM lines WHERE line_id = ?", (line_id,)).fetchone() is None:
            raise ToolError(f"Unknown line_id: {line_id}")
        rows = conn.execute(
            """SELECT ts, status, rx_power_dbm, snr_db, latency_ms, packet_loss_pct
                 FROM signal_history
                WHERE line_id = ? AND ts >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ?)
                ORDER BY ts""",
            (line_id, f"-{hours} hours"),
        ).fetchall()

    samples = [SignalSample(**dict(r)) for r in rows]
    drops = sum(
        1 for prev, cur in zip(samples, samples[1:])
        if prev.status == "online" and cur.status == "offline"
    )
    online = [s for s in samples if s.status == "online"]
    lat = [s.latency_ms for s in online if s.latency_ms is not None]
    loss = [s.packet_loss_pct for s in online if s.packet_loss_pct is not None]
    summary = SignalHistorySummary(
        samples=len(samples),
        drops=drops,
        offline_minutes=(len(samples) - len(online)) * SAMPLE_MINUTES,
        avg_latency_ms=round(sum(lat) / len(lat), 1) if lat else None,
        avg_packet_loss_pct=round(sum(loss) / len(loss), 2) if loss else None,
    )
    return SignalHistoryResult(line_id=line_id, hours=hours, summary=summary, samples=samples)


@mcp.tool()
def get_node_status(node_id: NodeId) -> NodeStatusResult:
    """Status of a network node: active incidents/outages (with ETA) and affected lines."""
    with db() as conn:
        node = conn.execute("SELECT * FROM nodes WHERE node_id = ?", (node_id,)).fetchone()
        if node is None:
            raise ToolError(f"Unknown node_id: {node_id}")
        incidents = conn.execute(
            "SELECT * FROM incidents WHERE node_id = ? AND status = 'active' ORDER BY started_at",
            (node_id,),
        ).fetchall()
        total, offline = conn.execute(
            "SELECT count(*), coalesce(sum(status = 'offline'), 0) FROM lines WHERE node_id = ?",
            (node_id,),
        ).fetchone()
    return NodeStatusResult(
        node_id=node["node_id"],
        name=node["name"],
        zone=node["zone"],
        has_active_incident=bool(incidents),
        active_incidents=[Incident(**dict(i)) for i in incidents],
        affected_lines=offline,
        total_lines=total,
    )


@mcp.tool()
def list_active_incidents() -> list[Incident]:
    """All currently active incidents/outages across the network."""
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM incidents WHERE status = 'active' ORDER BY started_at"
        ).fetchall()
    return [Incident(**dict(r)) for r in rows]


# ---------------------------------------------------------------------------
# Mock customer / workflow tools
# ---------------------------------------------------------------------------
def _normalize_phone(value: str) -> str:
    digits = re.sub(r"\D", "", value)
    return "+" + digits if digits else ""


@mcp.tool()
def find_customer(
    phone_or_account: Annotated[
        str, Field(description="Phone number (any format) or account id, e.g. ACC-50001")
    ],
) -> FindCustomerResult:
    """Look up a customer by phone number or account id."""
    q = phone_or_account.strip()
    phone = _normalize_phone(q)
    with db() as conn:
        row = conn.execute(
            """SELECT * FROM customers
                WHERE upper(account_id) = upper(?)
                   OR phone = ?
                   -- allow numbers given without country code
                   OR (length(?) >= 9 AND phone LIKE '%' || substr(?, 2))""",
            (q, phone, phone, phone),
        ).fetchone()
    if row is None:
        return FindCustomerResult(found=False)
    return FindCustomerResult(found=True, customer=Customer(**dict(row)))


@mcp.tool()
def create_ticket(
    customer_id: Annotated[str, Field(description="Customer id, e.g. CUS-001")],
    summary: Annotated[str, Field(min_length=1, description="Short description of the problem")],
    severity: Severity,
) -> TicketResult:
    """Open a support ticket for a customer."""
    with db() as conn:
        if conn.execute(
            "SELECT 1 FROM customers WHERE customer_id = ?", (customer_id,)
        ).fetchone() is None:
            raise ToolError(f"Unknown customer_id: {customer_id}")
        row = conn.execute(
            "INSERT INTO tickets (customer_id, summary, severity) VALUES (?, ?, ?) RETURNING *",
            (customer_id, summary, severity),
        ).fetchone()
    return TicketResult(**dict(row))


@mcp.tool()
def handoff_to_human(
    conversation_id: Annotated[str, Field(description="Chat conversation id")],
    reason: Annotated[str, Field(min_length=1, description="Why a human agent is needed")],
) -> HandoffResult:
    """Escalate the conversation to a human agent."""
    with db() as conn:
        row = conn.execute(
            "INSERT INTO handoffs (conversation_id, reason) VALUES (?, ?) RETURNING *",
            (conversation_id, reason),
        ).fetchone()
    return HandoffResult(**dict(row))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--http", action="store_true", help="Serve Streamable HTTP instead of stdio")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8001)
    args = ap.parse_args()

    if not args.http:
        mcp.run()
        return

    security = None
    if hosts := os.getenv("MOCK_MCP_ALLOWED_HOSTS"):
        allowed = [h.strip() for h in hosts.split(",") if h.strip()]
        security = TransportSecuritySettings(allowed_hosts=allowed, allowed_origins=[])
    mcp.run("streamable-http", host=args.host, port=args.port, transport_security=security)


if __name__ == "__main__":
    main()
