"""The read-only `--repo-overview` command: the Mappings table of the GUI as text or JSON.

It uses the same rows as the GUI (`gui_data.load_repo_table`), so the two always agree. Nothing is written.
"""
from typing import Any, Optional

from modules import gui_data, repo_overview

# (key in the row dict, heading) in the order of the text table.
COLUMNS: tuple[tuple[str, str], ...] = (
    ("repository", "Repository"),
    ("status", "Status"),
    ("tag", "Tag"),
    ("last_check", "Last check"),
    ("step", "Step"),
    ("next_check", "Next check"),
    ("files", "Files"),
    ("limit", "Limit"),
    ("last_finalized", "Last finalized"),
    ("last_finalized_file_count", "Finalized files"),
)


def build_overview(table: gui_data.RepoTable) -> list[dict[str, Any]]:
    """Return one dict per repository (the table's order: most recently worked-on first).

    `last_finalized_file_count` is a number, or None when the daemon has not stored one yet.
    """
    result: list[dict[str, Any]] = []
    for row in table.rows:
        entry = table.entries.get(row.repo, {})
        count = entry.get("last_finalized_file_count")
        known = isinstance(count, int) and not isinstance(count, bool) and count > 0
        result.append(
            {
                "repository": row.repo,
                "folder": row.folder,
                "destination": row.destination,
                "status": row.status,
                "tag": row.tag,
                "last_check": row.last_check,
                "step": row.step,
                "next_check": row.next_check,
                "files": row.files,
                "limit": row.limit,
                "last_finalized": str(entry.get("last_finalized") or "") or repo_overview.NO_VALUE,
                "last_finalized_file_count": count if known else None,
            }
        )
    return result


def collect() -> dict[str, Any]:
    """Load mapping.json and state.db and return {"repositories": [...], "db_error": text or None}."""
    table = gui_data.load_repo_table()
    return {"repositories": build_overview(table), "db_error": table.db_error}


def _cell(row: dict[str, Any], key: str) -> str:
    """Return the text of one cell; an unknown file count reads "-"."""
    value = row.get(key)
    return repo_overview.NO_VALUE if value is None or value == "" else str(value)


def format_report(report: dict[str, Any], repo_filter: Optional[str] = None) -> str:
    """Return the overview as an aligned text table (the optional filter is a case-insensitive part of the name)."""
    rows = [row for row in report["repositories"] if not repo_filter or repo_filter.lower() in row["repository"].lower()]
    lines: list[str] = []
    if report.get("db_error"):
        lines.append(f"Warning: {report['db_error']}")
    if not rows:
        lines.append("No repositories to show.")
        return "\n".join(lines)
    widths = [
        max(len(heading), *(len(_cell(row, key)) for row in rows)) for key, heading in COLUMNS
    ]
    lines.append("  ".join(heading.ljust(width) for (_, heading), width in zip(COLUMNS, widths)).rstrip())
    lines.append("  ".join("-" * width for width in widths))
    for row in rows:
        lines.append("  ".join(_cell(row, key).ljust(width) for (key, _), width in zip(COLUMNS, widths)).rstrip())
    lines.append(f"{len(rows)} repositor{'y' if len(rows) == 1 else 'ies'}.")
    return "\n".join(lines)
