import type { ActivityItem, RunOutcome } from "../../types";

export function ActivityPanel({
  activity,
  outcome,
}: {
  activity: ActivityItem[];
  outcome: RunOutcome | string;
}) {
  return (
    <details className="activity-panel">
      <summary>
        <span className={`run-status ${outcome}`} aria-hidden="true" />
        <span>Agent activity</span>
        <small>{activity.length ? `${activity.length} updates` : "No run yet"}</small>
      </summary>
      <div className="activity-log" aria-label="Agent activity log">
        {activity.length ? (
          activity.map((item) => (
            <p key={item.id}>
              <span>[{item.stream}]</span> {item.text}
            </p>
          ))
        ) : (
          <p>Run activity will appear here.</p>
        )}
      </div>
    </details>
  );
}
