import { useRef } from "react";

export function Splitter() {
  const draggingRef = useRef(false);
  return (
    <div
      className="splitter"
      role="separator"
      aria-orientation="vertical"
      aria-label="Resize document and chat panes"
      aria-valuemin={45}
      aria-valuemax={82}
      aria-valuenow={68}
      tabIndex={0}
      onPointerDown={(event) => {
        draggingRef.current = true;
        event.currentTarget.setPointerCapture(event.pointerId);
        event.currentTarget.classList.add("dragging");
      }}
      onPointerMove={(event) => {
        if (!draggingRef.current) return;
        const percent = Math.max(
          45,
          Math.min(82, (event.clientX / window.innerWidth) * 100),
        );
        document.documentElement.style.setProperty("--document-pane", `${percent}%`);
        event.currentTarget.setAttribute("aria-valuenow", String(Math.round(percent)));
      }}
      onPointerUp={(event) => {
        draggingRef.current = false;
        event.currentTarget.classList.remove("dragging");
        event.currentTarget.releasePointerCapture(event.pointerId);
      }}
      onKeyDown={(event) => {
        if (!["ArrowLeft", "ArrowRight"].includes(event.key)) return;
        event.preventDefault();
        const current = Number.parseFloat(
          getComputedStyle(document.documentElement)
            .getPropertyValue("--document-pane")
            .replace("%", ""),
        );
        const next = Math.max(
          45,
          Math.min(82, current + (event.key === "ArrowRight" ? 2 : -2)),
        );
        document.documentElement.style.setProperty("--document-pane", `${next}%`);
        event.currentTarget.setAttribute("aria-valuenow", String(next));
      }}
    />
  );
}
