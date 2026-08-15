"""Stop temp-rooted owned runtime services before deleting their directory."""

from __future__ import annotations

from typing import Any


def stop_owned_runtime(ogent: Any) -> None:
    """Stop coordinators holding SQLite/temp files so cleanup cannot race.

    Mirrors the production ``cleanup()`` ordering. Without this, a worker can
    keep ``ogent-state-v1.sqlite3`` open past TemporaryDirectory cleanup on
    slow runners (WinError 32).
    """
    for owned in (
        getattr(ogent, "DOCUMENT_INTELLIGENCE", None),
        getattr(ogent, "REFERENCE_INDEX_COORDINATOR", None),
    ):
        if owned is not None:
            try:
                owned.stop()
            except RuntimeError:
                continue
    actors = getattr(ogent, "WORKSPACE_ACTORS", None)
    if actors is not None:
        try:
            actors.stop_all()
        except RuntimeError:
            pass
