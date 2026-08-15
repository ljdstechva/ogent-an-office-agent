"""Versioned SQLite schema for durable Ogent state."""

from __future__ import annotations


SCHEMA_VERSION = 7

MIGRATIONS: tuple[tuple[int, str], ...] = (
    (
        1,
        """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE blobs (
    id TEXT PRIMARY KEY,
    sha256 TEXT NOT NULL UNIQUE,
    byte_size INTEGER NOT NULL CHECK (byte_size >= 0),
    media_type TEXT NOT NULL,
    relative_path TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE TABLE documents (
    id TEXT PRIMARY KEY,
    source_path TEXT,
    active_path TEXT NOT NULL,
    mode TEXT NOT NULL,
    format TEXT NOT NULL,
    canonical_path_key TEXT NOT NULL UNIQUE,
    backup_id TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE workspaces (
    id TEXT PRIMARY KEY,
    document_id TEXT REFERENCES documents(id),
    conversation_generation INTEGER NOT NULL DEFAULT 1
        CHECK (conversation_generation > 0),
    created_at TEXT NOT NULL,
    last_active_at TEXT NOT NULL,
    status TEXT NOT NULL,
    selected_provider TEXT,
    selected_model TEXT,
    selected_effort TEXT
);

CREATE TABLE document_revisions (
    id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    revision_number INTEGER NOT NULL CHECK (revision_number >= 0),
    package_sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    index_status TEXT NOT NULL,
    index_version INTEGER NOT NULL DEFAULT 1,
    UNIQUE(document_id, revision_number)
);

CREATE TABLE document_nodes (
    id TEXT PRIMARY KEY,
    revision_id TEXT NOT NULL
        REFERENCES document_revisions(id) ON DELETE CASCADE,
    stable_path TEXT NOT NULL,
    parent_path TEXT,
    kind TEXT NOT NULL,
    title TEXT,
    text_blob_id TEXT REFERENCES blobs(id),
    metadata_json TEXT NOT NULL DEFAULT '{}',
    content_sha256 TEXT NOT NULL,
    sheet_name TEXT,
    slide_number INTEGER,
    page_number INTEGER,
    ordinal INTEGER NOT NULL DEFAULT 0,
    UNIQUE(revision_id, stable_path)
);

CREATE TABLE document_edges (
    id TEXT PRIMARY KEY,
    revision_id TEXT NOT NULL
        REFERENCES document_revisions(id) ON DELETE CASCADE,
    source_node_id TEXT NOT NULL
        REFERENCES document_nodes(id) ON DELETE CASCADE,
    target_node_id TEXT NOT NULL
        REFERENCES document_nodes(id) ON DELETE CASCADE,
    edge_type TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE document_chunks (
    id TEXT PRIMARY KEY,
    revision_id TEXT NOT NULL
        REFERENCES document_revisions(id) ON DELETE CASCADE,
    node_id TEXT NOT NULL REFERENCES document_nodes(id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL CHECK (chunk_index >= 0),
    text TEXT NOT NULL,
    token_count INTEGER NOT NULL CHECK (token_count >= 0),
    content_sha256 TEXT NOT NULL,
    UNIQUE(node_id, chunk_index)
);

CREATE VIRTUAL TABLE document_chunks_fts USING fts5(
    chunk_id UNINDEXED,
    revision_id UNINDEXED,
    node_id UNINDEXED,
    text,
    title,
    stable_path,
    sheet_name,
    slide_number UNINDEXED,
    tokenize = 'unicode61'
);

CREATE TRIGGER document_chunks_fts_insert
AFTER INSERT ON document_chunks BEGIN
    INSERT INTO document_chunks_fts(
        chunk_id, revision_id, node_id, text, title,
        stable_path, sheet_name, slide_number
    )
    SELECT
        new.id, new.revision_id, new.node_id, new.text, node.title,
        node.stable_path, node.sheet_name, node.slide_number
    FROM document_nodes AS node
    WHERE node.id = new.node_id;
END;

CREATE TRIGGER document_chunks_fts_delete
AFTER DELETE ON document_chunks BEGIN
    DELETE FROM document_chunks_fts WHERE chunk_id = old.id;
END;

CREATE TRIGGER document_chunks_fts_update
AFTER UPDATE ON document_chunks BEGIN
    DELETE FROM document_chunks_fts WHERE chunk_id = old.id;
    INSERT INTO document_chunks_fts(
        chunk_id, revision_id, node_id, text, title,
        stable_path, sheet_name, slide_number
    )
    SELECT
        new.id, new.revision_id, new.node_id, new.text, node.title,
        node.stable_path, node.sheet_name, node.slide_number
    FROM document_nodes AS node
    WHERE node.id = new.node_id;
END;

CREATE TABLE turns (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL CHECK (sequence > 0),
    role TEXT NOT NULL,
    raw_content_blob_id TEXT NOT NULL REFERENCES blobs(id),
    display_excerpt TEXT NOT NULL,
    character_count INTEGER NOT NULL CHECK (character_count >= 0),
    provider TEXT,
    model TEXT,
    effort TEXT,
    created_at TEXT NOT NULL,
    run_outcome TEXT,
    UNIQUE(workspace_id, sequence)
);

CREATE TABLE runs (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    request_turn_id TEXT NOT NULL REFERENCES turns(id),
    state TEXT NOT NULL,
    mode TEXT NOT NULL,
    scope TEXT NOT NULL,
    plan_json TEXT NOT NULL DEFAULT '{}',
    dependencies_json TEXT NOT NULL DEFAULT '[]',
    expected_mutations_json TEXT NOT NULL DEFAULT '[]',
    coverage_target_json TEXT NOT NULL DEFAULT '{}',
    cancellation_requested INTEGER NOT NULL DEFAULT 0,
    verification_json TEXT NOT NULL DEFAULT '{}',
    timing_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE run_steps (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL,
    state TEXT NOT NULL,
    description TEXT NOT NULL,
    target_node_ids_json TEXT NOT NULL DEFAULT '[]',
    mutates INTEGER NOT NULL DEFAULT 0,
    tool TEXT,
    verification_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(run_id, sequence)
);

CREATE TABLE run_events (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    run_id TEXT REFERENCES runs(id) ON DELETE SET NULL,
    sequence INTEGER NOT NULL CHECK (sequence > 0),
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(workspace_id, sequence)
);

CREATE TABLE tool_receipts (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    operation TEXT NOT NULL,
    skill_name TEXT,
    skill_sha256 TEXT,
    document_revision INTEGER,
    package_sha256 TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    exit_status INTEGER,
    mutation_category TEXT,
    result_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE changesets (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    pre_revision_sha256 TEXT NOT NULL,
    post_revision_sha256 TEXT NOT NULL,
    affected_paths_json TEXT NOT NULL,
    assertions_json TEXT NOT NULL,
    rollback_blob_id TEXT REFERENCES blobs(id),
    created_at TEXT NOT NULL
);

CREATE TABLE attachments (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    original_name TEXT NOT NULL,
    blob_id TEXT NOT NULL REFERENCES blobs(id),
    detected_type TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    forgotten_at TEXT
);

CREATE TABLE recent_documents (
    canonical_path_key TEXT PRIMARY KEY,
    source_path TEXT NOT NULL,
    last_opened_at TEXT NOT NULL
);

CREATE TABLE legacy_imports (
    source_key TEXT PRIMARY KEY,
    imported_at TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX turns_workspace_sequence
    ON turns(workspace_id, sequence);
CREATE INDEX run_events_workspace_sequence
    ON run_events(workspace_id, sequence);
CREATE INDEX runs_workspace_updated
    ON runs(workspace_id, updated_at);
CREATE INDEX revisions_document_number
    ON document_revisions(document_id, revision_number);
CREATE INDEX nodes_revision_parent
    ON document_nodes(revision_id, parent_path, ordinal);
CREATE INDEX chunks_revision_node
    ON document_chunks(revision_id, node_id, chunk_index);
""",
    ),
    (
        2,
        """
ALTER TABLE turns
    ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}';

CREATE TABLE recovery_backups (
    backup_id TEXT PRIMARY KEY,
    backup_file TEXT NOT NULL,
    source_path TEXT NOT NULL,
    source_name TEXT NOT NULL,
    extension TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    byte_size INTEGER NOT NULL CHECK (byte_size >= 0),
    application_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    pending_delete INTEGER NOT NULL DEFAULT 0,
    delete_error TEXT,
    manifest_path TEXT NOT NULL UNIQUE,
    imported_at TEXT NOT NULL
);

CREATE INDEX recovery_backups_created
    ON recovery_backups(created_at);
CREATE INDEX recent_documents_last_opened
    ON recent_documents(last_opened_at DESC);
""",
    ),
    (
        3,
        """
CREATE TABLE skill_policies (
    id TEXT PRIMARY KEY,
    officecli_version TEXT NOT NULL,
    skill_name TEXT NOT NULL,
    policy_blob_id TEXT NOT NULL REFERENCES blobs(id),
    policy_sha256 TEXT NOT NULL,
    loaded_at TEXT NOT NULL,
    UNIQUE(officecli_version, skill_name)
);

CREATE TABLE capability_receipts (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    skill_name TEXT NOT NULL,
    skill_sha256 TEXT NOT NULL,
    policy_blob_id TEXT NOT NULL REFERENCES blobs(id),
    officecli_version TEXT NOT NULL,
    document_path_key TEXT NOT NULL,
    document_revision INTEGER NOT NULL CHECK (document_revision >= 0),
    package_sha256 TEXT NOT NULL,
    probe_operation TEXT NOT NULL,
    probe_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(run_id)
);

ALTER TABLE tool_receipts
    ADD COLUMN arguments_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE tool_receipts
    ADD COLUMN output_sha256 TEXT;
ALTER TABLE tool_receipts
    ADD COLUMN output_bytes INTEGER;

CREATE INDEX capability_receipts_workspace
    ON capability_receipts(workspace_id, created_at);
CREATE INDEX tool_receipts_run_sequence
    ON tool_receipts(run_id, started_at);
""",
    ),
    (
        4,
        """
ALTER TABLE documents
    ADD COLUMN current_revision_id TEXT
        REFERENCES document_revisions(id) ON DELETE SET NULL;

ALTER TABLE document_revisions
    ADD COLUMN parent_revision_id TEXT
        REFERENCES document_revisions(id) ON DELETE SET NULL;
ALTER TABLE document_revisions
    ADD COLUMN quick_manifest_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE document_revisions
    ADD COLUMN manifest_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE document_revisions
    ADD COLUMN indexed_at TEXT;
ALTER TABLE document_revisions
    ADD COLUMN error_code TEXT;
ALTER TABLE document_revisions
    ADD COLUMN source_size INTEGER NOT NULL DEFAULT 0
        CHECK (source_size >= 0);
ALTER TABLE document_revisions
    ADD COLUMN source_mtime_ns INTEGER NOT NULL DEFAULT 0
        CHECK (source_mtime_ns >= 0);

ALTER TABLE document_nodes
    ADD COLUMN origin_node_id TEXT
        REFERENCES document_nodes(id) ON DELETE SET NULL;
ALTER TABLE document_nodes
    ADD COLUMN native_key TEXT;
ALTER TABLE document_nodes
    ADD COLUMN locator_stability TEXT NOT NULL DEFAULT 'revision_scoped';
ALTER TABLE document_nodes
    ADD COLUMN lineage_key TEXT;
ALTER TABLE document_nodes
    ADD COLUMN source_paths_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE document_nodes
    ADD COLUMN locator_namespace TEXT NOT NULL DEFAULT 'internal';
ALTER TABLE document_nodes
    ADD COLUMN locator_resolvable INTEGER NOT NULL DEFAULT 0;

CREATE TABLE index_jobs (
    revision_id TEXT PRIMARY KEY
        REFERENCES document_revisions(id) ON DELETE CASCADE,
    attempt_id TEXT NOT NULL,
    attempt_generation INTEGER NOT NULL DEFAULT 1
        CHECK (attempt_generation > 0),
    status TEXT NOT NULL,
    progress REAL NOT NULL DEFAULT 0.0
        CHECK (progress >= 0.0 AND progress <= 1.0),
    indexed_nodes INTEGER NOT NULL DEFAULT 0
        CHECK (indexed_nodes >= 0),
    total_estimate INTEGER NOT NULL DEFAULT 0
        CHECK (total_estimate >= 0),
    started_at TEXT,
    updated_at TEXT NOT NULL,
    error_code TEXT
);

CREATE TABLE coverage_ledgers (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    revision_id TEXT NOT NULL
        REFERENCES document_revisions(id) ON DELETE CASCADE,
    required_paths_json TEXT NOT NULL,
    reviewed_paths_json TEXT NOT NULL DEFAULT '{}',
    unsupported_json TEXT NOT NULL DEFAULT '[]',
    visuals_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(run_id)
);

CREATE TABLE visual_regions (
    id TEXT PRIMARY KEY,
    revision_id TEXT NOT NULL
        REFERENCES document_revisions(id) ON DELETE CASCADE,
    stable_path TEXT NOT NULL,
    renderer_profile TEXT NOT NULL,
    region_key TEXT NOT NULL,
    blob_id TEXT NOT NULL REFERENCES blobs(id),
    media_type TEXT NOT NULL,
    byte_size INTEGER NOT NULL CHECK (byte_size >= 0),
    created_at TEXT NOT NULL,
    UNIQUE(revision_id, stable_path, renderer_profile, region_key)
);

CREATE TRIGGER documents_current_revision_same_document_insert
BEFORE INSERT ON documents
WHEN new.current_revision_id IS NOT NULL
     AND NOT EXISTS (
        SELECT 1 FROM document_revisions
        WHERE id = new.current_revision_id
          AND document_id = new.id
     )
BEGIN
    SELECT RAISE(ABORT, 'current revision belongs to another document');
END;

CREATE TRIGGER documents_current_revision_same_document_update
BEFORE UPDATE OF current_revision_id ON documents
WHEN new.current_revision_id IS NOT NULL
     AND NOT EXISTS (
        SELECT 1 FROM document_revisions
        WHERE id = new.current_revision_id
          AND document_id = new.id
     )
BEGIN
    SELECT RAISE(ABORT, 'current revision belongs to another document');
END;

CREATE TRIGGER revisions_parent_same_document_insert
BEFORE INSERT ON document_revisions
WHEN new.parent_revision_id IS NOT NULL
     AND NOT EXISTS (
        SELECT 1 FROM document_revisions
        WHERE id = new.parent_revision_id
          AND document_id = new.document_id
     )
BEGIN
    SELECT RAISE(ABORT, 'parent revision belongs to another document');
END;

CREATE TRIGGER revisions_parent_same_document_update
BEFORE UPDATE OF parent_revision_id, document_id ON document_revisions
WHEN new.parent_revision_id IS NOT NULL
     AND NOT EXISTS (
        SELECT 1 FROM document_revisions
        WHERE id = new.parent_revision_id
          AND document_id = new.document_id
     )
BEGIN
    SELECT RAISE(ABORT, 'parent revision belongs to another document');
END;

CREATE TRIGGER edges_revision_ownership_insert
BEFORE INSERT ON document_edges
WHEN NOT EXISTS (
        SELECT 1 FROM document_nodes
        WHERE id = new.source_node_id
          AND revision_id = new.revision_id
     )
     OR NOT EXISTS (
        SELECT 1 FROM document_nodes
        WHERE id = new.target_node_id
          AND revision_id = new.revision_id
     )
BEGIN
    SELECT RAISE(ABORT, 'edge endpoint belongs to another revision');
END;

CREATE TRIGGER edges_revision_ownership_update
BEFORE UPDATE OF revision_id, source_node_id, target_node_id
ON document_edges
WHEN NOT EXISTS (
        SELECT 1 FROM document_nodes
        WHERE id = new.source_node_id
          AND revision_id = new.revision_id
     )
     OR NOT EXISTS (
        SELECT 1 FROM document_nodes
        WHERE id = new.target_node_id
          AND revision_id = new.revision_id
     )
BEGIN
    SELECT RAISE(ABORT, 'edge endpoint belongs to another revision');
END;

CREATE INDEX revisions_package_sha
    ON document_revisions(document_id, package_sha256);
CREATE INDEX nodes_revision_kind
    ON document_nodes(revision_id, kind, ordinal);
CREATE INDEX nodes_revision_content
    ON document_nodes(revision_id, content_sha256);
CREATE INDEX nodes_revision_lineage
    ON document_nodes(revision_id, lineage_key);
CREATE UNIQUE INDEX edges_revision_unique
    ON document_edges(
        revision_id, source_node_id, target_node_id, edge_type
    );
CREATE INDEX coverage_revision
    ON coverage_ledgers(revision_id, updated_at);
CREATE INDEX visual_regions_revision
    ON visual_regions(revision_id, stable_path);
""",
    ),
    (
        5,
        """
ALTER TABLE run_steps
    ADD COLUMN dependencies_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE run_steps
    ADD COLUMN logical_id TEXT NOT NULL DEFAULT '';
UPDATE run_steps SET logical_id = id WHERE logical_id = '';
ALTER TABLE run_steps
    ADD COLUMN proof TEXT NOT NULL DEFAULT '';
ALTER TABLE run_steps
    ADD COLUMN estimated_work_units INTEGER NOT NULL DEFAULT 1
        CHECK (estimated_work_units > 0);
ALTER TABLE run_steps
    ADD COLUMN checkpoint_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE run_steps
    ADD COLUMN started_at TEXT;
ALTER TABLE run_steps
    ADD COLUMN completed_at TEXT;
ALTER TABLE run_steps
    ADD COLUMN error_code TEXT;

CREATE INDEX run_steps_run_state
    ON run_steps(run_id, state, sequence);
CREATE UNIQUE INDEX run_steps_run_logical_id
    ON run_steps(run_id, logical_id);
""",
    ),
    (
        6,
        """
CREATE TABLE reference_indexes (
    attachment_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    original_name TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    status TEXT NOT NULL,
    text_blob_id TEXT REFERENCES blobs(id),
    character_count INTEGER NOT NULL DEFAULT 0
        CHECK (character_count >= 0),
    chunk_count INTEGER NOT NULL DEFAULT 0
        CHECK (chunk_count >= 0),
    error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE reference_chunks (
    id TEXT PRIMARY KEY,
    attachment_id TEXT NOT NULL
        REFERENCES reference_indexes(attachment_id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL CHECK (chunk_index >= 0),
    text TEXT NOT NULL,
    character_count INTEGER NOT NULL CHECK (character_count >= 0),
    UNIQUE(attachment_id, chunk_index)
);

CREATE VIRTUAL TABLE reference_chunks_fts USING fts5(
    chunk_id UNINDEXED,
    attachment_id UNINDEXED,
    text,
    tokenize = 'unicode61'
);

CREATE TRIGGER reference_chunks_fts_insert
AFTER INSERT ON reference_chunks BEGIN
    INSERT INTO reference_chunks_fts(chunk_id, attachment_id, text)
    VALUES (new.id, new.attachment_id, new.text);
END;

CREATE TRIGGER reference_chunks_fts_delete
AFTER DELETE ON reference_chunks BEGIN
    DELETE FROM reference_chunks_fts WHERE chunk_id = old.id;
END;

CREATE TRIGGER reference_chunks_fts_update
AFTER UPDATE ON reference_chunks BEGIN
    DELETE FROM reference_chunks_fts WHERE chunk_id = old.id;
    INSERT INTO reference_chunks_fts(chunk_id, attachment_id, text)
    VALUES (new.id, new.attachment_id, new.text);
END;

CREATE INDEX reference_indexes_workspace_status
    ON reference_indexes(workspace_id, status, updated_at);
CREATE INDEX reference_chunks_attachment
    ON reference_chunks(attachment_id, chunk_index);
""",
    ),
    (
        7,
        """
CREATE TABLE changeset_undos (
    id TEXT PRIMARY KEY,
    changeset_id TEXT NOT NULL UNIQUE
        REFERENCES changesets(id) ON DELETE CASCADE,
    workspace_id TEXT NOT NULL
        REFERENCES workspaces(id) ON DELETE CASCADE,
    safety_blob_id TEXT NOT NULL REFERENCES blobs(id),
    restored_sha256 TEXT NOT NULL,
    validation_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX changeset_undos_workspace_created
    ON changeset_undos(workspace_id, created_at);
""",
    ),
)
