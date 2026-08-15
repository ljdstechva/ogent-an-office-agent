import { clientId, config } from "../config";

export class ApiError extends Error {
  readonly status: number;
  readonly payload: Record<string, unknown>;

  constructor(
    message: string,
    status: number,
    payload: Record<string, unknown> = {},
  ) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.payload = payload;
  }
}

export function scopedUrl(path: string): string {
  const value = new URL(path, window.location.origin);
  if (!value.searchParams.has("s")) {
    value.searchParams.set("s", config.sessionId);
  }
  return `${value.pathname}${value.search}`;
}

export async function api<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const headers = new Headers(options.headers);
  if (options.body && !(options.body instanceof FormData)) {
    headers.set("Content-Type", "application/json");
  }
  headers.set("X-Ogent-Token", config.token);
  headers.set("X-Ogent-Session", config.sessionId);
  headers.set("X-Ogent-Client", clientId);
  const response = await fetch(scopedUrl(path), { ...options, headers });
  let payload: Record<string, unknown> = {};
  if (response.status !== 204) {
    try {
      payload = (await response.json()) as Record<string, unknown>;
    } catch {
      payload = {};
    }
  }
  if (!response.ok) {
    throw new ApiError(
      typeof payload.error === "string"
        ? payload.error
        : `Request failed (${response.status}).`,
      response.status,
      payload,
    );
  }
  return payload as T;
}

export function eventStreamUrl(): string {
  const value = new URL("/events", window.location.origin);
  value.searchParams.set("s", config.sessionId);
  value.searchParams.set("token", config.token);
  value.searchParams.set("client", clientId);
  return value.href;
}

export function announceClose(): void {
  const value = new URL("/session/close", window.location.origin);
  value.searchParams.set("s", config.sessionId);
  value.searchParams.set("token", config.token);
  value.searchParams.set("client", clientId);
  navigator.sendBeacon(
    value.href,
    new Blob(["{}"], { type: "application/json" }),
  );
}
