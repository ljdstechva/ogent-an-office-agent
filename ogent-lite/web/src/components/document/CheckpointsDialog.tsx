import { useCallback, useEffect, useState } from "react";

import { api } from "../../api/client";
import { Dialog } from "../Dialog";

interface Checkpoint {
  name: string;
  source: string;
  created_at: string;
  byte_size: number;
}

interface CheckpointsDialogProps {
  open: boolean;
  onClose: () => void;
  busy: boolean;
  notify: (message: string) => void;
}

function formatTime(value: string): string {
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
}

function formatSize(bytes: number): string {
  if (bytes >= 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${Math.max(1, Math.round(bytes / 1024))} KB`;
}

export function CheckpointsDialog({
  open,
  onClose,
  busy,
  notify,
}: CheckpointsDialogProps) {
  const [checkpoints, setCheckpoints] = useState<Checkpoint[]>([]);
  const [working, setWorking] = useState(false);
  const [confirming, setConfirming] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const result = await api<{ checkpoints: Checkpoint[] }>("/checkpoints");
      setCheckpoints(result.checkpoints);
    } catch (error) {
      notify(
        error instanceof Error
          ? error.message
          : "Checkpoints could not be listed.",
      );
    }
  }, [notify]);

  useEffect(() => {
    if (open) {
      setConfirming(null);
      void refresh();
    }
  }, [open, refresh]);

  const save = async () => {
    setWorking(true);
    try {
      const result = await api<{
        message: string;
        checkpoints: Checkpoint[];
      }>("/checkpoint/save", { method: "POST", body: "{}" });
      setCheckpoints(result.checkpoints);
      notify(result.message);
    } catch (error) {
      notify(
        error instanceof Error
          ? error.message
          : "The checkpoint could not be saved.",
      );
    } finally {
      setWorking(false);
    }
  };

  const restore = async (name: string) => {
    setWorking(true);
    try {
      const result = await api<{
        message: string;
        checkpoints: Checkpoint[];
        warning?: string | null;
      }>("/checkpoint/restore", {
        method: "POST",
        body: JSON.stringify({ name, confirm: true }),
      });
      setCheckpoints(result.checkpoints);
      setConfirming(null);
      notify(result.warning ? `${result.message} ${result.warning}` : result.message);
    } catch (error) {
      notify(
        error instanceof Error
          ? error.message
          : "The checkpoint could not be restored.",
      );
    } finally {
      setWorking(false);
    }
  };

  return (
    <Dialog
      open={open}
      title="Checkpoints"
      description={
        "Timestamped copies stored beside the document under " +
        ".officecli-checkpoints. Restoring first checkpoints the current file."
      }
      onClose={onClose}
      className="checkpoints-dialog"
    >
      <div className="checkpoint-actions">
        <button
          type="button"
          className="toolbar-text-button"
          disabled={working || busy}
          onClick={() => void save()}
        >
          Save checkpoint
        </button>
      </div>
      {checkpoints.length ? (
        <ul className="checkpoint-list">
          {checkpoints.map((checkpoint) => (
            <li key={checkpoint.name}>
              <div className="checkpoint-meta">
                <strong>{formatTime(checkpoint.created_at)}</strong>
                <span>
                  {checkpoint.source} · {formatSize(checkpoint.byte_size)}
                </span>
              </div>
              {confirming === checkpoint.name ? (
                <span className="checkpoint-confirm">
                  <button
                    type="button"
                    className="toolbar-text-button danger"
                    disabled={working || busy}
                    onClick={() => void restore(checkpoint.name)}
                  >
                    Confirm restore
                  </button>
                  <button
                    type="button"
                    className="toolbar-text-button"
                    disabled={working}
                    onClick={() => setConfirming(null)}
                  >
                    Cancel
                  </button>
                </span>
              ) : (
                <button
                  type="button"
                  className="toolbar-text-button"
                  disabled={working || busy}
                  onClick={() => setConfirming(checkpoint.name)}
                >
                  Restore…
                </button>
              )}
            </li>
          ))}
        </ul>
      ) : (
        <p className="checkpoint-empty">
          No checkpoints yet. Save one before risky edits.
        </p>
      )}
    </Dialog>
  );
}
