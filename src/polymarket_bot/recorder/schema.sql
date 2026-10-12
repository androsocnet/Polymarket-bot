CREATE TABLE recording_runs (
    run_id TEXT PRIMARY KEY NOT NULL,
    started_wall_us INTEGER NOT NULL,
    ended_wall_us INTEGER,
    mode TEXT NOT NULL CHECK (mode IN ('BOOK_ONLY', 'REFERENCE_REQUIRED', 'APPROXIMATE_REFERENCE')),
    reference_activation TEXT NOT NULL CHECK (reference_activation IN ('DISABLED', 'VERIFIED')),
    state TEXT NOT NULL CHECK (state IN ('RUNNING', 'STOPPED', 'FAILED', 'INTERRUPTED')),
    app_revision TEXT NOT NULL,
    config_json TEXT NOT NULL,
    activation_evidence_json TEXT,
    last_committed_wall_us INTEGER,
    exit_reason TEXT,
    CHECK (mode <> 'REFERENCE_REQUIRED' OR reference_activation = 'VERIFIED'),
    CHECK (reference_activation <> 'VERIFIED' OR activation_evidence_json IS NOT NULL)
);

CREATE TABLE clock_anchors (
    anchor_id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES recording_runs(run_id),
    server_seconds INTEGER NOT NULL,
    request_start_mono_ns INTEGER NOT NULL,
    request_end_mono_ns INTEGER NOT NULL,
    request_start_wall_us INTEGER NOT NULL,
    request_end_wall_us INTEGER NOT NULL,
    anchor_mono_ns INTEGER NOT NULL,
    anchor_server_us INTEGER NOT NULL,
    uncertainty_us INTEGER NOT NULL CHECK (uncertainty_us >= 0),
    status TEXT NOT NULL CHECK (status IN ('VALID', 'DISCONTINUITY')),
    diagnostic_json TEXT
);
CREATE INDEX clock_anchors_run_mono ON clock_anchors(run_id, anchor_mono_ns);

CREATE TABLE markets (
    market_id TEXT PRIMARY KEY NOT NULL,
    event_id TEXT NOT NULL,
    condition_id TEXT NOT NULL UNIQUE,
    slug TEXT NOT NULL,
    question TEXT NOT NULL,
    window_start_us INTEGER NOT NULL,
    window_end_us INTEGER NOT NULL,
    first_seen_us INTEGER NOT NULL,
    up_token_id TEXT NOT NULL,
    down_token_id TEXT NOT NULL,
    up_outcome_index INTEGER NOT NULL CHECK (up_outcome_index IN (0, 1)),
    down_outcome_index INTEGER NOT NULL CHECK (down_outcome_index IN (0, 1)),
    lifecycle TEXT NOT NULL CHECK (lifecycle IN ('CURRENT', 'EXPIRED', 'INVALIDATED')),
    resolution_state TEXT NOT NULL CHECK (resolution_state IN ('PENDING', 'RETRY_WAIT', 'RESOLVED_DIRECTIONAL', 'RESOLVED_OTHER', 'CONFLICT')),
    resolution_next_due_us INTEGER,
    resolution_attempts INTEGER NOT NULL CHECK (resolution_attempts >= 0),
    selected_resolution_id INTEGER REFERENCES resolution_observations(resolution_id),
    CHECK (window_end_us - window_start_us = 900000000),
    CHECK (up_token_id <> down_token_id),
    CHECK (up_outcome_index <> down_outcome_index)
);
CREATE INDEX markets_window_start ON markets(window_start_us);
CREATE INDEX markets_resolution_due ON markets(resolution_state, resolution_next_due_us);

CREATE TABLE market_revisions (
    revision_id INTEGER PRIMARY KEY,
    market_id TEXT NOT NULL REFERENCES markets(market_id),
    observed_us INTEGER NOT NULL,
    run_id TEXT NOT NULL REFERENCES recording_runs(run_id),
    description TEXT NOT NULL,
    resolution_source_url TEXT NOT NULL,
    active INTEGER CHECK (active IN (0, 1)),
    closed INTEGER CHECK (closed IN (0, 1)),
    accepting_orders INTEGER CHECK (accepting_orders IN (0, 1)),
    enable_order_book INTEGER CHECK (enable_order_book IN (0, 1)),
    fee_config_json TEXT,
    tick_size_text TEXT,
    min_order_size_text TEXT,
    delay_config_json TEXT,
    gamma_raw_z BLOB NOT NULL,
    clob_config_raw_z BLOB,
    raw_sizes_json TEXT NOT NULL,
    codec_version INTEGER NOT NULL CHECK (codec_version = 1),
    content_hash TEXT NOT NULL
);
CREATE INDEX market_revisions_observed ON market_revisions(market_id, observed_us);

