export type RunStatus =
  | "idle"
  | "starting"
  | "working"
  | "stopping"
  | "error";

export type RunOutcome =
  | "neutral"
  | "working"
  | "analysis_completed"
  | "edit_completed"
  | "no_change"
  | "stopped"
  | "error";

export type IndexStatus =
  | "queued"
  | "indexing"
  | "quick_ready"
  | "partial"
  | "complete"
  | "failed"
  | "cancelled";

export interface PreviewIdentity {
  session_id: string;
  document_id: string;
  watch_port: number;
  watch_generation: string;
}

export interface AssistantStream {
  run_id: string;
  provider?: string;
  status: "streaming" | "completed" | "failed" | "cancelled";
  text: string;
  character_count?: number;
  delta_count?: number;
}

export interface Attachment {
  id: string;
  filename: string;
  size: number;
  detected_type?: string;
  kind?: string;
  status?: string;
  processing_status?: string;
  ocr_or_vision?: boolean;
  error?: string | null;
}

export interface PreviewSelectionTarget {
  selection_id: string;
  document_name?: string;
  path: string;
  label?: string;
  kind?: string;
  primary?: boolean;
  stale?: boolean;
}

export interface PreviewSelection {
  targets: PreviewSelectionTarget[];
  multi_select_mode?: boolean;
  limit_message?: string;
}

export interface TranscriptMessage {
  turn_id?: string;
  sequence?: number;
  role: "user" | "assistant" | "system";
  text: string;
  provider?: string;
  model?: string;
  effort?: string;
  run_outcome?: RunOutcome | string;
  attachments?: Attachment[];
  preview_selections?: PreviewSelectionTarget[];
  verification?: Record<string, unknown>;
}

export interface RunStep {
  id: string;
  sequence: number;
  description: string;
  target_node_ids: string[];
  mutates: boolean;
  tool?: string | null;
  proof: string;
  dependencies: string[];
  estimated_work_units: number;
  state:
    | "pending"
    | "running"
    | "completed"
    | "failed"
    | "cancelled";
  checkpoint?: Record<string, unknown>;
  verification?: Record<string, unknown>;
  started_at?: string | null;
  completed_at?: string | null;
  error_code?: string | null;
}

export interface RunPlan {
  schema_version: number;
  goal: string;
  mode: "analysis" | "edit";
  scope: "selected" | "local" | "whole_document";
  complexity: "fast_path" | "structured";
  steps: Omit<RunStep, "state">[];
  target_node_ids: string[];
  expected_mutations: string[];
  verification_assertions: string[];
  coverage_requirement: Record<string, unknown>;
  estimated_work_units: number;
}

export interface DocumentIndex {
  document_id?: string;
  revision_id?: string;
  revision_number?: number;
  status: IndexStatus;
  progress: number;
  indexed_nodes: number;
  total_estimate: number;
  quick_manifest?: Record<string, unknown>;
  manifest?: Record<string, unknown> | null;
  error_code?: string | null;
}

export interface DocumentNode {
  node_id: string;
  stable_path: string;
  parent_path?: string | null;
  kind: string;
  title?: string;
  text?: string;
  metadata?: Record<string, unknown>;
  sheet_name?: string | null;
  slide_number?: number | null;
  page_number?: number | null;
  ordinal?: number;
  content_sha256?: string;
  locator?: {
    native_key?: string;
    stability?: string;
    lineage_key?: string;
    source_paths?: string[];
    namespace?: string;
    resolvable?: boolean;
  };
}

export interface ProviderModel {
  id: string;
  displayName?: string;
  efforts?: string[];
  effortsVerified?: boolean;
  inputContextLimit?: number | null;
  contextLimitSource?: string;
}

export interface ProviderCapability {
  id: string;
  label: string;
  live: boolean;
  status: string;
  warning?: string | null;
  stale?: boolean;
  models?: ProviderModel[];
}

