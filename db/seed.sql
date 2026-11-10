-- Seed data for the mock signal-tracker MCP server.
-- All timestamps are relative to datetime('now') so every reset yields a fresh
-- "last 24h" window. Timestamps are UTC, ISO-8601.

-- FKs off while seeding: `lines` is derived from signal_history. The server enables them.
PRAGMA foreign_keys = OFF;

DROP TABLE IF EXISTS handoffs;
DROP TABLE IF EXISTS tickets;
DROP TABLE IF EXISTS signal_history;
DROP TABLE IF EXISTS incidents;
DROP TABLE IF EXISTS customers;
DROP TABLE IF EXISTS lines;
DROP TABLE IF EXISTS nodes;

CREATE TABLE nodes (
    node_id   TEXT PRIMARY KEY,
    name      TEXT NOT NULL,
    zone      TEXT NOT NULL
);

CREATE TABLE lines (
    line_id          TEXT PRIMARY KEY,
    node_id          TEXT NOT NULL REFERENCES nodes(node_id),
    technology       TEXT NOT NULL,              -- FTTH | VDSL
    status           TEXT NOT NULL CHECK (status IN ('online', 'offline')),
    rx_power_dbm     REAL,                       -- NULL when offline
    snr_db           REAL,
    sync_down_mbps   REAL,
    sync_up_mbps     REAL,
    last_change_at   TEXT NOT NULL               -- last status transition
);

CREATE TABLE customers (
    customer_id  TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    phone        TEXT NOT NULL UNIQUE,
    account_id   TEXT NOT NULL UNIQUE,
    line_id      TEXT NOT NULL UNIQUE REFERENCES lines(line_id),
    node_id      TEXT NOT NULL REFERENCES nodes(node_id),
    plan         TEXT NOT NULL
);

CREATE TABLE incidents (
    incident_id   TEXT PRIMARY KEY,
    node_id       TEXT NOT NULL REFERENCES nodes(node_id),
    type          TEXT NOT NULL,                 -- outage | degradation | maintenance
    status        TEXT NOT NULL CHECK (status IN ('active', 'resolved')),
    description   TEXT NOT NULL,
    started_at    TEXT NOT NULL,
    eta           TEXT,                          -- estimated resolution (active only)
    resolved_at   TEXT
);

CREATE TABLE signal_history (
    line_id          TEXT NOT NULL REFERENCES lines(line_id),
    ts               TEXT NOT NULL,
    status           TEXT NOT NULL CHECK (status IN ('online', 'offline')),
    rx_power_dbm     REAL,
    snr_db           REAL,
    latency_ms       REAL,
    packet_loss_pct  REAL,
    PRIMARY KEY (line_id, ts)
);

