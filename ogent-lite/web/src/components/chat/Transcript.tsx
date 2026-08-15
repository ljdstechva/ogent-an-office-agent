import { useVirtualizer } from "@tanstack/react-virtual";
import { useEffect, useMemo, useRef } from "react";

import { api } from "../../api/client";
import { humanFileSize } from "../../lib/format";
import type {
  AssistantStream,
  TranscriptMessage,
  WorkspaceSnapshot,
} from "../../types";
import { AttachmentIcon } from "../icons";

interface TranscriptProps {
  messages: TranscriptMessage[];
  stream: AssistantStream | null | undefined;
  activeDocument: string | null;
  update: (value: Partial<WorkspaceSnapshot>) => void;
  notify: (message: string) => void;
}

function MessageContext({
  message,
  update,
  notify,
}: {
  message: TranscriptMessage;
  update: TranscriptProps["update"];
  notify: TranscriptProps["notify"];
}) {
  const attachments = message.attachments ?? [];
  const selections = message.preview_selections ?? [];
  if (!attachments.length && !selections.length) return null;
  return (
    <div className="message-context">
      {attachments.map((item) => (
        <span className="message-context-card attachment" key={item.id}>
          <AttachmentIcon />
          <span>
            {item.filename} · {item.detected_type ?? item.kind ?? "File"} ·{" "}
            {humanFileSize(item.size)}
          </span>
        </span>
      ))}
      {selections.map((item) => {
        const canFocus =
          message.role === "user" &&
          Number.isSafeInteger(Number(message.sequence)) &&
          /^[0-9a-f]{32}$/.test(item.selection_id);
        const label = item.label || item.path;
        if (!canFocus) {
          return (
            <span className="message-context-card selection" key={item.selection_id}>
              {label}
            </span>
          );
        }
        return (
          <button
            className="message-context-card selection"
            type="button"
            key={item.selection_id}
            onClick={async () => {
              try {
                const result = await api<{
                  preview_selection: WorkspaceSnapshot["preview_selection"];
                  message?: string;
                }>("/selection/focus", {
                  method: "POST",
                  body: JSON.stringify({
                    turn_sequence: message.sequence,
                    selection_id: item.selection_id,
                  }),
                });
                update({ preview_selection: result.preview_selection });
                notify(result.message ?? `Focused ${label}.`);
              } catch (error) {
                notify(
                  error instanceof Error
                    ? error.message
                    : "The historical selection could not be focused.",
                );
              }
            }}
          >
            {label}
            <span aria-hidden="true">⌖</span>
          </button>
        );
      })}
    </div>
  );
}

export function Transcript({
  messages,
  stream,
  activeDocument,
  update,
  notify,
}: TranscriptProps) {
  const parentRef = useRef<HTMLDivElement>(null);
  const rows = useMemo(() => {
    const values: Array<
      | { type: "message"; value: TranscriptMessage; key: string }
      | { type: "stream"; value: AssistantStream; key: string }
    > = messages.map((message, index) => ({
      type: "message",
      value: message,
      key: message.turn_id ?? `message-${message.sequence ?? index}`,
    }));
    if (
      stream?.run_id &&
      stream.text &&
      !messages.some(
        (message) =>
          message.role === "assistant" && message.text === stream.text,
      )
    ) {
      values.push({
        type: "stream",
        value: stream,
        key: `stream-${stream.run_id}`,
      });
    }
    return values;
  }, [messages, stream]);
  const virtualizer = useVirtualizer({
    useFlushSync: false,
    count: rows.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => 92,
    measureElement: (element) => element.getBoundingClientRect().height,
    overscan: 8,
  });

  useEffect(() => {
    if (rows.length) {
      virtualizer.scrollToIndex(rows.length - 1, { align: "end" });
    }
  }, [rows.length, stream?.character_count, virtualizer]);

  if (!rows.length) {
    const name = activeDocument?.split(/[\\/]/).pop();
    return (
      <section
        className="transcript transcript-empty"
        aria-label="Conversation"
        tabIndex={0}
      >
        <div className="chat-empty-state">
          <strong>{name ? `New chat for ${name}` : "Open a document to begin"}</strong>
          <p>
            {name
              ? "Ask a question, select document content, or describe an edit."
              : "Ogent keeps each document conversation in its own durable workspace."}
          </p>
        </div>
      </section>
    );
  }

  return (
    <section
      className="transcript"
      ref={parentRef}
      aria-label="Conversation"
      aria-live="polite"
      tabIndex={0}
    >
      <div
        className="virtual-transcript-canvas"
        style={{ height: `${virtualizer.getTotalSize()}px` }}
      >
        {virtualizer.getVirtualItems().map((virtualItem) => {
          const row = rows[virtualItem.index];
          const role =
            row.type === "stream" ? "assistant" : row.value.role;
          const text = row.value.text;
          return (
            <article
              key={row.key}
              ref={virtualizer.measureElement}
              data-index={virtualItem.index}
              className={`message ${role === "user" ? "user" : "assistant"}${
                row.type === "stream" ? " provisional" : ""
              }`}
              style={{ transform: `translateY(${virtualItem.start}px)` }}
            >
              <div className="bubble">
                <div className="message-text">{text}</div>
                {row.type === "message" ? (
                  <MessageContext
                    message={row.value}
                    update={update}
                    notify={notify}
                  />
                ) : null}
                {row.type === "stream" && row.value.status === "streaming" ? (
                  <span className="stream-caret" aria-label="Assistant is still responding" />
                ) : null}
              </div>
            </article>
          );
        })}
      </div>
    </section>
  );
}
