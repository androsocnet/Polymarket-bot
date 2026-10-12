CREATE TABLE approximate_feeds (
    provider TEXT NOT NULL,
    pair TEXT NOT NULL,
    quote_currency TEXT NOT NULL,
    PRIMARY KEY (provider, pair)
);

CREATE TABLE approximate_observations (
    observation_id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES recording_runs(run_id),
    provider TEXT NOT NULL,
    pair TEXT NOT NULL,
    quote_currency TEXT NOT NULL,
    price_text TEXT,
    bid_price_text TEXT,
    ask_price_text TEXT,
    source_timestamp_us INTEGER,
    receipt_wall_us INTEGER NOT NULL,
    receipt_server_us INTEGER,
    raw_payload_hash TEXT NOT NULL,
    raw_payload_size INTEGER NOT NULL,
    transport_type TEXT NOT NULL,
    parser_revision TEXT NOT NULL,
    http_status INTEGER,
    result TEXT NOT NULL,
    error_code TEXT,
    receipt_mono_ns INTEGER
);

CREATE TABLE approximate_references (
    reference_id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES recording_runs(run_id),
    mode TEXT NOT NULL CHECK (mode = 'APPROXIMATE'),
    reference_kind TEXT NOT NULL,
    value_text TEXT,
    window_start_us INTEGER NOT NULL,
    window_end_us INTEGER NOT NULL,
    observation_count INTEGER NOT NULL,
    source_provider TEXT NOT NULL,
    was_fallback INTEGER NOT NULL CHECK (was_fallback IN (0, 1)),
    verification_status TEXT NOT NULL,
    receipt_wall_us INTEGER NOT NULL,
    receipt_server_us INTEGER,
    diagnostics_json TEXT,
    receipt_mono_ns INTEGER,
    window_time_domain TEXT NOT NULL DEFAULT 'LOCAL_WALL'
);
