import type { SVGProps } from "react";

type IconProps = SVGProps<SVGSVGElement>;

function SvgIcon({
  children,
  ...props
}: IconProps & { children: React.ReactNode }) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
      {...props}
    >
      {children}
    </svg>
  );
}

export function OgentMark({ className }: { className?: string }) {
  return (
    <svg
      className={className}
      viewBox="0 0 256 256"
      aria-hidden="true"
      focusable="false"
    >
      <defs>
        <linearGradient id="ogent-mark-gradient" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor="#17324d" />
          <stop offset="1" stopColor="#0d9488" />
        </linearGradient>
      </defs>
      <rect x="8" y="8" width="240" height="240" rx="56" fill="url(#ogent-mark-gradient)" />
      <circle cx="128" cy="120" r="66" fill="none" stroke="#fff" strokeWidth="30" />
      <circle cx="175" cy="167" r="16" fill="#14b8a6" stroke="#fff" strokeWidth="3" />
    </svg>
  );
}

export function SettingsIcon(props: IconProps) {
  return (
    <SvgIcon {...props}>
      <circle cx="12" cy="12" r="3.2" />
      <path d="M19.4 15a1.7 1.7 0 0 0 .34 1.88l.06.06-2.86 2.86-.06-.06A1.7 1.7 0 0 0 15 19.4a1.7 1.7 0 0 0-1.4 1.6H9.55A1.7 1.7 0 0 0 8 19.4a1.7 1.7 0 0 0-1.88.34l-.06.06-2.86-2.86.06-.06A1.7 1.7 0 0 0 3.6 15 1.7 1.7 0 0 0 2 13.55V10A1.7 1.7 0 0 0 3.6 9a1.7 1.7 0 0 0-.34-1.88l-.06-.06L6.06 4.2l.06.06A1.7 1.7 0 0 0 8 4.6 1.7 1.7 0 0 0 9.55 3h4.05A1.7 1.7 0 0 0 15 4.6a1.7 1.7 0 0 0 1.88-.34l.06-.06 2.86 2.86-.06.06A1.7 1.7 0 0 0 19.4 9 1.7 1.7 0 0 0 21 10.45V14a1.7 1.7 0 0 0-1.6 1Z" />
    </SvgIcon>
  );
}

export function MapIcon(props: IconProps) {
  return (
    <SvgIcon {...props}>
      <path d="m3 6 5-2 8 3 5-2v13l-5 2-8-3-5 2Z" />
      <path d="M8 4v13M16 7v13" />
    </SvgIcon>
  );
}

export function ContextIcon(props: IconProps) {
  return (
    <SvgIcon {...props}>
      <circle cx="11" cy="11" r="6.5" />
      <path d="m16 16 4 4M8.5 9h5M8.5 12h3.5" />
    </SvgIcon>
  );
}

export function CoverageIcon(props: IconProps) {
  return (
    <SvgIcon {...props}>
      <path d="M4 19V9M10 19V5M16 19v-7M22 19H2" />
      <path d="m4 7 6-4 6 6 5-5" />
    </SvgIcon>
  );
}

export function ReviewIcon(props: IconProps) {
  return (
    <SvgIcon {...props}>
      <path d="M5 3h11l3 3v15H5Z" />
      <path d="M16 3v4h4M8 11h8M8 15h5" />
      <path d="m8 18 1.2 1.2L12 16.4" />
    </SvgIcon>
  );
}

export function RefreshIcon(props: IconProps) {
  return (
    <SvgIcon {...props}>
      <path d="M20 6v5h-5M4 18v-5h5" />
      <path d="M18.2 9A7 7 0 0 0 6.4 6.4L4 9M5.8 15A7 7 0 0 0 17.6 17.6L20 15" />
    </SvgIcon>
  );
}

export function CloseIcon(props: IconProps) {
  return (
    <SvgIcon {...props}>
      <path d="m6 6 12 12M18 6 6 18" />
    </SvgIcon>
  );
}

export function AttachmentIcon(props: IconProps) {
  return (
    <SvgIcon {...props}>
      <path d="m20 11.5-8.6 8.6a5 5 0 0 1-7.1-7.1l9.2-9.2a3.5 3.5 0 0 1 5 5L9.2 18a2 2 0 0 1-2.8-2.8l8.5-8.5" />
    </SvgIcon>
  );
}

export function ChevronIcon(props: IconProps) {
  return (
    <SvgIcon {...props}>
      <path d="m9 6 6 6-6 6" />
    </SvgIcon>
  );
}

export function UndoIcon(props: IconProps) {
  return (
    <SvgIcon {...props}>
      <path d="M9 7 4 12l5 5" />
      <path d="M5 12h8a6 6 0 0 1 6 6" />
    </SvgIcon>
  );
}
