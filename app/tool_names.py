"""The MCP tool names the bot relies on, in one place.

The system prompt, the bot's special handling (customer memory, handoff,
history notes) and the admin page all refer to tools through these constants.
If the real signal tracker names a tool differently, change it here; the app
logs a warning at startup for any name below that no connected server exposes.
"""
FIND_CUSTOMER = "find_customer"
LINE_STATUS = "get_line_status"
NODE_STATUS = "get_node_status"
SIGNAL_HISTORY = "get_signal_history"
CREATE_TICKET = "create_ticket"
HANDOFF = "handoff_to_human"

REQUIRED = (FIND_CUSTOMER, LINE_STATUS, NODE_STATUS, SIGNAL_HISTORY, CREATE_TICKET, HANDOFF)

# Placeholders available to the system prompt template.
PROMPT_NAMES = {
    "find_customer": FIND_CUSTOMER,
    "line_status": LINE_STATUS,
    "node_status": NODE_STATUS,
    "signal_history": SIGNAL_HISTORY,
    "create_ticket": CREATE_TICKET,
    "handoff": HANDOFF,
}
