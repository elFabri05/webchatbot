# ISP support chatbot – trial

A Spanish-language support chatbot for internet problems: no connection, slow internet, connection drops.
Before it diagnoses anything, the bot checks the customer's line and their area through the
**signal tracker MCP server**. This trial runs against a **mock** MCP server with seeded SQLite data that exposes the
same tool interface, so you can switch to the real tracker by changing configuration.

- **Model:** Claude (`claude-opus-5-5` by default) through the Anthropic API, using a manual tool-use loop with at most 5 tool calls per customer message.
- **Pages:** a web chat at `/` with a tool-call debug panel, and an admin page at `/admin` listing tickets, handoffs and token usage.
- **Stack:** FastAPI + uv, SQLite, Docker Compose.

---

## 1. Setup

You need Claude API credentials. Put `ANTHROPIC_API_KEY=...` in `.env`, or leave it empty to use the SDK's
default credential chain (for example a profile from `ant auth login`).

### With Docker (recommended)

```bash
cp .env.example .env        # then set ANTHROPIC_API_KEY
docker compose up --build   # or: make up
```

| URL | What |
|---|---|
| http://localhost:8000/ | Chat (`?conversation=<id>` reopens a conversation) |
| http://localhost:8000/admin | Admin: tickets, handoffs, token usage per conversation |
| http://localhost:8000/health | Status and the tools loaded from MCP |
| http://localhost:8001/mcp | Mock MCP server (for `compare_tools.py`) |

- The mock seeds its data on first start. The app waits until the mock is healthy.
- Conversations and mock data live in Docker volumes, so they survive restarts. `docker compose down -v` deletes them.
- Both ports are bound to `127.0.0.1`, because `/admin` has no authentication.

### Without Docker

```bash
cp .env.example .env        # then set ANTHROPIC_API_KEY
make install                # uv sync + seed data/isp.db
make run                    # http://localhost:8000
```

Locally, the app launches the mock as a subprocess over stdio (`MCP_SIGNAL_SERVER="python -m mock_mcp.server"`).
To use HTTP like in Docker, run `make mock-http` and set `MCP_SIGNAL_SERVER=http://localhost:8001/mcp`.

### Resetting data

```bash
make reset-db
```

This rebuilds the mock tracker data from `db/seed.sql` and clears all conversations, tickets in the admin view, and
handoffs. It acts on the Docker stack if it's running, otherwise on the local `data/` files.

**Run it at least daily.** The seed is relative to the moment of the reset: "the last 24 h" of signal history, an outage that
started 2 h ago with an ETA 3 h ahead. After a few hours the ETA is in the past and the scenarios stop making sense.

### Make targets

| Target | |
|---|---|
| `make up` / `make down` / `make logs` | Docker Compose stack |
| `make install` / `make run` / `make mock-http` | Local development |
| `make reset-db` | Fresh seed data + clear conversations |
| `make test` | Test suite, no API calls |
| `make test-live` | Scenario tests against the real Claude API (costs money) |

---

## 2. Test customers

Give the bot a phone number in any format (`600111005`, `600 111 005`, `+34 600 111 005`) or an account id
(`ACC-50005`). Each customer is set up for one scenario:

| Scenario | Customer | Phone | Account | What's in the data | What the bot should do |
|---|---|---|---|---|---|
| **Area outage** | Ana López | 600 111 005 | ACC-50005 | Line offline; node NORTE-01 has active outage INC-2041 (fibre cut), ETA ≈ 3 h after reset | Explain the outage in the area and give the ETA in local time. No troubleshooting at home. |
| Area outage | Javier Sánchez | 600 111 006 | ACC-50006 | Same node, fibre | Same |
| Area outage | Carmen Pérez | 600 111 007 | ACC-50007 | Same node, VDSL | Same |
| **Line offline** | Lucía Fernández | 600 111 003 | ACC-50003 | Line down for ~5 h; node CENTRO-02 healthy | Problem is on their line: open a ticket (high) and hand off for a technical visit |
| **Weak signal** | José Martínez | 600 111 002 | ACC-50002 | Fibre, online, rx power ≈ −27 dBm, some packet loss | Ticket and handoff (needs a technician). No troubleshooting at home. |
| Weak signal | Laura Díaz | 600 111 009 | ACC-50009 | VDSL, online, SNR ≈ 6 dB | Same |
| **Intermittent drops** | Carlos Rodríguez | 600 111 004 | ACC-50004 | Online *right now*, but ~15 drops (~4 h offline) in the last 24 h | Check the signal history, open a ticket, hand off |
| **All good** | María González | 600 111 001 | ACC-50001 | Healthy fibre, no incidents | Basic troubleshooting one step at a time; hand off after 2 failed attempts |
| All good | Miguel Gómez | 600 111 008 | ACC-50008 | Healthy fibre (node had maintenance yesterday, resolved) | Same |
| All good | Pablo Ruiz | 600 111 010 | ACC-50010 | Healthy fibre, 1 Gb | Same |
| **Unknown customer** | – | 699 000 000 (anything not above) | ACC-99999 | No match | Ask the customer to check the number. No diagnosis. |

