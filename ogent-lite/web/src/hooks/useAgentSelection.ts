import { useEffect, useMemo, useState } from "react";

import { api } from "../api/client";
import type {
  AgentCapabilities,
  ProviderCapability,
  ProviderModel,
} from "../types";

const settingsKey = "ogent-agent-settings-v2";

interface StoredSettings {
  provider: string | null;
  fast?: boolean;
  selections: Record<string, { model: string | null; effort: string }>;
}

export interface AgentSelection {
  providerId: string;
  modelId: string;
  effort: string;
  fast: boolean;
  provider: ProviderCapability | null;
  model: ProviderModel | null;
  ready: boolean;
  status: string;
  refreshing: boolean;
  setProviderId: (value: string) => void;
  setModelId: (value: string) => void;
  setEffort: (value: string) => void;
  setFast: (value: boolean) => void;
  refresh: () => Promise<void>;
}

function loadStored(): StoredSettings {
  try {
    const parsed = JSON.parse(
      localStorage.getItem(settingsKey) ?? "{}",
    ) as Partial<StoredSettings>;
    return {
      provider:
        typeof parsed.provider === "string" ? parsed.provider : null,
      fast: parsed.fast === true,
      selections:
        parsed.selections && typeof parsed.selections === "object"
          ? parsed.selections
          : {},
    };
  } catch {
    return { provider: null, fast: false, selections: {} };
  }
}

export function useAgentSelection(
  capabilities: AgentCapabilities,
  onCapabilities: (value: AgentCapabilities) => void,
  onError: (message: string) => void,
): AgentSelection {
  const [stored, setStored] = useState<StoredSettings>(loadStored);
  const [refreshing, setRefreshing] = useState(false);
  const providers = capabilities.providers ?? [];
  const providerId =
    (stored.provider &&
    providers.some((provider) => provider.id === stored.provider)
      ? stored.provider
      : providers.find(
          (provider) => provider.live && provider.status === "ready",
        )?.id) ??
    providers[0]?.id ??
    "";
  const provider =
    providers.find((candidate) => candidate.id === providerId) ?? null;
  const saved = stored.selections[providerId];
  const modelId =
    (saved?.model &&
    provider?.models?.some((model) => model.id === saved.model)
      ? saved.model
      : provider?.models?.[0]?.id) ?? "";
  const model =
    provider?.models?.find((candidate) => candidate.id === modelId) ?? null;
  const allowedEfforts = model?.efforts ?? [];
  const effort =
    saved?.effort &&
    (saved.effort === "automatic" || allowedEfforts.includes(saved.effort))
      ? saved.effort
      : "automatic";

  useEffect(() => {
    try {
      localStorage.setItem(settingsKey, JSON.stringify(stored));
    } catch {
      // Local persistence is optional; provider selection remains usable.
    }
  }, [stored]);

  const updateSelection = (
    key: "model" | "effort",
    value: string | null,
  ) => {
    setStored((current) => ({
      ...current,
      provider: providerId,
      selections: {
        ...current.selections,
        [providerId]: {
          model:
            key === "model"
              ? value
              : current.selections[providerId]?.model ?? modelId ?? null,
          effort:
            key === "effort"
              ? value ?? "automatic"
              : current.selections[providerId]?.effort ?? "automatic",
        },
      },
    }));
  };

  const status = useMemo(() => {
    if (!provider) {
      return capabilities.refreshing
        ? "Checking installed agent CLIs…"
        : "No agent provider was reported.";
    }
    if (!provider.live || provider.status !== "ready") {
      return provider.warning ?? `${provider.label} is not ready.`;
    }
    if (!model) return `${provider.label} did not report a selectable model.`;
    if (model.effortsVerified && !allowedEfforts.length) {
      return "Ready · this model uses the agent CLI default effort.";
    }
    return model.effortsVerified
      ? "Ready · model and effort support verified from the installed CLI."
      : "Ready · model list is live; effort support is verified before use.";
  }, [
    allowedEfforts.length,
    capabilities.refreshing,
    model,
    provider,
  ]);

  return {
    providerId,
    modelId,
    effort,
    fast: stored.fast === true,
    provider,
    model,
    ready: Boolean(
      provider?.live && provider.status === "ready" && model,
    ),
    status,
    refreshing,
    setProviderId: (value) =>
      setStored((current) => ({ ...current, provider: value })),
    setModelId: (value) => updateSelection("model", value),
    setEffort: (value) => updateSelection("effort", value),
    setFast: (value) =>
      setStored((current) => ({ ...current, fast: value === true })),
    refresh: async () => {
      if (refreshing) return;
      setRefreshing(true);
      try {
        const result = await api<AgentCapabilities>(
          "/api/agent-capabilities/refresh",
          {
            method: "POST",
            body: JSON.stringify({ provider: providerId || null }),
          },
        );
        onCapabilities(result);
      } catch (error) {
        onError(
          error instanceof Error
            ? error.message
            : "Agent capabilities could not be refreshed.",
        );
      } finally {
        setRefreshing(false);
      }
    },
  };
}
