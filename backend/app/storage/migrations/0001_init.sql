-- 0001_init.sql — business database schema (ImplementationPlan §6.1).
-- All timestamps are ISO-8601 UTC strings. JSON payloads are stored as TEXT.

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------- tasks ----
CREATE TABLE IF NOT EXISTS tasks (
    task_id            TEXT PRIMARY KEY,
    root_task_id       TEXT NOT NULL REFERENCES tasks(task_id),
    parent_task_id     TEXT NULL REFERENCES tasks(task_id),
    task_level         TEXT NOT NULL CHECK (task_level IN ('root', 'child')),
    agent_id           TEXT NULL,
    agent_version      TEXT NULL,
    task_kind          TEXT NULL,
    goal               TEXT NOT NULL,
    depends_on         TEXT NOT NULL DEFAULT '[]',
    status             TEXT NOT NULL,
    passed             INTEGER NULL CHECK (passed IN (0, 1) OR passed IS NULL),
    skip_reason        TEXT NULL,
    workflow_version   TEXT NOT NULL,
    revision           INTEGER NOT NULL DEFAULT 0,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_root ON tasks(root_task_id);
CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks(parent_task_id);
CREATE INDEX IF NOT EXISTS idx_tasks_kind ON tasks(root_task_id, task_kind);

-- --------------------------------------------------------- task_controls ---
-- Mutable per-root runtime control: lease, fencing, run segment, step budget.
-- Kept separate from tasks so recovery mutexes are DB-backed (§6.1, §11.1).
CREATE TABLE IF NOT EXISTS task_controls (
    root_task_id        TEXT PRIMARY KEY REFERENCES tasks(task_id),
    lease_owner         TEXT NULL,
    lease_expires_at    TEXT NULL,
    heartbeat_at        TEXT NULL,
    fencing_token       INTEGER NOT NULL DEFAULT 0,
    run_segment_id      TEXT NOT NULL,
    graph_steps_granted INTEGER NOT NULL DEFAULT 0,
    graph_steps_used    INTEGER NOT NULL DEFAULT 0,
    current_decision_id TEXT NULL,
    parent_corrections  INTEGER NOT NULL DEFAULT 0,
    required_action     TEXT NULL,
    updated_at          TEXT NOT NULL
);

-- ---------------------------------------------------- dispatch_batches -----
CREATE TABLE IF NOT EXISTS dispatch_batches (
    dispatch_batch_id   TEXT PRIMARY KEY,
    root_task_id        TEXT NOT NULL REFERENCES tasks(task_id),
    expected_attempt_ids TEXT NOT NULL DEFAULT '[]',
    received_attempt_ids TEXT NOT NULL DEFAULT '[]',
    outcome_origins     TEXT NOT NULL DEFAULT '{}',
    status              TEXT NOT NULL DEFAULT 'open',
    consumed            INTEGER NOT NULL DEFAULT 0 CHECK (consumed IN (0, 1)),
    created_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_batches_root ON dispatch_batches(root_task_id);

-- ------------------------------------------------------------- attempts -----
CREATE TABLE IF NOT EXISTS attempts (
    attempt_id         TEXT PRIMARY KEY,
    task_id            TEXT NOT NULL REFERENCES tasks(task_id),
    root_task_id       TEXT NOT NULL REFERENCES tasks(task_id),
    dispatch_batch_id  TEXT NULL REFERENCES dispatch_batches(dispatch_batch_id),
    attempt_no         INTEGER NOT NULL CHECK (attempt_no >= 1),
    source_version     TEXT NOT NULL,
    contract_version   TEXT NULL,
    input_refs         TEXT NOT NULL DEFAULT '{}',
    constraints        TEXT NOT NULL DEFAULT '{}',
    retry_reason       TEXT NULL,
    status             TEXT NOT NULL,
    error              TEXT NULL,
    started_at         TEXT NULL,
    finished_at        TEXT NULL,
    created_at         TEXT NOT NULL,
    UNIQUE (task_id, attempt_no)
);
CREATE INDEX IF NOT EXISTS idx_attempts_root ON attempts(root_task_id);
CREATE INDEX IF NOT EXISTS idx_attempts_batch ON attempts(dispatch_batch_id);

-- --------------------------------------------------------- task_results -----
CREATE TABLE IF NOT EXISTS task_results (
    result_id          TEXT PRIMARY KEY,
    attempt_id         TEXT NOT NULL UNIQUE REFERENCES attempts(attempt_id),
    root_task_id       TEXT NOT NULL,
    task_id            TEXT NOT NULL,
    dispatch_batch_id  TEXT NOT NULL,
    agent_id           TEXT NOT NULL,
    agent_version      TEXT NOT NULL,
    task_kind          TEXT NOT NULL,
    source_version     TEXT NOT NULL,
    contract_version   TEXT NOT NULL,
    status             TEXT NOT NULL,
    passed             INTEGER NULL CHECK (passed IN (0, 1) OR passed IS NULL),
    summary            TEXT NOT NULL,
    result_refs        TEXT NOT NULL DEFAULT '{}',
    evidence_refs      TEXT NOT NULL DEFAULT '[]',
    error              TEXT NULL,
    content_digest     TEXT NOT NULL,
    started_at         TEXT NOT NULL,
    finished_at        TEXT NOT NULL,
    created_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_results_root ON task_results(root_task_id);

-- ------------------------------------------------------ attempt_terminals ---
-- One terminal outcome per attempt: agent report or controller failure (§10.1).
CREATE TABLE IF NOT EXISTS attempt_terminals (
    attempt_id         TEXT PRIMARY KEY REFERENCES attempts(attempt_id),
    dispatch_batch_id  TEXT NULL,
    origin             TEXT NOT NULL CHECK (origin IN ('agent', 'controller')),
    outcome            TEXT NOT NULL CHECK (outcome IN ('completed', 'failed')),
    result_id          TEXT NULL,
    error_ref          TEXT NULL,
    fencing_token      INTEGER NOT NULL DEFAULT 0,
    operation_key      TEXT NOT NULL UNIQUE,
    created_at         TEXT NOT NULL
);

-- -------------------------------------------------------- budget_ledger ----
CREATE TABLE IF NOT EXISTS budget_ledger (
    entry_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    root_task_id    TEXT NOT NULL REFERENCES tasks(task_id),
    budget_kind     TEXT NOT NULL,
    scope_key       TEXT NOT NULL,
    operation_key   TEXT NOT NULL,
    delta           INTEGER NOT NULL CHECK (delta <> 0),
    reason          TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    UNIQUE (operation_key, budget_kind)
);
CREATE INDEX IF NOT EXISTS idx_ledger_root ON budget_ledger(root_task_id, budget_kind);

-- ------------------------------------------------------------ artifacts ----
CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id        TEXT PRIMARY KEY,
    root_task_id       TEXT NOT NULL,
    producer_attempt_id TEXT NULL,
    artifact_type      TEXT NOT NULL,
    source_version     TEXT NULL,
    storage_ref        TEXT NOT NULL,
    hash               TEXT NOT NULL,
    size               INTEGER NOT NULL,
    metadata           TEXT NOT NULL DEFAULT '{}',
    created_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_artifacts_root ON artifacts(root_task_id, artifact_type);
CREATE INDEX IF NOT EXISTS idx_artifacts_source ON artifacts(root_task_id, source_version);

-- ---------------------------------------------------- patch_applications ---
CREATE TABLE IF NOT EXISTS patch_applications (
    application_id    TEXT PRIMARY KEY,
    root_task_id      TEXT NOT NULL,
    patch_id          TEXT NOT NULL,
    repair_attempt_id TEXT NOT NULL,
    base_version      TEXT NOT NULL,
    result_version    TEXT NULL,
    status            TEXT NOT NULL,
    idempotency_key   TEXT NOT NULL UNIQUE,
    error             TEXT NULL,
    prepared_at       TEXT NOT NULL,
    committed_at      TEXT NULL
);
CREATE INDEX IF NOT EXISTS idx_patch_apps_root ON patch_applications(root_task_id);

-- ----------------------------------------------------- execution_events ----
CREATE TABLE IF NOT EXISTS execution_events (
    event_id          TEXT PRIMARY KEY,
    root_task_id      TEXT NOT NULL,
    task_id           TEXT NULL,
    attempt_id        TEXT NULL,
    dispatch_batch_id TEXT NULL,
    source_version    TEXT NULL,
    sequence          INTEGER NOT NULL,
    timestamp         TEXT NOT NULL,
    actor_id          TEXT NOT NULL,
    event_type        TEXT NOT NULL,
    payload           TEXT NOT NULL DEFAULT '{}',
    artifact_refs     TEXT NOT NULL DEFAULT '[]',
    duration_ms       INTEGER NULL,
    UNIQUE (root_task_id, sequence)
);
CREATE INDEX IF NOT EXISTS idx_events_root_seq ON execution_events(root_task_id, sequence);

-- ----------------------------------------------------- workflow_configs ---
CREATE TABLE IF NOT EXISTS workflow_configs (
    workflow_version  TEXT PRIMARY KEY,
    schema_version    TEXT NOT NULL,
    check_mode        TEXT NOT NULL,
    semantic_hash     TEXT NOT NULL,
    content_digest    TEXT NOT NULL,
    config_json       TEXT NOT NULL,
    created_at        TEXT NOT NULL
);

-- ------------------------------------------------- idempotency_records -----
CREATE TABLE IF NOT EXISTS idempotency_records (
    operation_key       TEXT PRIMARY KEY,
    operation_kind      TEXT NOT NULL,
    root_task_id        TEXT NULL,
    request_fingerprint TEXT NOT NULL,
    response_ref        TEXT NULL,
    created_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_idem_root ON idempotency_records(root_task_id);

-- ------------------------------------------------------------- uploads -----
CREATE TABLE IF NOT EXISTS uploads (
    source_id       TEXT PRIMARY KEY,
    content_digest  TEXT NOT NULL,
    files           TEXT NOT NULL,
    total_bytes     INTEGER NOT NULL,
    root_task_id    TEXT NULL,
    created_at      TEXT NOT NULL
);

-- --------------------------------------------- acceptance_contracts --------
CREATE TABLE IF NOT EXISTS acceptance_contracts (
    contract_version TEXT NOT NULL,
    root_task_id     TEXT NOT NULL,
    content_hash     TEXT NOT NULL,
    artifact_id      TEXT NOT NULL,
    supersedes       TEXT NULL,
    append_reason    TEXT NULL,
    created_at       TEXT NOT NULL,
    -- a version is only unique inside one root task: every task starts at contract-1
    PRIMARY KEY (root_task_id, contract_version)
);
CREATE INDEX IF NOT EXISTS idx_contracts_root ON acceptance_contracts(root_task_id);

-- -------------------------------------------------------- check_results ----
CREATE TABLE IF NOT EXISTS check_results (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    check_id           TEXT NOT NULL,
    root_task_id       TEXT NOT NULL REFERENCES tasks(task_id),
    contract_version   TEXT NOT NULL,
    source_version     TEXT NOT NULL,
    producer_attempt_id TEXT NOT NULL,
    method             TEXT NULL,
    status             TEXT NOT NULL,
    evidence_refs      TEXT NOT NULL DEFAULT '[]',
    reason             TEXT NULL,
    test_count         INTEGER NULL,
    passed_count       INTEGER NULL,
    failed_count       INTEGER NULL,
    skipped_count      INTEGER NULL,
    test_suite_hash    TEXT NULL,
    executed           INTEGER NOT NULL DEFAULT 1 CHECK (executed IN (0, 1)),
    created_at         TEXT NOT NULL,
    UNIQUE (check_id, source_version, producer_attempt_id)
);
CREATE INDEX IF NOT EXISTS idx_checks_lookup ON check_results(source_version, check_id);

-- ------------------------------------------------------------- findings ----
CREATE TABLE IF NOT EXISTS findings (
    finding_id           TEXT NOT NULL,
    root_task_id         TEXT NOT NULL,
    source_version       TEXT NOT NULL,
    producer_attempt_id  TEXT NOT NULL,
    goal_ref             TEXT NULL,
    required_for_goal    INTEGER NOT NULL DEFAULT 0 CHECK (required_for_goal IN (0, 1)),
    check_id             TEXT NULL,
    file_path            TEXT NOT NULL,
    line                 INTEGER NULL,
    symbol               TEXT NULL,
    rule                 TEXT NOT NULL,
    severity             TEXT NOT NULL,
    message              TEXT NOT NULL,
    evidence_refs        TEXT NOT NULL DEFAULT '[]',
    status               TEXT NOT NULL,
    resolution_evidence_refs TEXT NOT NULL DEFAULT '[]',
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL,
    PRIMARY KEY (finding_id, source_version)
);
CREATE INDEX IF NOT EXISTS idx_findings_root ON findings(root_task_id, status);

-- --------------------------------------------------------- verifications ---
CREATE TABLE IF NOT EXISTS verifications (
    verification_id    TEXT PRIMARY KEY,
    root_task_id       TEXT NOT NULL,
    producer_attempt_id TEXT NOT NULL,
    source_version     TEXT NOT NULL,
    contract_version   TEXT NOT NULL,
    target_finding_ids TEXT NOT NULL DEFAULT '[]',
    coverage           TEXT NOT NULL DEFAULT '[]',
    not_run            TEXT NOT NULL DEFAULT '[]',
    passed             INTEGER NULL CHECK (passed IN (0, 1) OR passed IS NULL),
    evidence_refs      TEXT NOT NULL DEFAULT '[]',
    notes              TEXT NULL,
    artifact_id        TEXT NULL,
    created_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_verifications_root ON verifications(root_task_id, source_version);

-- -------------------------------------------------------------- reports ----
CREATE TABLE IF NOT EXISTS reports (
    report_id      TEXT PRIMARY KEY,
    root_task_id   TEXT NOT NULL REFERENCES tasks(task_id),
    kind           TEXT NOT NULL,
    artifact_id    TEXT NULL,
    final_status   TEXT NOT NULL,
    passed         INTEGER NULL CHECK (passed IN (0, 1) OR passed IS NULL),
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reports_root ON reports(root_task_id, kind);
