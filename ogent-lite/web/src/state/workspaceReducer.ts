import type {
  ActivityItem,
  AssistantStream,
  RunStep,
  WorkspaceEventEnvelope,
  WorkspaceSnapshot,
} from "../types";

export type WorkspaceAction =
  | { type: "snapshot"; snapshot: Partial<WorkspaceSnapshot> }
  | { type: "transcript"; transcript: WorkspaceSnapshot["transcript"]; total: number }
  | { type: "stream"; connected: boolean }
  | { type: "event"; envelope: WorkspaceEventEnvelope }
  | { type: "reset"; snapshot?: Partial<WorkspaceSnapshot> };

const scopedEvents = new Set([
  "activity",
  "assistant.completed",
  "assistant.delta",
  "conversation_reset",
  "memory_cleared",
  "message",
  "preview_selection",
  "preview_status",
  "references",
  "run",
  "run.accepted",
  "run.plan",
  "run.step.cancelled",
  "run.step.checkpoint",
  "run.step.completed",
  "run.step.failed",
  "run.step.pending",
  "run.step.started",
  "turn.appended",
]);

function appendActivity(
  current: ActivityItem[],
  stream: unknown,
  text: unknown,
): ActivityItem[] {
  const safeText = typeof text === "string" ? text : "";
  if (!safeText) return current;
  return [
    ...current,
    {
      id: `${Date.now()}-${current.length}`,
      stream: typeof stream === "string" ? stream : "agent",
      text: safeText,
    },
  ].slice(-180);
}

function updateStep(steps: RunStep[], value: unknown): RunStep[] {
  if (!value || typeof value !== "object") return steps;
  const step = value as RunStep;
  if (!step.id) return steps;
  const found = steps.some((item) => item.id === step.id);
  return found
    ? steps.map((item) => (item.id === step.id ? step : item))
    : [...steps, step].sort((left, right) => left.sequence - right.sequence);
}

function assistantDelta(
  current: AssistantStream | null | undefined,
  data: Record<string, unknown>,
): AssistantStream {
  const runId = String(data.run_id ?? "");
  const base =
    current?.run_id === runId
      ? current
      : {
          run_id: runId,
          provider: String(data.provider ?? ""),
          status: "streaming" as const,
          text: "",
          delta_count: 0,
        };
  const expectedLength = Number(data.character_count ?? 0);
  const delta = String(data.delta ?? "");
  const text =
    expectedLength > 0 && base.text.length >= expectedLength
      ? base.text
      : `${base.text}${delta}`;
  return {
    ...base,
    status: "streaming",
    text,
    character_count: text.length,
    delta_count: Number(data.delta_index ?? base.delta_count ?? 0),
  };
}

