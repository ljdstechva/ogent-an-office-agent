import type { RunPlan, RunStep } from "../../types";

interface PlanTimelineProps {
  plan: RunPlan | null | undefined;
  steps: RunStep[];
}

export function PlanTimeline({ plan, steps }: PlanTimelineProps) {
  if (!plan) return null;
  const completed = steps.filter((step) => step.state === "completed").length;
  const work = steps.reduce(
    (sum, step) => sum + Number(step.estimated_work_units || 1),
    0,
  );
  const completedWork = steps.reduce(
    (sum, step) =>
      sum +
      (step.state === "completed"
        ? Number(step.estimated_work_units || 1)
        : 0),
    0,
  );
  const active = steps.some(
    (step) => step.state === "running" || step.state === "pending",
  );
  return (
    <details className="plan-timeline" open={active}>
      <summary>
        <span>
          <strong>Run plan</strong>
          <small>
            {plan.complexity.replaceAll("_", " ")} · {completed}/{steps.length} steps
          </small>
        </span>
        <progress
          max={Math.max(1, work)}
          value={completedWork}
          aria-label="Run plan progress"
        />
      </summary>
      <ol>
        {steps.map((step) => (
          <li key={step.id} data-state={step.state}>
            <span className="step-marker" aria-hidden="true" />
            <div>
              <strong>{step.description}</strong>
              <span>
                {step.state === "running"
                  ? "In progress"
                  : step.state === "completed"
                    ? step.proof
                    : step.state === "failed"
                      ? `Failed${step.error_code ? ` · ${step.error_code}` : ""}`
                      : step.state === "cancelled"
                        ? "Cancelled"
                        : step.dependencies.length
                          ? `Waiting for ${step.dependencies.join(", ")}`
                          : "Ready"}
              </span>
            </div>
          </li>
        ))}
      </ol>
    </details>
  );
}
