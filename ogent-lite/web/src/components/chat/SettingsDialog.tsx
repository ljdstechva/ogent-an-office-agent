import { useState } from "react";

import { api } from "../../api/client";
import type { OgentActions } from "../../hooks/useOgentActions";
import { formatLocalDate, humanFileSize } from "../../lib/format";
import type { Attachment, RecoverySummary, WorkspaceSnapshot } from "../../types";
import { Dialog } from "../Dialog";

interface SettingsDialogProps {
  open: boolean;
  onClose: () => void;
  workspace: WorkspaceSnapshot;
  actions: OgentActions;
  update: (value: Partial<WorkspaceSnapshot>) => void;
  notify: (message: string) => void;
}

export function SettingsDialog({
  open,
  onClose,
  workspace,
  actions,
  update,
  notify,
}: SettingsDialogProps) {
  const [busy, setBusy] = useState<string | null>(null);
  const recovery = workspace.recovery;
  const memory = workspace.session_memory;

  const run = async (key: string, callback: () => Promise<void>) => {
    if (busy) return;
    setBusy(key);
    try {
      await callback();
    } catch (error) {
      notify(error instanceof Error ? error.message : "The settings action failed.");
    } finally {
      setBusy(null);
    }
  };

  return (
    <Dialog
      open={open}
      onClose={onClose}
      title="Settings and recovery"
      description="Workspace-local recovery, memory, and retained attachment controls."
      className="settings-dialog"
    >
      <div className="dialog-scroll">
        <section className="settings-section" aria-labelledby="recovery-title">
          <h3 id="recovery-title">Recovery backups</h3>
          <dl className="settings-grid">
            <dt>Folder</dt>
            <dd>{recovery.folder || "—"}</dd>
            <dt>Retention</dt>
            <dd>{Number(recovery.retention_days ?? 30)} days</dd>
            <dt>Backups</dt>
            <dd>{Number(recovery.count ?? 0)}</dd>
            <dt>Total size</dt>
            <dd>{humanFileSize(recovery.total_size)}</dd>
            <dt>Oldest</dt>
            <dd>{formatLocalDate(recovery.oldest_created_at)}</dd>
            <dt>Newest</dt>
            <dd>{formatLocalDate(recovery.newest_created_at)}</dd>
            <dt>Last cleanup</dt>
            <dd>
              {recovery.last_cleanup
                ? `${formatLocalDate(recovery.last_cleanup.completed_at)} · deleted ${
                    recovery.last_cleanup.deleted ?? 0
                  }`
                : "Not run"}
            </dd>
          </dl>
          <p>
            Expiry is exactly 30 × 24 hours from creation and is applied at the
            first cleanup after that time.
          </p>
          <div className="settings-actions">
            <button
              className="secondary-button"
              type="button"
              disabled={Boolean(busy)}
              onClick={() =>
                void run("folder", async () => {
                  const result = await api<{ message?: string }>(
                    "/settings/recovery/open-folder",
                    { method: "POST", body: "{}" },
                  );
                  notify(result.message ?? "Recovery folder opened.");
                })
              }
            >
              Open backup folder
            </button>
            <button
              className="secondary-button"
              type="button"
              disabled={Boolean(busy)}
              onClick={() =>
                void run("cleanup", async () => {
                  const result = await api<{
                    message?: string;
                    recovery: RecoverySummary;
                  }>("/settings/recovery/delete-expired", {
                    method: "POST",
                    body: "{}",
                  });
                  update({ recovery: result.recovery });
                  notify(result.message ?? "Expired cleanup completed.");
                })
              }
            >
              {busy === "cleanup" ? "Cleaning…" : "Delete expired now"}
            </button>
          </div>
        </section>
        <section className="settings-section" aria-labelledby="memory-title">
          <h3 id="memory-title">Session memory</h3>
          <dl className="settings-grid">
            <dt>Retained turns</dt>
            <dd>{Number(memory.retained_turns ?? 0)}</dd>
            <dt>Attachments</dt>
            <dd>{Number(memory.retained_attachments ?? 0)}</dd>
            <dt>Attachment size</dt>
            <dd>{humanFileSize(memory.retained_attachment_bytes)}</dd>
            <dt>Workspace created</dt>
            <dd>{formatLocalDate(memory.created_at)}</dd>
          </dl>
          <p>
            Conversation state is durable for this workspace. Provider-side data
            policies still apply independently.
          </p>
          <div className="retained-list" aria-label="Retained attachments">
            {workspace.retained_attachments.length ? (
              workspace.retained_attachments.map((item) => (
                <div className="retained-item" key={item.id}>
                  <span>
                    <strong>{item.filename}</strong>
                    <small>
                      {item.detected_type ?? item.kind ?? "File"} ·{" "}
                      {humanFileSize(item.size)}
                    </small>
                  </span>
                  <button
                    className="secondary-button"
                    type="button"
                    disabled={Boolean(busy)}
                    onClick={() =>
                      void run(`forget-${item.id}`, async () => {
                        const result = await api<{
                          references: Attachment[];
                          retained: Attachment[];
                        }>("/reference/forget", {
                          method: "POST",
                          body: JSON.stringify({ attachment_id: item.id }),
                        });
                        update({
                          references: result.references,
                          retained_attachments: result.retained,
                        });
                      })
                    }
                  >
                    Forget
                  </button>
                </div>
              ))
            ) : (
              <p>No retained attachments.</p>
            )}
          </div>
          <button
            className="secondary-button"
            type="button"
            disabled={!workspace.active_document || Boolean(busy)}
            onClick={() =>
              void run("new-chat", async () => {
                await actions.resetConversation();
                onClose();
              })
            }
          >
            Start a new chat
          </button>
        </section>
      </div>
    </Dialog>
  );
}
