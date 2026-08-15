import { useState } from "react";

import { formatLocalDate } from "../../lib/format";
import type { ChangeReview } from "../../types";
import { Dialog } from "../Dialog";
import { UndoIcon } from "../icons";

interface ChangeReviewPanelProps {
  review: ChangeReview | null;
  onUndo: (changesetId: string) => Promise<void>;
}

export function ChangeReviewPanel({
  review,
  onUndo,
}: ChangeReviewPanelProps) {
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  if (!review?.changeset_id) {
    return (
      <div className="panel-state">
        No verified document change is available for review.
      </div>
    );
  }
  const assertions = Object.entries(review.assertions);
  const excerpts = review.excerpts ?? [];
  const formulaStyleChanges = review.formula_style_changes ?? [];
  return (
    <div className="change-review">
      <div className="change-summary">
        <div>
          <strong>{review.undone ? "Run undone" : "Verified change"}</strong>
          <span>{formatLocalDate(review.created_at)}</span>
        </div>
        <p>
          {review.affected_paths.length
            ? `${review.affected_paths.length} stable path(s) recorded.`
            : "The package changed with document-level evidence."}
        </p>
      </div>
      <section>
        <h3>Affected paths</h3>
        <ul className="path-list">
          {review.affected_paths.map((path) => (
            <li key={path}>{path}</li>
          ))}
        </ul>
      </section>
      <section>
        <h3>Verification proof</h3>
        <dl className="assertion-list">
          {assertions.map(([key, value]) => (
            <div key={key}>
              <dt>{key.replaceAll("_", " ")}</dt>
              <dd data-state={value === true ? "pass" : undefined}>
                {typeof value === "boolean" ? (value ? "Passed" : "No") : String(value)}
              </dd>
            </div>
          ))}
        </dl>
      </section>
      {excerpts.length ? (
        <section>
          <h3>Before and after</h3>
          <div className="change-excerpts">
            {excerpts.map((excerpt) => (
              <details key={excerpt.path}>
                <summary>{excerpt.path}</summary>
                <dl className="node-details">
                  <dt>Before</dt>
                  <dd className="node-excerpt">
                    {excerpt.before || "No indexed text"}
                  </dd>
                  <dt>After</dt>
                  <dd className="node-excerpt">
                    {excerpt.after || "No indexed text"}
                  </dd>
                </dl>
              </details>
            ))}
          </div>
        </section>
      ) : null}
      {formulaStyleChanges.length ? (
        <section>
          <h3>Formula and style evidence</h3>
          <ul className="path-list">
            {formulaStyleChanges.map((change) => (
              <li key={change.path}>
                {change.path}: {change.fields.join(", ")}
              </li>
            ))}
          </ul>
        </section>
      ) : null}
      <button
        className="undo-button"
        type="button"
        disabled={!review.can_undo || busy}
        onClick={() => setConfirming(true)}
      >
        <UndoIcon />
        {review.undone ? "Already undone" : "Undo this run"}
      </button>
      {!review.can_undo && review.undo_reason ? (
        <p className="undo-reason">{review.undo_reason}</p>
      ) : null}
      <Dialog
        open={confirming}
        title="Undo this completed run?"
        description="Ogent will restore the exact verified pre-run package only if the current document still matches this run."
        onClose={() => {
          if (!busy) setConfirming(false);
        }}
      >
        <div className="dialog-body">
          <p>
            This creates a safety snapshot, restores the earlier package, validates
            it, advances the document revision, and keeps an audit record.
          </p>
          <div className="dialog-actions">
            <button
              className="secondary-button"
              type="button"
              disabled={busy}
              onClick={() => setConfirming(false)}
            >
              Cancel
            </button>
            <button
              className="danger-button"
              type="button"
              disabled={busy}
              onClick={async () => {
                setBusy(true);
                try {
                  await onUndo(review.changeset_id!);
                  setConfirming(false);
                } finally {
                  setBusy(false);
                }
              }}
            >
              {busy ? "Restoring…" : "Undo verified run"}
            </button>
          </div>
        </div>
      </Dialog>
    </div>
  );
}