A few more things to try:
- **Escalation triggers:** talk about billing ("me cobrasteis dos veces"), sound angry, or ask for a person. Each should hand off to a human.
- **MCP server down:** start a conversation, then stop the tracker. In Docker, run `docker compose stop mock-mcp`. Locally, run the mock with `make mock-http`, set `MCP_SIGNAL_SERVER=http://localhost:8001/mcp`, then stop it with Ctrl+C. The bot should say it can't check right now and must not invent any status or ETA. A dead tracker fails immediately; a hung one gives up after `MCP_CALL_TIMEOUT` (15 s). Note that the app loads the tool list at startup, so it won't **start** while the tracker is down. In Docker, the restart policy retries until it's back.

Network summary: **NODO-NORTE-01** has the active outage, **NODO-CENTRO-02** has no incidents, and **NODO-SUR-03** had a
maintenance window about 20 h ago, now resolved. Signal history is sampled every 15 minutes.

---

## 3. Switching to the real signal tracker

The tracker is chosen by configuration only:

| `MCP_SIGNAL_SERVER` value | Transport |
|---|---|
| `"python -m mock_mcp.server"` | stdio. Any value that isn't a URL is the command line to launch. |
| `http://mock-mcp:8001/mcp` | Streamable HTTP (the mock in Docker) |
| `https://<real-tracker>/mcp` | Streamable HTTP. `MCP_SIGNAL_TOKEN` is sent as a Bearer token. |

### Steps

1. **Make the mock mirror the real tool contract.** The signal-tool schemas in `mock_mcp/contract.py` are
   **provisional**: they were written before the real schemas were available. Update them and `mock_mcp/server.py` to
   match the real server, then check that the two match:
   ```bash
   uv run python scripts/compare_tools.py "python -m mock_mcp.server" https://<real-tracker>/mcp
   ```
   It reports missing or extra tools and any difference in input or output schemas. The mock-only customer tools are ignored.
2. **Tool names.** The prompt, the bot and the admin page refer to tools through `app/tool_names.py`.
   - If the real names match, nothing changes.
   - If they don't, edit that one file.
   - At startup the app logs a warning for every expected tool that no connected server exposes. Check `/health` for the loaded tool list.
3. **Point the app at it.**
   - Local: set `MCP_SIGNAL_SERVER=https://<real-tracker>/mcp` (and `MCP_SIGNAL_TOKEN`) in `.env`.
   - Docker: `docker-compose.yml` overrides `MCP_SIGNAL_SERVER` for the app container. Add
     `MCP_SIGNAL_SERVER_DOCKER=https://<real-tracker>/mcp` to `.env` (Compose reads it for that substitution), then
     run `docker compose up -d app`. The `mock-mcp` service can stay up or be removed.
4. **Customer tools.** The real tracker probably doesn't provide `find_customer`, `create_ticket` and `handoff_to_human`.
   Set `MCP_CUSTOMER_SERVER` to the server that does. In the trial you can keep the mock for these while the signal data
   comes from the real tracker: `MCP_CUSTOMER_SERVER=http://mock-mcp:8001/mcp`. If both servers expose a tool with the
   same name, the signal server wins.
5. **Check it.** `/health` should list the expected tools. `POST /debug/tools/<name>` calls a tool directly, for example
   `curl -X POST localhost:8000/debug/tools/get_node_status -H 'content-type: application/json' -d '{"node_id":"…"}'`.
   Then chat with a real customer's line.

The admin page reads tickets and handoffs from the app's own log of tool calls, not from the mock database, so it keeps
working with the real servers.

---

## 4. Configuration (`.env`)

| Variable | Default | |
|---|---|---|
| `MCP_SIGNAL_SERVER` | `"python -m mock_mcp.server"` | Signal tracker: URL (HTTP) or command (stdio) |
| `MCP_SIGNAL_TOKEN` | – | Bearer token for HTTP MCP servers |
| `MCP_CUSTOMER_SERVER` | same as signal | Server with the customer, ticket and handoff tools |
| `MCP_CALL_TIMEOUT` | `15` | Seconds per tool call before treating the tracker as down |
| `ANTHROPIC_API_KEY` | – | Empty = SDK default credential chain |
| `BOT_MODEL` | `claude-opus-5-5` | Model for the chat |
| `BOT_SUMMARY_MODEL` | same as model | Model for the running summary |
| `BOT_EFFORT` | `medium` | `low` … `max`. Lower is cheaper and faster. |
| `BOT_MAX_TOOL_CALLS` | `5` | Tool calls per customer message |
| `BOT_HISTORY_WINDOW` | `10` | Messages kept verbatim; older ones are summarised |
| `BOT_TIMEZONE` | `Europe/Madrid` | Local time for ETAs (tools return UTC) |
| `BOT_FALLBACKS` | `true` | If the model declines a request, the API retries it on a fallback model |
| `APP_DB_PATH` | `data/app.db` | Conversations, usage, tool-call log |
| `MOCK_DB_PATH` | `data/isp.db` | Mock tracker data |
| `CHAT_DEBUG` | `true` | Return tool calls and usage to the chat page. **Set to `false` before real customers use it.** |