export interface AgentCapabilities {
  refreshing?: boolean;
  refreshingProviders?: string[];
  probing?: Array<{ provider: string; model: string }>;
  providers: ProviderCapability[];
}

export interface SessionSummary {
  id: string;
  document_name?: string;
  run_status?: string;
}

export interface RecoverySummary {
  folder?: string;
  retention_days?: number;
  count?: number;
  total_size?: number;
  oldest_created_at?: string | null;
  newest_created_at?: string | null;
  last_cleanup?: {
    completed_at?: string;
    deleted?: number;
    pending_delete?: number;
  } | null;
}

export interface SessionMemory {
  created_at?: string;
  retained_turns?: number;
  retained_attachments?: number;
  retained_attachment_bytes?: number;
  durable?: boolean;
}

export interface ActivityItem {
  id: string;
  stream: string;
  text: string;
}

export interface CoverageCategory {
  category: string;
  required: number;
  reviewed: number;
  complete: boolean;
}

export interface CoverageReview {
  run_id?: string;
  revision_id?: string;
  required: boolean;
  complete: boolean | null;
  categories: CoverageCategory[];
  unsupported: string[];
  visual_interpretation_used: string[];
  disclosure: string;
}

export interface ChangeReview {
  changeset_id?: string;
  run_id?: string;
  created_at?: string;
  outcome?: string;
  affected_paths: string[];
  assertions: Record<string, boolean | string | number | null>;
  pre_revision_sha256?: string;
  post_revision_sha256?: string;
  excerpts?: Array<{
    path: string;
    before: string;
    after: string;
  }>;
  formula_style_changes?: Array<{
    path: string;
    fields: string[];
  }>;
  can_undo: boolean;
  undone: boolean;
  undo_reason?: string | null;
  undone_at?: string | null;
}

export interface WorkspaceSnapshot {
  app?: string;
  version?: string;
  session_id: string;
  created_at?: string;
  active_document: string | null;
  source_document?: string | null;
  document_mode?: string | null;
  document_id?: string | null;
  document_revision?: number;
  watch_url: string | null;
  watch_alive?: boolean;
  watch_port?: number | null;
  watch_generation?: string | null;
  preview_identity?: PreviewIdentity | null;
  run_status: RunStatus | string;
  last_run_outcome: RunOutcome | string;
  run_id?: string | null;
  run_plan?: RunPlan | null;
  run_steps?: RunStep[];
  assistant_stream?: AssistantStream | null;
  transcript: TranscriptMessage[];
  transcript_paged?: boolean;
  transcript_total?: number;
  transcript_page_url?: string | null;
  conversation_generation: number;
  last_error?: string | null;
  preview_update_status?: string;
  preview_update_message?: string;
  preview_confirmation?: Record<string, unknown> | null;
  complex_layout?: boolean;
  complex_layout_detail?: string | null;
  snapshot_in_progress?: boolean;
  snapshot_available?: boolean;
  snapshot_error?: string | null;
  references: Attachment[];
  retained_attachments: Attachment[];
  preview_selection: PreviewSelection;
  document_index?: DocumentIndex | null;
  source_document_index?: DocumentIndex | null;
  recent: string[];
  sessions: SessionSummary[];
  agent_capabilities: AgentCapabilities;
  recovery: RecoverySummary;
  session_memory: SessionMemory;
  activity: ActivityItem[];
  stream_connected: boolean;
  features?: {
    large_text_assets?: boolean;
    warm_provider_transport?: boolean;
    strict_disk_forecast?: boolean;
  };
  quotas?: {
    max_inline_turn_characters?: number;
    max_reference_file_bytes?: number;
  };
}

export interface WorkspaceEventEnvelope {
  sequence?: number;
  generation?: number;
  type: string;
  data?: Record<string, unknown>;
}

export interface TranscriptPage {
  items: TranscriptMessage[];
  total: number;
  conversation_generation: number;
}

export interface DocumentNodesPage {
  revision_id: string;
  status: IndexStatus;
  offset: number;
  nodes: DocumentNode[];
}