CREATE TABLE sample_slots (
    slot_id INTEGER PRIMARY KEY,
    market_id TEXT NOT NULL REFERENCES markets(market_id),
    slot_index INTEGER NOT NULL CHECK (slot_index BETWEEN 0 AND 449),
    scheduled_us INTEGER NOT NULL,
    run_id TEXT REFERENCES recording_runs(run_id),
    anchor_id INTEGER REFERENCES clock_anchors(anchor_id),
    metadata_revision_id INTEGER NOT NULL REFERENCES market_revisions(revision_id),
    status TEXT NOT NULL CHECK (status IN ('IN_PROGRESS', 'COMPLETE', 'PARTIAL', 'FAILED', 'MISSED', 'INTERRUPTED')),
    started_server_us INTEGER,
    finished_server_us INTEGER,
    committed_wall_us INTEGER,
    start_mono_ns INTEGER,
    end_mono_ns INTEGER,
    delay_us INTEGER CHECK (delay_us >= 0),
    reason TEXT NOT NULL,
    quality_json TEXT NOT NULL,
    reference_status TEXT NOT NULL,
    UNIQUE (market_id, slot_index)
);
CREATE INDEX sample_slots_market_status ON sample_slots(market_id, status);
CREATE INDEX sample_slots_scheduled ON sample_slots(scheduled_us);

CREATE TABLE request_attempts (
    request_id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES recording_runs(run_id),
    market_id TEXT REFERENCES markets(market_id),
    slot_id INTEGER REFERENCES sample_slots(slot_id),
    anchor_id INTEGER REFERENCES clock_anchors(anchor_id),
    operation TEXT NOT NULL,
    attempt_number INTEGER NOT NULL CHECK (attempt_number >= 1),
    method TEXT NOT NULL CHECK (method IN ('GET', 'POST')),
    path TEXT NOT NULL,
    selector_json TEXT NOT NULL,
    started_wall_us INTEGER NOT NULL,
    ended_wall_us INTEGER NOT NULL,
    started_server_us INTEGER,
    ended_server_us INTEGER,
    start_mono_ns INTEGER NOT NULL,
    end_mono_ns INTEGER NOT NULL,
    latency_ns INTEGER NOT NULL CHECK (latency_ns >= 0),
    http_status INTEGER,
    result TEXT NOT NULL,
    error_code TEXT,
    response_bytes INTEGER CHECK (response_bytes >= 0),
    response_raw_z BLOB,
    raw_size_bytes INTEGER CHECK (raw_size_bytes BETWEEN 0 AND 10485760),
    codec_version INTEGER CHECK (codec_version = 1),
    raw_complete INTEGER NOT NULL CHECK (raw_complete IN (0, 1)),
    CHECK ((response_raw_z IS NULL AND raw_size_bytes IS NULL AND codec_version IS NULL)
        OR (response_raw_z IS NOT NULL AND raw_size_bytes IS NOT NULL AND codec_version IS NOT NULL)),
    CHECK (method <> 'POST' OR (operation = 'BOOK_BATCH' AND path = '/books'))
);
CREATE INDEX request_attempts_operation_started ON request_attempts(operation, started_wall_us);
CREATE INDEX request_attempts_slot ON request_attempts(slot_id);

CREATE TABLE book_observations (
    slot_id INTEGER NOT NULL REFERENCES sample_slots(slot_id),
    outcome TEXT NOT NULL CHECK (outcome IN ('UP', 'DOWN')),
    expected_token_id TEXT NOT NULL,
    expected_condition_id TEXT NOT NULL,
    request_id INTEGER REFERENCES request_attempts(request_id),
    status TEXT NOT NULL CHECK (status IN ('VALID', 'UNAVAILABLE', 'INVALID', 'LATE', 'INTERRUPTED')),
    error_code TEXT,
    quality_json TEXT NOT NULL,
    source_timestamp_raw TEXT,
    source_us INTEGER,
    book_hash TEXT,
    best_bid_text TEXT,
    best_ask_text TEXT,
    bid_size_text TEXT,
    ask_size_text TEXT,
    midpoint_text TEXT,
    spread_text TEXT,
    tick_size_text TEXT,
    min_order_size_text TEXT,
    bid_level_count INTEGER CHECK (bid_level_count >= 0),
    ask_level_count INTEGER CHECK (ask_level_count >= 0),
    PRIMARY KEY (slot_id, outcome),
    CHECK (status = 'VALID' OR (best_bid_text IS NULL AND best_ask_text IS NULL
        AND bid_size_text IS NULL AND ask_size_text IS NULL AND midpoint_text IS NULL AND spread_text IS NULL))
);
CREATE INDEX book_observations_token_source ON book_observations(expected_token_id, source_us);

