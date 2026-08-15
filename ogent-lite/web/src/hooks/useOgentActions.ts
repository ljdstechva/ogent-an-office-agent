import { useCallback, useState } from "react";

import { api } from "../api/client";
import type {
  Attachment,
  PreviewSelection,
  WorkspaceSnapshot,
} from "../types";
import {
  defaultInlineTurnCharacters,
  defaultTextAssetBytes,
  planLargeTextAsset,
} from "../lib/largeTextAsset";

const maxDocumentBytes = 128 * 1024 * 1024;
const maxReferenceBytes = 50 * 1024 * 1024;
const maxReferenceTotalBytes = 100 * 1024 * 1024;

interface OpenResult extends Partial<WorkspaceSnapshot> {
  action?: string;
  session_id?: string;
  message?: string;
  uploaded?: boolean;
  blank_initialized?: boolean;
}

interface ActionOptions {
  state: WorkspaceSnapshot;
  update: (value: Partial<WorkspaceSnapshot>) => void;
  notify: (message: string) => void;
}

export interface OgentActions {
  opening: boolean;
  uploadingDocument: boolean;
  uploadingReferences: boolean;
  sending: boolean;
  stopping: boolean;
  resuming: boolean;
  openPath: (path: string) => Promise<void>;
  browsePath: () => Promise<string>;
  uploadDocument: (file: File) => Promise<void>;
  uploadReferences: (files: File[]) => Promise<void>;
  removeReference: (attachment: Attachment) => Promise<void>;
  clearReferences: () => Promise<void>;
  clearSelection: () => Promise<void>;
  removeSelection: (selectionId: string) => Promise<void>;
  setMultiSelect: (enabled: boolean) => Promise<void>;
  send: (
    message: string,
    provider: string,
    model: string,
    effort: string,
    fast?: boolean,
  ) => Promise<boolean>;
  stop: () => Promise<void>;
  resume: (runId: string) => Promise<boolean>;
  resetConversation: () => Promise<void>;
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : "The request failed.";
}