---

## 5. Tests

```bash
make test        # 33 tests, no API calls, ~20 s
make test-live   # scenario tests with the real model
```

- **`tests/test_scenarios.py`:** one scripted conversation per seeded scenario (area outage, line offline, weak signal ×2,
  intermittent drops, all-good line, unknown customer, MCP server down). Claude's side is scripted, so these tests check:
  - that each scenario produces the data the bot must base its answer on
  - that the plumbing works end to end against the real mock server: customer memory, tool budget, tickets, handoffs
  - that the bot fails fast when the tracker dies mid-conversation
- **`tests/test_live.py`:** the same scenarios with the **real model**. They check behaviour:
  - it checks the line and node before diagnosing
  - it reports the real ETA in local time
  - it escalates on a broken line, on billing, and after 2 failed attempts
  - it doesn't diagnose an unknown customer, and doesn't invent data when the tracker is down
  - the prompt cache gets hit

  Assertions are structural, so normal wording variation passes. These tests are skipped unless `RUN_LIVE_TESTS=1`; each run is a few dozen API calls.
- **`tests/test_bot.py`, `tests/test_api.py`, `tests/test_mock_mcp.py`:** loop mechanics, request and cache layout, history
  window and summary, HTTP endpoints, the mock's tools.

---

## 6. How it works

```
browser ──POST /chat──▶ FastAPI ──▶ ChatBot loop ──▶ Claude Messages API
                                        │  ▲
                              tool_use  ▼  │ tool_result
                                    MCPToolbox ──stdio/HTTP──▶ signal tracker MCP (mock or real)
                                        │
                               SQLite: conversations, messages, usage, tool-call log
```

- **Loop** (`app/bot.py`):
  - The model calls tools and the bot runs them, in parallel when the model asks for several at once.
  - After `BOT_MAX_TOOL_CALLS`, further calls get an error result, and the next request uses `tool_choice: none`, so the model has to answer with what it has.
  - `handoff_to_human` always receives the real conversation id. After a handoff, the bot stops answering that conversation.
- **Prompt** (`app/prompts.py`, in Spanish): identify the customer → check line + area → troubleshoot only if the line
  looks fine → escalate on 2 failed attempts, anger, billing, or a technical visit. Never invent ETAs or data.
- **Memory:** the model sees the last 10 messages plus a running summary of older ones. The identified customer and a compact log of
  recent tool results are added to each turn's context. Every message is stored for audit.
- **Prompt caching:** the tool list (sorted, built once) and the system prompt (frozen at startup) each carry a cache
  breakpoint. Per-conversation data goes in a trailing `role: "system"` message, so it never breaks the cached
  prefix. Check the cache columns on `/admin`.
- **History:** past turns are replayed as plain text, so trimming the window never edits a turn that Claude's earlier
  reasoning depends on.
- **MCP calls:** one short session per call with a hard timeout. If the tracker crashes, the call fails in milliseconds, and a
  hung tracker hits the timeout; neither takes the app down. Cost: about 50 ms per call over HTTP, about 0.4 s over stdio.

## 7. Project layout

```
app/
  main.py          FastAPI app: /chat, pages, admin + debug endpoints
  bot.py           tool-use loop, history window, running summary
  prompts.py       system prompt, per-turn context, summary prompt
  tool_names.py    the MCP tool names the bot depends on
  mcp_client.py    transport-agnostic MCP client + toolbox
  storage.py       SQLite conversation storage
  static/          chat.html, admin.html (no build step)
mock_mcp/
  contract.py      tool schemas (signal tools PROVISIONAL)
  server.py        SQLite-backed mock MCP server (stdio or HTTP)
db/seed.sql        schema + seed data (relative to reset time)
scripts/           reset_db.py, compare_tools.py, chat.py (terminal client)
tests/             see §5
```

## 8. Known limitations (trial)

- **Signal tool schemas are provisional.** They must be replaced with the real tracker's contract (§3).
- **`/admin` has no authentication.** Ports are bound to localhost; add authentication before exposing the app.
- **`CHAT_DEBUG=true` shows tool data to whoever uses the chat.** Turn it off for real customers.
- **Replies aren't streamed.** The page shows a typing indicator until the full reply arrives.
- **Not yet verified against the live API or with Docker.** `make test-live` and `make up` haven't been run in the environment
  where this was built.
# webchatbot
