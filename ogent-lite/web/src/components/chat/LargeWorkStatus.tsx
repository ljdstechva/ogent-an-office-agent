import { useEffect, useState } from "react";

import type { WorkspaceSnapshot } from "../../types";

export function LargeWorkStatus({
  workspace,
  onRetry,
  retrying = false,
}: {
  workspace: WorkspaceSnapshot;
  onRetry: (resumable: boolean) => void;
  retrying?: boolean;
}) {
  const [elapsed, setElapsed] = useState(0);
  const active = !["idle", "error"].includes(workspace.run_status);
  useEffect(() => {
    if (!active) {
      setElapsed(0);
      return;
    }
    const started = Date.now();
    const timer = window.setInterval(
      () => setElapsed(Math.floor((Date.now() - started) / 1000)),
      1000,
    );
    return () => window.clearInterval(timer);
  }, [active, workspace.run_id]);

  const index = workspace.document_index;
  const currentStep = workspace.run_steps?.find(
    (step) => step.state === "running",
  );
  const failedStep = workspace.run_steps?.find(
    (step) => step.state === "failed",
  );
  const resumable = Boolean(
    failedStep?.checkpoint?.partition_manifest_blob_id &&
      failedStep.checkpoint.next_partition,
  );
  if (!index && !workspace.run_plan) return null;
  return (
    <section className="large-work-status" aria-label="Large work controls">
      <div>
        <span>Index</span>
        <strong>
          {index
            ? `${Math.round((index.progress ?? 0) * 100)}% · ${index.status.replaceAll("_", " ")}`
            : "Not started"}
        </strong>
      </div>
      <div>
        <span>Current step</span>
        <strong>{currentStep?.description ?? (active ? "Preparing" : "Idle")}</strong>
      </div>
      <div>
        <span>Elapsed</span>
        <strong>{active ? `${elapsed}s` : "—"}</strong>
      </div>
      {failedStep ? (
        <button
          className="secondary-button"
          type="button"
          disabled={retrying}
          onClick={() => onRetry(resumable)}
        >
          {retrying
            ? "Resuming…"
            : resumable
              ? "Resume interrupted review"
              : "Retry failed request"}
        </button>
      ) : null}
    </section>
  );
}