export function useOgentActions({
  state,
  update,
  notify,
}: ActionOptions): OgentActions {
  const [opening, setOpening] = useState(false);
  const [uploadingDocument, setUploadingDocument] = useState(false);
  const [uploadingReferences, setUploadingReferences] = useState(false);
  const [sending, setSending] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [resuming, setResuming] = useState(false);

  const applyOpenResult = useCallback(
    (result: OpenResult) => {
      if (result.action === "focus_session" && result.session_id) {
        window.location.assign(`/?s=${encodeURIComponent(result.session_id)}`);
        return;
      }
      if (result.action === "document_already_open") {
        notify(result.message ?? "That document is already open.");
        return;
      }
      if (result.action === "pdf_import") {
        notify(result.message ?? "Preparing a protected PDF working copy.");
        return;
      }
      update({
        active_document: result.active_document ?? null,
        watch_url: result.watch_url ?? null,
        watch_port: result.watch_port ?? null,
        watch_generation: result.watch_generation ?? null,
        document_revision: result.document_revision ?? 0,
        preview_identity: result.preview_identity ?? null,
        complex_layout: Boolean(result.complex_layout),
        complex_layout_detail: result.complex_layout_detail ?? null,
        document_mode:
          result.document_mode ??
          (result.uploaded ? "browser_import" : "local_direct"),
        last_run_outcome: "neutral",
        preview_selection: { targets: [] },
        watch_alive: true,
      });
      notify(
        result.blank_initialized
          ? "Blank Office document initialized and opened."
          : result.uploaded
            ? "Browser upload · editing an imported copy. Save or copy the finished file when done."
            : result.document_mode === "local_direct"
              ? "Editing original · recovery backup created"
              : result.message ?? "Protected working document opened.",
      );
    },
    [notify, update],
  );

  return {
    opening,
    uploadingDocument,
    uploadingReferences,
    sending,
    stopping,
    resuming,
    openPath: async (path) => {
      const clean = path.trim();
      if (!clean) {
        notify("Paste a document path.");
        return;
      }
      setOpening(true);
      try {
        const result = await api<OpenResult>("/open", {
          method: "POST",
          body: JSON.stringify({ path: clean }),
        });
        applyOpenResult(result);
      } catch (error) {
        notify(errorMessage(error));
      } finally {
        setOpening(false);
      }
    },
    browsePath: async () => {
      try {
        const result = await api<{ path?: string }>("/pick", {
          method: "POST",
          body: "{}",
        });
        return result.path ?? "";
      } catch (error) {
        notify(errorMessage(error));
        return "";
      }
    },
    uploadDocument: async (file) => {
      if (!/\.(docx|xlsx|pptx|pdf)$/i.test(file.name)) {
        notify("Choose a DOCX, XLSX, PPTX, or PDF file.");
        return;
      }
      const blankOffice =
        file.size === 0 && /\.(docx|xlsx|pptx)$/i.test(file.name);
      if (file.size === 0 && !blankOffice) {
        notify("An empty PDF has no pages to open.");
        return;
      }
      if (file.size > maxDocumentBytes) {
        notify("The document exceeds Ogent's 128 MB import limit.");
        return;
      }
      setUploadingDocument(true);
      notify(blankOffice ? `Initializing ${file.name}…` : `Importing ${file.name}…`);
      try {
        const result = await api<OpenResult>("/upload", {
          method: "POST",
          headers: {
            "Content-Type": "application/octet-stream",
            "X-Ogent-Filename": encodeURIComponent(file.name),
          },
          body: file,
        });
        applyOpenResult(result);
      } catch (error) {
        notify(errorMessage(error));
      } finally {
        setUploadingDocument(false);
      }
    },
    uploadReferences: async (files) => {
      const selected = files.filter(Boolean);
      if (!selected.length) return;
      const usable = selected.filter((file) =>
        /\.(docx|xlsx|pptx|pdf|txt|md|csv|png|jpe?g|webp|bmp|tiff?)$/i.test(
          file.name,
        ),
      );
      if (usable.length !== selected.length) {
        notify("Unsupported attachment skipped.");
      }
      const invalid = usable.find(
        (file) => file.size === 0 || file.size > maxReferenceBytes,
      );
      if (invalid) {
        notify(
          invalid.size === 0
            ? `${invalid.name} is empty.`
            : `${invalid.name} exceeds the 50 MB attachment limit.`,
        );
        return;
      }
      const ready = state.references.filter(
        (attachment) => attachment.status !== "Failed",
      );
      if (ready.length + usable.length > 20) {
        notify("Each message accepts up to 20 attachments.");
        return;
      }
      const bytes =
        ready.reduce((sum, item) => sum + Number(item.size ?? 0), 0) +
        usable.reduce((sum, item) => sum + item.size, 0);
      if (bytes > maxReferenceTotalBytes) {
        notify("Attachments for one message may total at most 100 MB.");
        return;
      }
      setUploadingReferences(true);
      try {
        let next = 0;
        const uploaded: Attachment[] = [];
        const worker = async () => {
          while (next < usable.length) {
            const file = usable[next++];
            const result = await api<{
              attachment: Attachment;
              references: Attachment[];
              retained: Attachment[];
            }>("/reference/upload", {
              method: "POST",
              headers: {
                "Content-Type": "application/octet-stream",
                "X-Ogent-Filename": encodeURIComponent(file.name),
              },
              body: file,
            });
            uploaded.push(result.attachment);
            update({
              references: result.references,
              retained_attachments: result.retained,
            });
          }
        };
        await Promise.all(
          Array.from(
            { length: Math.min(3, usable.length) },
            () => worker(),
          ),
        );
        notify(`${uploaded.length} attachment(s) ready for the next message.`);
      } catch (error) {
        notify(errorMessage(error));
      } finally {
        setUploadingReferences(false);
      }
    },
    removeReference: async (attachment) => {
      try {
        const result = await api<{
          references: Attachment[];
          retained: Attachment[];
        }>("/reference/remove", {
          method: "POST",
          body: JSON.stringify({ attachment_id: attachment.id }),
        });
        update({
          references: result.references,
          retained_attachments: result.retained,
        });
      } catch (error) {
        notify(errorMessage(error));
      }
    },
    clearReferences: async () => {
      try {
        const result = await api<{
          references: Attachment[];
          retained: Attachment[];
        }>("/reference/clear", { method: "POST", body: "{}" });
        update({
          references: result.references,
          retained_attachments: result.retained,
        });
      } catch (error) {
        notify(errorMessage(error));
      }
    },
    clearSelection: async () => {
      try {
        const result = await api<{ preview_selection: PreviewSelection }>(
          "/selection/clear",
          { method: "POST", body: "{}" },
        );
        update({ preview_selection: result.preview_selection });
      } catch (error) {
        notify(errorMessage(error));
      }
    },
    removeSelection: async (selectionId) => {
      try {
        const result = await api<{ preview_selection: PreviewSelection }>(
          "/selection/remove",
          {
            method: "POST",
            body: JSON.stringify({ selection_id: selectionId }),
          },
        );
        update({ preview_selection: result.preview_selection });
      } catch (error) {
        notify(errorMessage(error));
      }
    },
    setMultiSelect: async (enabled) => {
      try {
        const result = await api<{ preview_selection: PreviewSelection }>(
          "/selection/multi-mode",
          {
            method: "POST",
            body: JSON.stringify({ enabled }),
          },
        );
        update({ preview_selection: result.preview_selection });
        notify(
          enabled
            ? "Touch multi-select is on."
            : "Touch multi-select is off.",
        );
      } catch (error) {
        notify(errorMessage(error));
      }
    },
    send: async (message, provider, model, effort, fast = false) => {
      const text = message.trim() ? message : "";
      const hasReferences = state.references.some(
        (attachment) => attachment.status !== "Failed",
      );
      const hasSelection = state.preview_selection.targets.length > 0;
      if (!text && !hasReferences && !hasSelection) return false;
      let outgoingMessage = message;
      let textAsset = null;
      try {
        textAsset = planLargeTextAsset(message, {
          inlineCharacterLimit:
            state.quotas?.max_inline_turn_characters ??
            defaultInlineTurnCharacters,
          assetByteLimit:
            state.quotas?.max_reference_file_bytes ??
            defaultTextAssetBytes,
        });
      } catch (error) {
        notify(errorMessage(error));
        return false;
      }
      if (textAsset && state.features?.large_text_assets === false) {
        notify(
          "This Ogent configuration does not allow large pasted-text assets.",
        );
        return false;
      }
      setSending(true);
      try {
        if (textAsset) {
          const alreadyPending = state.references.some(
            (attachment) =>
              attachment.filename === textAsset.filename &&
              attachment.status !== "Failed",
          );
          if (!alreadyPending) {
            setUploadingReferences(true);
            const file = new File([textAsset.text], textAsset.filename, {
              type: "text/plain;charset=utf-8",
            });
            const uploaded = await api<{
              attachment: Attachment;
              references: Attachment[];
              retained: Attachment[];
            }>("/reference/upload", {
              method: "POST",
              headers: {
                "Content-Type": "application/octet-stream",
                "X-Ogent-Filename": encodeURIComponent(file.name),
              },
              body: file,
            });
            update({
              references: uploaded.references,
              retained_attachments: uploaded.retained,
            });
          }
          outgoingMessage = textAsset.prompt;
          notify(
            "Large pasted text stored losslessly as an indexed attachment.",
          );
        }
        const result = await api<{ action?: string; session_id?: string }>(
          "/chat",
          {
            method: "POST",
            body: JSON.stringify({
              message: outgoingMessage,
              provider,
              model,
              effort,
              fast,
            }),
          },
        );
        update({
          references: [],
          preview_selection: {
            ...state.preview_selection,
            targets: [],
            limit_message: undefined,
          },
          run_status: "starting",
          last_run_outcome: "working",
        });
        if (result.action === "focus_session" && result.session_id) {
          window.location.assign(
            `/?s=${encodeURIComponent(result.session_id)}`,
          );
        }
        return true;
      } catch (error) {
        notify(errorMessage(error));
        return false;
      } finally {
        if (textAsset) setUploadingReferences(false);
        setSending(false);
      }
    },
    stop: async () => {
      setStopping(true);
      try {
        await api("/stop", { method: "POST", body: "{}" });
      } catch (error) {
        notify(errorMessage(error));
      } finally {
        setStopping(false);
      }
    },
    resume: async (runId: string) => {
      if (!/^[0-9a-f]{32}$/.test(runId)) {
        notify("The interrupted run identifier is unavailable.");
        return false;
      }
      setResuming(true);
      try {
        const result = await api<{
          run_id: string;
          completed_partitions: number;
          partition_count: number;
        }>(
          `/api/workspaces/${encodeURIComponent(state.session_id)}/runs/${runId}/resume`,
          { method: "POST", body: "{}" },
        );
        update({
          run_id: result.run_id,
          run_status: "starting",
          last_run_outcome: "working",
          last_error: null,
        });
        notify(
          `Resuming after ${result.completed_partitions}/${result.partition_count} completed partitions.`,
        );
        return true;
      } catch (error) {
        notify(errorMessage(error));
        return false;
      } finally {
        setResuming(false);
      }
    },
    resetConversation: async () => {
      try {
        const result = await api<
          Partial<WorkspaceSnapshot> & { message?: string }
        >("/conversation/reset", {
          method: "POST",
          body: JSON.stringify({ confirm: true }),
        });
        update({
          transcript: [],
          references: [],
          retained_attachments: [],
          preview_selection: result.preview_selection ?? { targets: [] },
          session_memory: result.session_memory ?? {},
          conversation_generation:
            result.conversation_generation ??
            state.conversation_generation + 1,
          run_plan: null,
          run_steps: [],
          assistant_stream: null,
          last_run_outcome: "neutral",
          run_status: "idle",
        });
        notify(result.message ?? "New chat started for this document.");
      } catch (error) {
        notify(errorMessage(error));
        throw error;
      }
    },
  };
}
