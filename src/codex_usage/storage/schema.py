"""SQLite schema for local operational state and disposable query data."""

DATABASE_VERSION = 2

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS source_cursors (
    source_id TEXT PRIMARY KEY,
    source_path TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    byte_offset INTEGER NOT NULL CHECK (byte_offset >= 0),
    last_complete_line_digest TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS outbox_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    enqueued_at TEXT NOT NULL,
    ledger_path TEXT,
    flushed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_outbox_pending
    ON outbox_events(flushed_at, sequence);

CREATE TABLE IF NOT EXISTS parser_issues (
    issue_id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL,
    code TEXT NOT NULL,
    record_position INTEGER NOT NULL,
    cli_version TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    UNIQUE(source_id, code, record_position, cli_version)
);

CREATE TABLE IF NOT EXISTS collect_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    detail_code TEXT
);

CREATE TABLE IF NOT EXISTS sync_runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    detail_code TEXT
);

CREATE TABLE IF NOT EXISTS usage_events (
    source_event_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE,
    revision INTEGER NOT NULL,
    occurred_at TEXT NOT NULL,
    device_id TEXT NOT NULL,
    key_id TEXT NOT NULL,
    original_project_id TEXT,
    effective_project_id TEXT,
    project_resolution TEXT NOT NULL,
    activity_repository_count INTEGER NOT NULL,
    thread_key TEXT NOT NULL,
    root_thread_key TEXT,
    parent_thread_key TEXT,
    forked_from_thread_key TEXT,
    turn_key TEXT,
    token_event_ordinal INTEGER NOT NULL,
    operation TEXT NOT NULL,
    model TEXT,
    reasoning_effort TEXT,
    source_kind TEXT NOT NULL,
    cli_version TEXT,
    delta_input_tokens INTEGER,
    delta_cached_input_tokens INTEGER,
    delta_cache_write_input_tokens INTEGER,
    delta_output_tokens INTEGER,
    delta_reasoning_output_tokens INTEGER,
    delta_total_tokens INTEGER,
    flags_json TEXT NOT NULL,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_usage_project_time
    ON usage_events(effective_project_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_usage_device_time
    ON usage_events(device_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_usage_model_time
    ON usage_events(model, occurred_at);
CREATE INDEX IF NOT EXISTS idx_usage_thread
    ON usage_events(thread_key, turn_key);

CREATE TABLE IF NOT EXISTS mapping_events (
    logical_key TEXT PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE,
    revision INTEGER NOT NULL,
    occurred_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    subject_type TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    target_project_id TEXT,
    display_value TEXT,
    payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS project_aliases (
    source_project_id TEXT PRIMARY KEY,
    target_project_id TEXT NOT NULL,
    event_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS quota_snapshots (
    event_id TEXT PRIMARY KEY,
    occurred_at TEXT NOT NULL,
    device_id TEXT NOT NULL,
    key_id TEXT NOT NULL,
    scope_key TEXT,
    window_minutes INTEGER,
    used_percent REAL,
    remaining_percent REAL,
    reset_at TEXT,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_quota_time
    ON quota_snapshots(occurred_at);

CREATE TABLE IF NOT EXISTS read_model_state (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    generation INTEGER NOT NULL,
    rebuilt_at TEXT NOT NULL,
    key_id TEXT,
    input_event_count INTEGER NOT NULL,
    effective_usage_count INTEGER NOT NULL
);

CREATE VIEW IF NOT EXISTS usage_daily_utc AS
SELECT
    substr(occurred_at, 1, 10) AS utc_date,
    effective_project_id,
    device_id,
    model,
    SUM(delta_input_tokens) AS input_tokens,
    SUM(delta_cached_input_tokens) AS cached_input_tokens,
    SUM(delta_output_tokens) AS output_tokens,
    SUM(delta_reasoning_output_tokens) AS reasoning_output_tokens,
    SUM(delta_total_tokens) AS total_tokens,
    COUNT(*) AS event_count
FROM usage_events
WHERE delta_total_tokens IS NOT NULL
GROUP BY substr(occurred_at, 1, 10), effective_project_id, device_id, model;
"""
