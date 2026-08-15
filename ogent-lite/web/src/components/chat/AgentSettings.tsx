import type { AgentSelection } from "../../hooks/useAgentSelection";
import type { AgentCapabilities } from "../../types";
import { titleCase } from "../../lib/format";
import { RefreshIcon } from "../icons";

interface AgentSettingsProps {
  selection: AgentSelection;
  capabilities: AgentCapabilities;
}

export function AgentSettings({
  selection,
  capabilities,
}: AgentSettingsProps) {
  return (
    <div className="agent-control">
      <div className="agent-settings" aria-label="Agent settings">
        <label>
          <span>Agent</span>
          <select
            aria-label="AI agent provider"
            value={selection.providerId}
            onChange={(event) => selection.setProviderId(event.target.value)}
            disabled={!capabilities.providers.length}
          >
            {capabilities.providers.map((provider) => (
              <option key={provider.id} value={provider.id}>
                {provider.label}
                {provider.live && provider.status === "ready"
                  ? ""
                  : ` · ${provider.status.replaceAll("_", " ")}`}
              </option>
            ))}
          </select>
        </label>
        <label>
          <span>Model</span>
          <select
            aria-label="AI model"
            value={selection.modelId}
            onChange={(event) => selection.setModelId(event.target.value)}
            disabled={selection.fast || !selection.provider?.models?.length}
          >
            {(selection.provider?.models ?? []).map((model) => (
              <option key={model.id} value={model.id}>
                {model.displayName || model.id}
              </option>
            ))}
          </select>
        </label>
        <label>
          <span>Effort</span>
          <select
            aria-label="Model effort"
            value={selection.effort}
            onChange={(event) => selection.setEffort(event.target.value)}
            disabled={selection.fast || !selection.model}
          >
            <option value="automatic">Automatic · CLI default</option>
            {(selection.model?.efforts ?? []).map((effort) => (
              <option key={effort} value={effort}>
                {titleCase(effort)}
              </option>
            ))}
          </select>
        </label>
        <label className="fast-toggle">
          <span>Fast</span>
          <input
            type="checkbox"
            aria-label="Fast mode: use this provider's low-latency model and effort"
            title="On: the provider's documented low-latency model and effort with a smaller retrieved context. Off: your selections."
            checked={selection.fast}
            onChange={(event) => selection.setFast(event.target.checked)}
          />
        </label>
        <button
          className="agent-refresh"
          type="button"
          aria-label="Refresh models and efforts"
          title="Refresh models and efforts"
          disabled={selection.refreshing}
          onClick={() => void selection.refresh()}
        >
          <RefreshIcon />
        </button>
      </div>
      <p
        className={`agent-status${selection.ready ? " ready" : " error"}`}
        role="status"
      >
        {selection.status}
      </p>
    </div>
  );
}
