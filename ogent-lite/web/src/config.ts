export interface OgentBootstrapConfig {
  token: string;
  sessionId: string;
  version: string;
}

declare global {
  interface Window {
    __OGENT_CONFIG__?: OgentBootstrapConfig;
  }
}

function readConfig(): OgentBootstrapConfig {
  const value = window.__OGENT_CONFIG__;
  if (
    !value ||
    typeof value.token !== "string" ||
    typeof value.sessionId !== "string" ||
    typeof value.version !== "string"
  ) {
    throw new Error("Ogent bootstrap configuration is unavailable.");
  }
  return Object.freeze({ ...value });
}

export const config = readConfig();

export const clientId =
  globalThis.crypto?.randomUUID?.() ??
  `${Date.now().toString(16)}-${Math.random().toString(16).slice(2)}`;