CREATE TABLE tickets (
    ticket_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id  TEXT NOT NULL REFERENCES customers(customer_id),
    summary      TEXT NOT NULL,
    severity     TEXT NOT NULL CHECK (severity IN ('low', 'medium', 'high', 'critical')),
    status       TEXT NOT NULL DEFAULT 'open',
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE handoffs (
    handoff_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id  TEXT NOT NULL,
    reason           TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'pending',
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- ---------------------------------------------------------------------------
-- Nodes
--   NODO-NORTE-01  : ACTIVE outage (fiber cut), ETA +3h
--   NODO-CENTRO-02 : no incidents at all
--   NODO-SUR-03    : one incident resolved yesterday, nothing active
-- ---------------------------------------------------------------------------
INSERT INTO nodes VALUES
    ('NODO-NORTE-01',  'Nodo Norte 01',  'Barrio Norte'),
    ('NODO-CENTRO-02', 'Nodo Centro 02', 'Centro'),
    ('NODO-SUR-03',    'Nodo Sur 03',    'Zona Sur');

INSERT INTO incidents VALUES
    ('INC-2041', 'NODO-NORTE-01', 'outage', 'active',
     'Corte de fibra troncal por obras en la vía pública. Cuadrilla en el lugar.',
     strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-2 hours'),
     strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '+3 hours'),
     NULL),
    ('INC-2033', 'NODO-SUR-03', 'maintenance', 'resolved',
     'Mantenimiento programado de OLT.',
     strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-20 hours'),
     NULL,
     strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-19 hours'));

-- ---------------------------------------------------------------------------
-- Line profiles (scratch table used only to generate data)
--   good         : online, healthy signal
--   weak         : online, low rx power / high loss
--   offline      : customer-side down for ~5h, node healthy (CPE/drop issue)
--   intermittent : online now, repeated drops over the last 24h
--   outage       : down for ~2h because of INC-2041
-- ---------------------------------------------------------------------------
CREATE TEMP TABLE profiles (
    line_id TEXT PRIMARY KEY, node_id TEXT, tech TEXT, kind TEXT,
    base_rx REAL, base_snr REAL, base_lat REAL, base_loss REAL,
    down_mbps REAL, up_mbps REAL, seed INTEGER
);
INSERT INTO temp.profiles VALUES
    ('LN-1001', 'NODO-CENTRO-02', 'FTTH', 'good',         -19.5, NULL, 9,  0.0, 600, 300, 3),
    ('LN-1002', 'NODO-CENTRO-02', 'FTTH', 'weak',         -27.6, NULL, 38, 2.5, 300, 150, 7),
    ('LN-1003', 'NODO-CENTRO-02', 'FTTH', 'offline',      -20.1, NULL, 11, 0.0, 300, 150, 11),
    ('LN-1004', 'NODO-CENTRO-02', 'VDSL', 'intermittent',  NULL, 9.5, 28, 4.0, 50,  10,  13),
    ('LN-1005', 'NODO-NORTE-01',  'FTTH', 'outage',       -18.9, NULL, 8,  0.0, 1000, 500, 17),
    ('LN-1006', 'NODO-NORTE-01',  'FTTH', 'outage',       -21.2, NULL, 10, 0.0, 300, 150, 19),
    ('LN-1007', 'NODO-NORTE-01',  'VDSL', 'outage',        NULL, 18.0, 15, 0.0, 50,  10,  23),
    ('LN-1008', 'NODO-SUR-03',    'FTTH', 'good',         -20.4, NULL, 12, 0.0, 600, 300, 29),
    ('LN-1009', 'NODO-SUR-03',    'VDSL', 'weak',          NULL, 6.0, 45, 3.0, 30,  5,   31),
    ('LN-1010', 'NODO-SUR-03',    'FTTH', 'good',         -18.2, NULL, 7,  0.0, 1000, 500, 37);

-- 97 samples per line, every 15 min, from now-24h (n=0) to now (n=96).
WITH RECURSIVE steps(n) AS (
    SELECT 0 UNION ALL SELECT n + 1 FROM steps WHERE n < 96
),
raw AS (
    SELECT p.*, s.n,
           strftime('%Y-%m-%dT%H:%M:%SZ', 'now', printf('-%d minutes', (96 - s.n) * 15)) AS ts,
           ((s.n * 37 + p.seed * 11) % 21 - 10) / 10.0 AS jitter,    -- -1.0 .. +1.0
           CASE
               WHEN p.kind = 'offline'      AND s.n >= 76            THEN 'offline'  -- last 5h
               WHEN p.kind = 'outage'       AND s.n >= 88            THEN 'offline'  -- last 2h
               WHEN p.kind = 'intermittent' AND (s.n * 7) % 13 < 2   THEN 'offline'  -- ~15% drops
               ELSE 'online'
           END AS st
    FROM temp.profiles p CROSS JOIN steps s
)
INSERT INTO signal_history (line_id, ts, status, rx_power_dbm, snr_db, latency_ms, packet_loss_pct)
SELECT line_id, ts, st,
       CASE WHEN st = 'online' AND base_rx  IS NOT NULL THEN round(base_rx + jitter * (CASE kind WHEN 'intermittent' THEN 3 ELSE 1 END), 1) END,
       CASE WHEN st = 'online' AND base_snr IS NOT NULL THEN round(base_snr + jitter * (CASE kind WHEN 'intermittent' THEN 3 ELSE 1 END), 1) END,
       CASE WHEN st = 'online' THEN round(base_lat * (1 + abs(jitter) * (CASE WHEN kind IN ('weak','intermittent') THEN 1.5 ELSE 0.3 END)), 1) END,
       CASE WHEN st = 'online' THEN round(max(0, base_loss + jitter * base_loss * 0.8), 2) END
FROM raw;

-- Current line state = latest sample + last status transition time.
INSERT INTO lines (line_id, node_id, technology, status, rx_power_dbm, snr_db,
                   sync_down_mbps, sync_up_mbps, last_change_at)
SELECT p.line_id, p.node_id, p.tech, h.status, h.rx_power_dbm, h.snr_db,
       CASE WHEN h.status = 'online' THEN
            CASE p.kind WHEN 'weak' THEN round(p.down_mbps * 0.6) ELSE p.down_mbps END END,
       CASE WHEN h.status = 'online' THEN
            CASE p.kind WHEN 'weak' THEN round(p.up_mbps * 0.6) ELSE p.up_mbps END END,
       COALESCE(
           (SELECT min(h2.ts) FROM signal_history h2
             WHERE h2.line_id = p.line_id AND h2.status = h.status
               AND h2.ts > COALESCE((SELECT max(h3.ts) FROM signal_history h3
                                      WHERE h3.line_id = p.line_id AND h3.status <> h.status), '')),
           h.ts)
FROM temp.profiles p
JOIN signal_history h ON h.line_id = p.line_id
WHERE h.ts = (SELECT max(ts) FROM signal_history WHERE line_id = p.line_id);

DROP TABLE temp.profiles;

-- ---------------------------------------------------------------------------
-- Customers
-- ---------------------------------------------------------------------------
INSERT INTO customers VALUES
    ('CUS-001', 'María González',     '+34600111001', 'ACC-50001', 'LN-1001', 'NODO-CENTRO-02', 'Fibra 600 Mb'),
    ('CUS-002', 'José Martínez',      '+34600111002', 'ACC-50002', 'LN-1002', 'NODO-CENTRO-02', 'Fibra 300 Mb'),
    ('CUS-003', 'Lucía Fernández',    '+34600111003', 'ACC-50003', 'LN-1003', 'NODO-CENTRO-02', 'Fibra 300 Mb'),
    ('CUS-004', 'Carlos Rodríguez',   '+34600111004', 'ACC-50004', 'LN-1004', 'NODO-CENTRO-02', 'VDSL 50 Mb'),
    ('CUS-005', 'Ana López',          '+34600111005', 'ACC-50005', 'LN-1005', 'NODO-NORTE-01',  'Fibra 1 Gb'),
    ('CUS-006', 'Javier Sánchez',     '+34600111006', 'ACC-50006', 'LN-1006', 'NODO-NORTE-01',  'Fibra 300 Mb'),
    ('CUS-007', 'Carmen Pérez',       '+34600111007', 'ACC-50007', 'LN-1007', 'NODO-NORTE-01',  'VDSL 50 Mb'),
    ('CUS-008', 'Miguel Gómez',       '+34600111008', 'ACC-50008', 'LN-1008', 'NODO-SUR-03',    'Fibra 600 Mb'),
    ('CUS-009', 'Laura Díaz',         '+34600111009', 'ACC-50009', 'LN-1009', 'NODO-SUR-03',    'VDSL 30 Mb'),
    ('CUS-010', 'Pablo Ruiz',         '+34600111010', 'ACC-50010', 'LN-1010', 'NODO-SUR-03',    'Fibra 1 Gb');
