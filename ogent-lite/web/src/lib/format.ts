export function fileName(path: string | null | undefined): string {
  return String(path ?? "").split(/[\\/]/).pop() || "";
}

export function humanFileSize(bytes: number | null | undefined): string {
  const value = Number(bytes ?? 0);
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

export function formatLocalDate(value: string | null | undefined): string {
  if (!value) return "—";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? "—" : parsed.toLocaleString();
}

export function modeLabel(mode: string | null | undefined): string {
  if (mode === "local_direct") {
    return "Editing original · recovery backup created";
  }
  if (mode === "browser_import") {
    return "Browser upload · editing an imported copy";
  }
  if (mode === "pdf_conversion") {
    return "Protected PDF conversion · editing working DOCX";
  }
  return "Ready for a protected open";
}

export function titleCase(value: string): string {
  return value
    .split(/[-_]/)
    .filter(Boolean)
    .map((part) => `${part.charAt(0).toUpperCase()}${part.slice(1)}`)
    .join(" ");
}
