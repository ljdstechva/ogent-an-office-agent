import type { CoverageReview, DocumentIndex } from "../../types";

interface CoveragePanelProps {
  coverage: CoverageReview | null;
  index: DocumentIndex | null;
}

export function CoveragePanel({ coverage, index }: CoveragePanelProps) {
  if (!index) {
    return (
      <div className="panel-state">
        Open a document to inspect indexing and review coverage.
      </div>
    );
  }
  if (index.status !== "complete") {
    return (
      <div className="coverage-summary partial">
        <strong>Coverage is not yet complete</strong>
        <p>
          {index.status === "failed"
            ? "The structural index failed. Ogent will not claim whole-document coverage."
            : `${index.indexed_nodes} of ${index.total_estimate || "estimated"} nodes indexed. Whole-document work remains gated.`}
        </p>
        <progress
          max={1}
          value={Math.max(0, Math.min(1, index.progress || 0))}
          aria-label="Document indexing progress"
        />
      </div>
    );
  }
  if (!coverage?.required) {
    return (
      <div className="panel-state">
        The current index is complete. A whole-document run will create an exact
        structural coverage ledger.
      </div>
    );
  }
  return (
    <div className="coverage-panel">
      <div
        className={`coverage-summary${coverage.complete ? " complete" : " partial"}`}
      >
        <strong>
          {coverage.complete ? "Coverage complete" : "Coverage incomplete"}
        </strong>
        <p>{coverage.disclosure}</p>
      </div>
      <div className="coverage-categories">
        {coverage.categories.map((category) => {
          const ratio = category.required
            ? category.reviewed / category.required
            : 1;
          return (
            <section key={category.category}>
              <div>
                <strong>{category.category.replaceAll("_", " ")}</strong>
                <span>
                  {category.reviewed}/{category.required}
                </span>
              </div>
              <progress
                max={1}
                value={Math.max(0, Math.min(1, ratio))}
                aria-label={`${category.category} coverage`}
              />
            </section>
          );
        })}
      </div>
      {coverage.unsupported.length ? (
        <section className="coverage-exceptions">
          <h3>Unreadable or unsupported</h3>
          <ul>
            {coverage.unsupported.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        </section>
      ) : null}
    </div>
  );
}
