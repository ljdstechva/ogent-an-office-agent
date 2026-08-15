import type { OgentActions } from "../../hooks/useOgentActions";
import type { PreviewSelection } from "../../types";
import { CloseIcon } from "../icons";

export function SelectionTray({
  selection,
  actions,
}: {
  selection: PreviewSelection;
  actions: OgentActions;
}) {
  if (!selection.targets.length) return null;
  return (
    <section className="selection-tray" aria-label="Focused preview context">
      <header>
        <span>Focused preview context</span>
        <small>{selection.limit_message}</small>
        <button type="button" onClick={() => void actions.clearSelection()}>
          Clear selection
        </button>
      </header>
      <div className="selection-chips">
        {selection.targets.map((target) => (
          <span
            className={`selection-chip${target.primary ? " primary" : ""}${
              target.stale ? " stale" : ""
            }`}
            key={target.selection_id}
            title={`${target.document_name ?? "Document"} · ${target.path}`}
          >
            {target.primary ? <small>Primary</small> : null}
            <strong>
              {target.label || target.path}
              {target.stale ? " · stale, reselect" : ""}
            </strong>
            <button
              type="button"
              aria-label={`Remove selected target ${target.label || target.path}`}
              onClick={() => void actions.removeSelection(target.selection_id)}
            >
              <CloseIcon />
            </button>
          </span>
        ))}
      </div>
    </section>
  );
}
