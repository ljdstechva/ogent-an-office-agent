import { useState } from "react";

import type { OgentActions } from "../../hooks/useOgentActions";
import { Dialog } from "../Dialog";

export function NewChatDialog({
  open,
  onClose,
  actions,
}: {
  open: boolean;
  onClose: () => void;
  actions: OgentActions;
}) {
  const [busy, setBusy] = useState(false);
  return (
    <Dialog
      open={open}
      onClose={() => {
        if (!busy) onClose();
      }}
      title="Start a new chat?"
      description="This permanently clears the chat and AI memory for only this document."
    >
      <div className="dialog-body">
        <ul>
          <li>
            Chat messages, retained attachments, and submitted selection history
            are cleared.
          </li>
          <li>
            The document, edits, recovery backup, and Live View position are
            preserved.
          </li>
          <li>Other open documents and their chats are not changed.</li>
        </ul>
        <div className="dialog-actions">
          <button
            className="secondary-button"
            type="button"
            disabled={busy}
            onClick={onClose}
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
                await actions.resetConversation();
                onClose();
              } finally {
                setBusy(false);
              }
            }}
          >
            {busy ? "Clearing…" : "Start new chat"}
          </button>
        </div>
      </div>
    </Dialog>
  );
}