export function workspaceReducer(
  state: WorkspaceSnapshot,
  action: WorkspaceAction,
): WorkspaceSnapshot {
  if (action.type === "snapshot") {
    return {
      ...state,
      ...action.snapshot,
      activity: state.activity,
      stream_connected: state.stream_connected,
    };
  }
  if (action.type === "transcript") {
    return {
      ...state,
      transcript: action.transcript,
      transcript_total: action.total,
    };
  }
  if (action.type === "stream") {
    return { ...state, stream_connected: action.connected };
  }
  if (action.type === "reset") {
    return {
      ...state,
      ...(action.snapshot ?? {}),
      transcript: [],
      references: [],
      retained_attachments: [],
      run_plan: null,
      run_steps: [],
      assistant_stream: null,
      activity: [],
      last_run_outcome: "neutral",
      run_status: "idle",
      preview_selection:
        action.snapshot?.preview_selection ?? { targets: [] },
    };
  }

  const { envelope } = action;
  const data = envelope.data ?? {};
  const eventGeneration = Number(
    envelope.generation ??
      data.generation ??
      state.conversation_generation,
  );
  if (
    scopedEvents.has(envelope.type) &&
    envelope.type !== "conversation_reset" &&
    eventGeneration !== state.conversation_generation
  ) {
    return state;
  }
  switch (envelope.type) {
    case "snapshot":
      return {
        ...state,
        ...(data as Partial<WorkspaceSnapshot>),
        activity: state.activity,
        stream_connected: state.stream_connected,
      };
    case "conversation_reset":
    case "memory_cleared":
      return workspaceReducer(state, {
        type: "reset",
        snapshot: {
          conversation_generation: eventGeneration,
          session_memory:
            (data.session_memory as WorkspaceSnapshot["session_memory"]) ??
            (data as WorkspaceSnapshot["session_memory"]),
          preview_selection:
            (data.preview_selection as WorkspaceSnapshot["preview_selection"]) ??
            { targets: [] },
        },
      });
    case "message":
      return {
        ...state,
        transcript: [
          ...state.transcript,
          data as unknown as WorkspaceSnapshot["transcript"][number],
        ],
      };
    case "assistant.delta":
      return {
        ...state,
        assistant_stream: assistantDelta(state.assistant_stream, data),
      };
    case "assistant.completed":
      return {
        ...state,
        assistant_stream: {
          run_id: String(data.run_id ?? ""),
          provider: String(data.provider ?? ""),
          status: "completed",
          text: String(data.text ?? ""),
          character_count: Number(data.character_count ?? 0),
          delta_count: Number(data.delta_count ?? 0),
        },
      };
    case "activity":
      return {
        ...state,
        activity: appendActivity(state.activity, data.stream, data.text),
      };
    case "recent":
      return { ...state, recent: (data.items as string[]) ?? [] };
    case "sessions":
      return {
        ...state,
        sessions:
          (data.items as WorkspaceSnapshot["sessions"]) ?? [],
      };
    case "references":
      return {
        ...state,
        references:
          (data.items as WorkspaceSnapshot["references"]) ?? [],
        retained_attachments:
          (data.retained as WorkspaceSnapshot["retained_attachments"]) ??
          state.retained_attachments,
      };
    case "preview_selection":
      return {
        ...state,
        preview_selection: data as unknown as WorkspaceSnapshot["preview_selection"],
      };
    case "preview_status":
      return {
        ...state,
        preview_update_status: String(data.status ?? "idle"),
        preview_update_message: String(data.message ?? ""),
        preview_confirmation:
          (data.confirmation as Record<string, unknown> | null) ?? null,
      };
    case "recovery":
      return {
        ...state,
        recovery: data as WorkspaceSnapshot["recovery"],
      };
    case "run.plan":
      return {
        ...state,
        run_id: String(data.run_id ?? state.run_id ?? ""),
        run_plan: data.plan as WorkspaceSnapshot["run_plan"],
        run_steps:
          (data.steps as WorkspaceSnapshot["run_steps"]) ?? [],
      };
    case "run.step.started":
    case "run.step.completed":
    case "run.step.failed":
    case "run.step.cancelled":
    case "run.step.pending":
    case "run.step.checkpoint":
      return {
        ...state,
        run_steps: updateStep(state.run_steps ?? [], data.step),
      };
    case "run": {
      const status = String(data.status ?? state.run_status);
      const outcome = data.outcome
        ? String(data.outcome)
        : status === "working" || status === "starting"
          ? "working"
          : state.last_run_outcome;
      return {
        ...state,
        run_status: status,
        last_run_outcome: outcome,
        run_id:
          typeof data.run_id === "string" ? data.run_id : state.run_id,
        activity: appendActivity(
          state.activity,
          "run",
          `${String(data.provider ?? data.kind ?? "Agent")} ${String(
            data.label ?? data.status ?? "updated",
          )}`,
        ),
      };
    }
    case "watch":
      return {
        ...state,
        watch_alive: data.status === "ready",
        watch_url:
          typeof data.watch_url === "string"
            ? data.watch_url
            : state.watch_url,
        watch_port:
          typeof data.port === "number" ? data.port : state.watch_port,
        watch_generation:
          typeof data.watch_generation === "string"
            ? data.watch_generation
            : state.watch_generation,
        preview_identity:
          (data.preview_identity as WorkspaceSnapshot["preview_identity"]) ??
          state.preview_identity,
      };
    case "document":
      return {
        ...state,
        active_document: String(data.working ?? state.active_document ?? ""),
        source_document:
          typeof data.source === "string" ? data.source : state.source_document,
        watch_url:
          typeof data.watch_url === "string"
            ? data.watch_url
            : state.watch_url,
        watch_port:
          typeof data.watch_port === "number"
            ? data.watch_port
            : state.watch_port,
        watch_generation:
          typeof data.watch_generation === "string"
            ? data.watch_generation
            : state.watch_generation,
        document_revision: Number(
          data.document_revision ?? state.document_revision ?? 0,
        ),
        preview_identity:
          (data.preview_identity as WorkspaceSnapshot["preview_identity"]) ??
          state.preview_identity,
        complex_layout: Boolean(data.complex_layout),
        complex_layout_detail:
          typeof data.complex_layout_detail === "string"
            ? data.complex_layout_detail
            : null,
        document_mode:
          typeof data.document_mode === "string"
            ? data.document_mode
            : state.document_mode,
        watch_alive: true,
      };
    case "document_revision":
      return {
        ...state,
        document_revision: Number(
          data.revision ?? state.document_revision ?? 0,
        ),
      };
    case "index_progress":
      return {
        ...state,
        ...(data.role === "source"
          ? { source_document_index: data as unknown as WorkspaceSnapshot["source_document_index"] }
          : { document_index: data as unknown as WorkspaceSnapshot["document_index"] }),
      };
    case "snapshot_status":
      return {
        ...state,
        snapshot_in_progress: data.status === "working",
        snapshot_error:
          typeof data.error === "string" ? data.error : state.snapshot_error,
      };
    default:
      return state;
  }
}