CREATE TABLE reference_feeds (
    feed_id TEXT PRIMARY KEY NOT NULL CHECK (feed_id = '0x0002ee6757e8822c00d273bc340fc24c9cafe123a4ff2ea1dbdb31944bc7d95f'),
    name TEXT NOT NULL CHECK (name = 'BTC/USD-Streams-TWAP-60s-mainnet-production'),
    environment TEXT NOT NULL CHECK (environment = 'mainnet'),
    source_url TEXT NOT NULL,
    catalog_metadata_json TEXT NOT NULL,
    lookback_seconds INTEGER NOT NULL CHECK (lookback_seconds = 60),
    report_schema_version INTEGER NOT NULL CHECK (report_schema_version = 2),
    price_scale INTEGER,
    decoder_revision TEXT,
    verified_us INTEGER,
    verification_evidence_json TEXT
);

CREATE TABLE reference_reports (
    report_id INTEGER PRIMARY KEY,
    feed_id TEXT NOT NULL REFERENCES reference_feeds(feed_id),
    report_hash TEXT NOT NULL,
    first_fetch_id INTEGER NOT NULL REFERENCES reference_fetches(fetch_id),
    valid_from_seconds INTEGER,
    observations_seconds INTEGER,
    expires_at_seconds INTEGER,
    decoded_price_text TEXT,
    decoder_revision TEXT,
    decode_status TEXT NOT NULL CHECK (decode_status IN ('DECODED', 'INVALID', 'UNVERIFIED')),
    verification_status TEXT NOT NULL,
    first_received_wall_us INTEGER NOT NULL,
    first_received_server_us INTEGER,
    raw_report_z BLOB NOT NULL,
    raw_size_bytes INTEGER NOT NULL CHECK (raw_size_bytes BETWEEN 0 AND 10485760),
    codec_version INTEGER NOT NULL CHECK (codec_version = 1),
    UNIQUE (feed_id, report_hash)
);
CREATE INDEX reference_reports_feed_observed ON reference_reports(feed_id, observations_seconds);

CREATE TABLE reference_fetches (
    fetch_id INTEGER PRIMARY KEY,
    request_id INTEGER NOT NULL UNIQUE REFERENCES request_attempts(request_id),
    feed_id TEXT NOT NULL REFERENCES reference_feeds(feed_id),
    purpose TEXT NOT NULL CHECK (purpose IN ('SLOT_CURRENT', 'OPENING', 'CLOSING', 'REPAIR')),
    origin TEXT NOT NULL CHECK (origin IN ('LIVE_LATEST', 'HISTORICAL_LOOKUP')),
    target_us INTEGER,
    status TEXT NOT NULL,
    error_code TEXT,
    response_raw_z BLOB,
    raw_size_bytes INTEGER CHECK (raw_size_bytes BETWEEN 0 AND 10485760),
    codec_version INTEGER CHECK (codec_version = 1),
    CHECK ((response_raw_z IS NULL AND raw_size_bytes IS NULL AND codec_version IS NULL)
        OR (response_raw_z IS NOT NULL AND raw_size_bytes IS NOT NULL AND codec_version IS NOT NULL))
);

