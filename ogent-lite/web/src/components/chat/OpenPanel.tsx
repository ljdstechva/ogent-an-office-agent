import { useRef, useState } from "react";

import type { OgentActions } from "../../hooks/useOgentActions";
import type { WorkspaceSnapshot } from "../../types";

interface OpenPanelProps {
  workspace: WorkspaceSnapshot;
  actions: OgentActions;
}

export function OpenPanel({ workspace, actions }: OpenPanelProps) {
  const [path, setPath] = useState("");
  const fileRef = useRef<HTMLInputElement>(null);
  const body = (
    <>
      <button
        className="drop-target"
        type="button"
        disabled={actions.uploadingDocument}
        onClick={() => fileRef.current?.click()}
        onDragOver={(event) => {
          event.preventDefault();
          event.dataTransfer.dropEffect = "copy";
        }}
        onDrop={(event) => {
          event.preventDefault();
          const file = event.dataTransfer.files[0];
          if (file) void actions.uploadDocument(file);
        }}
      >
        <strong>
          {actions.uploadingDocument ? "Importing file…" : "Drop a file here"}
        </strong>
        <span>or click to choose · DOCX, XLSX, PPTX, PDF</span>
      </button>
      <input
        ref={fileRef}
        className="file-input"
        type="file"
        aria-label="Choose a document to import"
        accept=".docx,.xlsx,.pptx,.pdf"
        onChange={(event) => {
          const file = event.target.files?.[0];
          if (file) void actions.uploadDocument(file);
          event.target.value = "";
        }}
      />
      <div className="open-divider">or open by path</div>
      <div className="open-line">
        <input
          className="path-field"
          value={path}
          onChange={(event) => setPath(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") void actions.openPath(path);
          }}
          type="text"
          placeholder="D:\Reports\document.docx"
          autoComplete="off"
          aria-label="Local document path"
        />
        <button
          className="secondary-button"
          type="button"
          onClick={async () => {
            const selected = await actions.browsePath();
            if (selected) setPath(selected);
          }}
        >
          Browse…
        </button>
        <button
          className="primary-button"
          type="button"
          disabled={actions.opening}
          onClick={() => void actions.openPath(path)}
        >
          {actions.opening ? "Opening…" : "Open"}
        </button>
      </div>
      <label className="recent-field">
        <span className="sr-only">Recent documents</span>
        <select
          value=""
          onChange={(event) => {
            if (event.target.value) setPath(event.target.value);
          }}
        >
          <option value="">Recent documents</option>
          {workspace.recent.map((item) => (
            <option key={item} value={item}>
              {item.split(/[\\/]/).pop() || item}
            </option>
          ))}
        </select>
      </label>
    </>
  );
  if (workspace.active_document) {
    return (
      <details className="open-panel is-collapsible">
        <summary>Open another document…</summary>
        <div className="open-panel-body">{body}</div>
      </details>
    );
  }
  return (
    <section className="open-panel" aria-label="Open document">
      {body}
    </section>
  );
}
