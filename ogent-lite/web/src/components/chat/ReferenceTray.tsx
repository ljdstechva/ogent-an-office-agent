import { humanFileSize } from "../../lib/format";
import type { OgentActions } from "../../hooks/useOgentActions";
import type { Attachment } from "../../types";
import { CloseIcon } from "../icons";

export function ReferenceTray({
  references,
  actions,
}: {
  references: Attachment[];
  actions: OgentActions;
}) {
  if (!references.length) return null;
  return (
    <section className="reference-tray" aria-label="Attachments for next message">
      <header>
        <span>Attachments for next message</span>
        <button
          type="button"
          disabled={actions.uploadingReferences}
          onClick={() => void actions.clearReferences()}
        >
          Clear all
        </button>
      </header>
      <div className="reference-chips">
        {references.map((item) => (
          <div
            className={`reference-chip${item.status === "Failed" ? " failed" : ""}`}
            key={item.id}
            title={item.error ?? undefined}
          >
            <span>
              <strong>{item.filename}</strong>
              <small>
                {humanFileSize(item.size)} · {item.status ?? "Ready"}
              </small>
            </span>
            <button
              type="button"
              aria-label={`Remove ${item.filename}`}
              onClick={() => void actions.removeReference(item)}
            >
              <CloseIcon />
            </button>
          </div>
        ))}
      </div>
    </section>
  );
}