CREATE TABLE reference_associations (
    association_id INTEGER PRIMARY KEY,
    market_id TEXT NOT NULL REFERENCES markets(market_id),
    feed_id TEXT NOT NULL REFERENCES reference_feeds(feed_id),
    role TEXT NOT NULL CHECK (role IN ('SLOT_CURRENT', 'OPENING', 'CLOSING')),
    slot_id INTEGER REFERENCES sample_slots(slot_id),
    target_us INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('DISABLED', 'PENDING', 'AVAILABLE', 'UNAVAILABLE', 'AMBIGUOUS', 'CONFLICT')),
    reason TEXT,
    fetch_id INTEGER REFERENCES reference_fetches(fetch_id),
    report_id INTEGER REFERENCES reference_reports(report_id),
    origin TEXT NOT NULL CHECK (origin IN ('LIVE_LATEST', 'HISTORICAL_LOOKUP', 'NOT_FETCHED')),
    learned_wall_us INTEGER NOT NULL,
    learned_server_us INTEGER,
    available_live INTEGER NOT NULL CHECK (available_live IN (0, 1)),
    selection_policy_revision TEXT,
    next_due_us INTEGER,
    attempt_count INTEGER NOT NULL CHECK (attempt_count >= 0),
    CHECK ((role = 'SLOT_CURRENT' AND slot_id IS NOT NULL) OR (role IN ('OPENING', 'CLOSING') AND slot_id IS NULL)),
    CHECK (origin <> 'HISTORICAL_LOOKUP' OR available_live = 0),
    CHECK (origin <> 'NOT_FETCHED' OR (available_live = 0 AND fetch_id IS NULL AND report_id IS NULL)),
    CHECK (status <> 'DISABLED' OR (origin = 'NOT_FETCHED' AND report_id IS NULL)),
    CHECK (available_live = 0 OR (origin = 'LIVE_LATEST' AND status = 'AVAILABLE' AND fetch_id IS NOT NULL AND report_id IS NOT NULL))
);
CREATE UNIQUE INDEX reference_associations_slot_learned ON reference_associations(slot_id, learned_wall_us) WHERE role = 'SLOT_CURRENT';
CREATE UNIQUE INDEX reference_associations_boundary_learned ON reference_associations(market_id, role, learned_wall_us) WHERE role IN ('OPENING', 'CLOSING');

CREATE TABLE resolution_observations (
    resolution_id INTEGER PRIMARY KEY,
    market_id TEXT NOT NULL REFERENCES markets(market_id),
    request_id INTEGER NOT NULL REFERENCES request_attempts(request_id),
    condition_id TEXT NOT NULL,
    status_raw TEXT,
    payouts_raw_json TEXT,
    provenance_json TEXT NOT NULL,
    reported_resolved_us INTEGER,
    reported_time_raw TEXT,
    learned_wall_us INTEGER NOT NULL,
    learned_server_us INTEGER,
    mapped_outcome TEXT CHECK (mapped_outcome IN ('UP', 'DOWN')),
    classification TEXT NOT NULL,
    response_hash TEXT NOT NULL,
    raw_resolution_z BLOB NOT NULL,
    raw_size_bytes INTEGER NOT NULL CHECK (raw_size_bytes BETWEEN 0 AND 10485760),
    codec_version INTEGER NOT NULL CHECK (codec_version = 1),
    UNIQUE (request_id, market_id)
);
CREATE INDEX resolution_observations_market_learned ON resolution_observations(market_id, learned_wall_us);

CREATE TABLE recorder_events (
    event_id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES recording_runs(run_id),
    market_id TEXT REFERENCES markets(market_id),
    kind TEXT NOT NULL CHECK (kind IN ('STARTUP', 'SHUTDOWN', 'RESTART', 'DISCOVERY_GAP', 'CLOCK_PAUSE', 'IDENTITY_CONFLICT', 'STORAGE_FAILURE')),
    started_us INTEGER NOT NULL,
    ended_us INTEGER,
    detail_json TEXT NOT NULL
);
CREATE INDEX recorder_events_kind_started ON recorder_events(kind, started_us);

CREATE TABLE storage_checkpoints (
    checkpoint_id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES recording_runs(run_id),
    observed_wall_us INTEGER NOT NULL,
    db_file_bytes INTEGER NOT NULL CHECK (db_file_bytes >= 0),
    page_size INTEGER NOT NULL CHECK (page_size >= 0),
    page_count INTEGER NOT NULL CHECK (page_count >= 0),
    freelist_count INTEGER NOT NULL CHECK (freelist_count >= 0),
    journal_bytes INTEGER NOT NULL CHECK (journal_bytes >= 0),
    committed_slot_count INTEGER NOT NULL CHECK (committed_slot_count >= 0),
    reference_report_count INTEGER NOT NULL CHECK (reference_report_count >= 0),
    book_raw_bytes INTEGER NOT NULL CHECK (book_raw_bytes >= 0),
    book_compressed_bytes INTEGER NOT NULL CHECK (book_compressed_bytes >= 0),
    reference_raw_bytes INTEGER NOT NULL CHECK (reference_raw_bytes >= 0),
    reference_compressed_bytes INTEGER NOT NULL CHECK (reference_compressed_bytes >= 0)
);
CREATE INDEX storage_checkpoints_observed ON storage_checkpoints(observed_wall_us);

-- APPROXIMATE MODE ADDITIONS (v2 only);

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
    price_text TEXT NOT NULL,
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
    UNIQUE (run_id, provider, receipt_wall_us)
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
    diagnostics_json TEXT
);
