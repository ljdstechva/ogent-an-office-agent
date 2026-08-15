import { config } from "../config";
import type { WorkspaceSnapshot } from "../types";

export const initialWorkspace: WorkspaceSnapshot = {
  session_id: config.sessionId,
  active_document: null,
  watch_url: null,
  run_status: "idle",
  last_run_outcome: "neutral",
  transcript: [],
  conversation_generation: 1,
  references: [],
  retained_attachments: [],
  preview_selection: { targets: [] },
  recent: [],
  sessions: [],
  agent_capabilities: { refreshing: true, providers: [] },
  recovery: {},
  session_memory: {},
  activity: [],
  stream_connected: false,
};
