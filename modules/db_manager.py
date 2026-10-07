import re
import os
import sqlite3
from typing import Iterable, Optional

STATE_DB_NAME = "state.db"


def get_state_db_path():
    """Return the SQLite state database path beside the app files."""
    app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(app_dir, STATE_DB_NAME)


def purge_state_database():
    """Delete the local state database if it exists."""
    state_db_path = get_state_db_path()
    if not os.path.exists(state_db_path):
        return False

    os.remove(state_db_path)
    return True


def open_database():
    """Open the SQLite database, enable WAL mode, and ensure the schema exists."""
    connection = sqlite3.connect(get_state_db_path())
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON;")
    
    # Enable Write-Ahead Logging for daemon and GUI concurrency.
    connection.execute("PRAGMA journal_mode=WAL;")
    
    # Store persistent metadata used for duplicate file prevention.
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS asset_state (
            release_key TEXT NOT NULL,
            item_key TEXT NOT NULL,
            file_name TEXT,
            file_path TEXT NOT NULL,
            size INTEGER,
            last_modified REAL,
            etag TEXT,
            expected_signature TEXT,
            local_size INTEGER,
            local_mtime REAL,
            PRIMARY KEY (release_key, item_key)
        )
        """
    )
    
    # Store queued jobs for re-checks and retries.
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS job_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            repo TEXT NOT NULL,
            tag TEXT NOT NULL,
            release_type TEXT,
            status TEXT NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING', 'COMPLETED', 'FAILED', 'SUPERSEDED')),
            attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
            next_check_time REAL NOT NULL,          -- Unix timestamp
            expected_commit TEXT,                   -- 7-char hash to detect overwrites
            downloaded_count INTEGER NOT NULL DEFAULT 0,
            skipped_count INTEGER NOT NULL DEFAULT 0,
            total_items INTEGER NOT NULL DEFAULT 0,
            working_dir TEXT,
            last_result TEXT,
            created_at REAL NOT NULL DEFAULT (CAST(strftime('%s', 'now') AS REAL)),
            updated_at REAL NOT NULL DEFAULT (CAST(strftime('%s', 'now') AS REAL)),
            completed_at REAL
        )
        """
    )

    # Store per-job skipped item details for queue observability/reporting.
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS job_skip_details (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id INTEGER NOT NULL,
            attempt_count INTEGER NOT NULL CHECK (attempt_count >= 1),
            item_key TEXT,
            file_name TEXT,
            reason TEXT NOT NULL,
            recorded_at REAL NOT NULL DEFAULT (CAST(strftime('%s', 'now') AS REAL)),
            FOREIGN KEY (job_id) REFERENCES job_queue(id) ON DELETE CASCADE
        )
        """
    )

    # Store lifecycle events (completed/partial moves, typed warnings, cycle
    # summaries) so CLI and GUI share one structured source instead of parsing
    # text log files. event_type is intentionally unconstrained (no CHECK) so
    # new event types can be added later without a table rebuild; the set of
    # valid values is governed by lifecycle_logger.py alone.
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS lifecycle_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            category TEXT,
            repo TEXT,
            tag TEXT,
            commit_hash TEXT,
            destination_path TEXT,
            message TEXT NOT NULL,
            created_at REAL NOT NULL DEFAULT (CAST(strftime('%s', 'now') AS REAL))
        )
        """
    )

    # Migrate away from the original restrictive event_type CHECK constraint
    # (SQLite can't ALTER a CHECK constraint in place, so rebuild the table).
    lifecycle_events_table_def = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='lifecycle_events'"
    ).fetchone()
    if lifecycle_events_table_def and "CHECK" in (lifecycle_events_table_def["sql"] or ""):
        connection.execute("ALTER TABLE lifecycle_events RENAME TO lifecycle_events_old")
        connection.execute(
            """
            CREATE TABLE lifecycle_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                category TEXT,
                repo TEXT,
                tag TEXT,
                commit_hash TEXT,
                destination_path TEXT,
                message TEXT NOT NULL,
                created_at REAL NOT NULL DEFAULT (CAST(strftime('%s', 'now') AS REAL))
            )
            """
        )
        connection.execute(
            """
            INSERT INTO lifecycle_events (
                id, event_type, category, repo, tag, commit_hash, destination_path, message, created_at
            )
            SELECT id, event_type, category, repo, tag, commit_hash, destination_path, message, created_at
            FROM lifecycle_events_old
            """
        )
        connection.execute("DROP TABLE lifecycle_events_old")

    # Indexes for queue polling and reporting performance.
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_job_queue_status_next_check_time
        ON job_queue(status, next_check_time)
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_job_queue_created_at
        ON job_queue(created_at)
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_job_queue_status_created_at
        ON job_queue(status, created_at)
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_job_skip_details_job_id
        ON job_skip_details(job_id)
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_job_skip_details_reason
        ON job_skip_details(reason)
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_lifecycle_events_created_at
        ON lifecycle_events(created_at)
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_lifecycle_events_event_type
        ON lifecycle_events(event_type)
        """
    )

    # Apply backward-compatible schema migrations for existing databases.
    existing_columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(job_queue)").fetchall()
    }
    if "downloaded_count" not in existing_columns:
        connection.execute(
            "ALTER TABLE job_queue ADD COLUMN downloaded_count INTEGER NOT NULL DEFAULT 0"
        )
    if "skipped_count" not in existing_columns:
        connection.execute(
            "ALTER TABLE job_queue ADD COLUMN skipped_count INTEGER NOT NULL DEFAULT 0"
        )
    if "total_items" not in existing_columns:
        connection.execute(
            "ALTER TABLE job_queue ADD COLUMN total_items INTEGER NOT NULL DEFAULT 0"
        )
    if "last_result" not in existing_columns:
        connection.execute(
            "ALTER TABLE job_queue ADD COLUMN last_result TEXT"
        )
    if "working_dir" not in existing_columns:
        connection.execute(
            "ALTER TABLE job_queue ADD COLUMN working_dir TEXT"
        )
    if "created_at" not in existing_columns:
        connection.execute(
            "ALTER TABLE job_queue ADD COLUMN created_at REAL"
        )
    if "updated_at" not in existing_columns:
        connection.execute(
            "ALTER TABLE job_queue ADD COLUMN updated_at REAL"
        )
    if "completed_at" not in existing_columns:
        connection.execute(
            "ALTER TABLE job_queue ADD COLUMN completed_at REAL"
        )

    # Backfill timestamps for rows created before timestamp columns existed.
    connection.execute(
        """
        UPDATE job_queue
        SET created_at = COALESCE(created_at, next_check_time, CAST(strftime('%s', 'now') AS REAL))
        WHERE created_at IS NULL
        """
    )
    connection.execute(
        """
        UPDATE job_queue
        SET updated_at = COALESCE(updated_at, created_at, next_check_time, CAST(strftime('%s', 'now') AS REAL))
        WHERE updated_at IS NULL
        """
    )

    # Single-row control channel for the running polling daemon (pause and
    # forced polls). Written by the GUI/CLI, read by the daemon; see
    # daemon_control.py for the semantics.
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS daemon_control (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            paused INTEGER NOT NULL DEFAULT 0,
            poll_now_request REAL
        )
        """
    )
    # Session override for terminal log mirroring: NULL = follow config.json,
    # 1 = force on, 0 = force off. Added in v1.1.1.
    control_columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(daemon_control)").fetchall()
    }
    if "log_override" not in control_columns:
        connection.execute("ALTER TABLE daemon_control ADD COLUMN log_override INTEGER")
    # Epoch stamp of the latest graceful-stop request (GUI/CLI --stop).
    if "stop_request" not in control_columns:
        connection.execute("ALTER TABLE daemon_control ADD COLUMN stop_request REAL")
    # Epoch stamp of the latest "check folders now" request (GUI button / CLI --check-folders).
    if "check_folders_request" not in control_columns:
        connection.execute("ALTER TABLE daemon_control ADD COLUMN check_folders_request REAL")

    # Latest folder count per repository (written by the daemon's limit checks, read by the GUI).
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS folder_counts (
            repo_key TEXT PRIMARY KEY,
            repo TEXT NOT NULL,
            folder_count INTEGER NOT NULL,
            counted_at REAL NOT NULL
        )
        """
    )

    connection.commit()
    return connection


def set_folder_count(connection, repo, folder_count, counted_at):
    """Remember how many release folders a repository had when they were last counted."""
    connection.execute(
        """
        INSERT INTO folder_counts (repo_key, repo, folder_count, counted_at) VALUES (?, ?, ?, ?)
        ON CONFLICT(repo_key) DO UPDATE SET
            repo = excluded.repo, folder_count = excluded.folder_count, counted_at = excluded.counted_at
        """,
        (str(repo).strip().lower(), str(repo).strip(), int(folder_count), float(counted_at)),
    )
    connection.commit()


def delete_folder_counts(connection, keep_repos=None, repo=None):
    """Delete one repository's count, or every count except those of `keep_repos` (names, any case)."""
    if repo is not None:
        cursor = connection.execute("DELETE FROM folder_counts WHERE repo_key = ?", (str(repo).strip().lower(),))
    else:
        keep = sorted({str(name).strip().lower() for name in (keep_repos or [])})
        if keep:
            cursor = connection.execute(
                f"DELETE FROM folder_counts WHERE repo_key NOT IN ({','.join('?' * len(keep))})", keep
            )
        else:
            cursor = connection.execute("DELETE FROM folder_counts")
    connection.commit()
    return cursor.rowcount


def get_folder_counts(connection):
    """{lower-cased repo: {"folder_count": int, "counted_at": float}} (read-only)."""
    rows = connection.execute("SELECT repo_key, folder_count, counted_at FROM folder_counts").fetchall()
    return {row[0]: {"folder_count": row[1], "counted_at": row[2]} for row in rows}


# Asset state helpers.

def load_release_state(connection, release_key):
    """Return stored asset state for a release key."""
    rows = connection.execute(
        """
        SELECT item_key, file_name, file_path, size, last_modified, etag,
               expected_signature, local_size, local_mtime
        FROM asset_state
        WHERE release_key = ?
        """,
        (release_key,),
    ).fetchall()

    state = {}
    for row in rows:
        state[row["item_key"]] = {
            "file_name": row["file_name"],
            "file_path": row["file_path"],
            "size": row["size"],
            "last_modified": row["last_modified"],
            "etag": row["etag"],
            "expected_signature": row["expected_signature"],
            "local_size": row["local_size"],
            "local_mtime": row["local_mtime"],
        }
    return state


def save_state_entry(connection, release_key, item_key, file_name, rel_file_path, expected_signature, remote_size, remote_last_modified, remote_etag, base_dir):
    """Insert or update one asset-state record using a relative path."""
    # Reconstruct the absolute path for local filesystem checks
    abs_path = os.path.join(base_dir, rel_file_path)
    try:
        local_size = os.path.getsize(abs_path)
        local_mtime = os.path.getmtime(abs_path)
    except OSError:
        local_size = None
        local_mtime = None

    connection.execute(
        """
        INSERT INTO asset_state (
            release_key, item_key, file_name, file_path, size, last_modified,
            etag, expected_signature, local_size, local_mtime
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(release_key, item_key) DO UPDATE SET
            file_name = excluded.file_name,
            file_path = excluded.file_path,
            size = excluded.size,
            last_modified = excluded.last_modified,
            etag = excluded.etag,
            expected_signature = excluded.expected_signature,
            local_size = excluded.local_size,
            local_mtime = excluded.local_mtime
        """,
        (release_key, item_key, file_name, rel_file_path, remote_size, remote_last_modified, remote_etag, expected_signature, local_size, local_mtime),
    )
    connection.commit()


def prune_release_state(connection, release_key, valid_item_keys, base_dir):
    """Delete stale asset-state rows using dynamically constructed absolute paths."""
    rows = connection.execute("SELECT item_key, file_path FROM asset_state WHERE release_key = ?", (release_key,)).fetchall()
    
    stale_keys = []
    for row in rows:
        item_key = row["item_key"]
        rel_path = row["file_path"]
        
        if not rel_path:
            stale_keys.append(item_key)
            continue
            
        abs_path = os.path.join(base_dir, rel_path)
        if item_key not in valid_item_keys or not os.path.exists(abs_path):
            stale_keys.append(item_key)

    if stale_keys:
        connection.executemany(
            "DELETE FROM asset_state WHERE release_key = ? AND item_key = ?",
            [(release_key, item_key) for item_key in stale_keys],
        )
        connection.commit()


# Queue state helpers.

def enqueue_job(connection, repo, tag, release_type=None, next_check_time=None, expected_commit=None):
    """Insert a new queued job row and return its row id."""
    if next_check_time is None:
        next_check_time = 0.0

    cursor = connection.execute(
        """
        INSERT INTO job_queue (
            repo,
            tag,
            release_type,
            status,
            attempt_count,
            next_check_time,
            expected_commit,
            downloaded_count,
            skipped_count,
            total_items,
            working_dir,
            last_result,
            created_at,
            updated_at,
            completed_at
        )
        VALUES (?, ?, ?, 'PENDING', 0, ?, ?, 0, 0, 0, NULL, NULL, CAST(strftime('%s', 'now') AS REAL), CAST(strftime('%s', 'now') AS REAL), NULL)
        """,
        (repo, tag, release_type, float(next_check_time), expected_commit),
    )
    connection.commit()
    return cursor.lastrowid


def get_pending_job_for_release(connection, repo, tag, release_type=None, expected_commit=None, exclude_job_id=None):
    """Return one pending job for the same release identity, optionally matching commit."""
    conditions = [
        "status = 'PENDING'",
        "repo = ?",
        "tag = ?",
        "(release_type = ? OR (release_type IS NULL AND ? IS NULL))",
    ]
    params = [repo, tag, release_type, release_type]

    if expected_commit is not None:
        conditions.append("expected_commit = ?")
        params.append(expected_commit)

    if exclude_job_id is not None:
        conditions.append("id <> ?")
        params.append(int(exclude_job_id))

    query = f"""
        SELECT
            id,
            repo,
            tag,
            release_type,
            status,
            attempt_count,
            next_check_time,
            created_at,
            expected_commit,
            downloaded_count,
            skipped_count,
            total_items,
            working_dir,
            last_result
        FROM job_queue
        WHERE {' AND '.join(conditions)}
        ORDER BY created_at ASC, id ASC
        LIMIT 1
    """

    return connection.execute(query, tuple(params)).fetchone()


def get_pending_jobs_for_release(connection, repo, tag, release_type=None):
    """Return pending jobs for one release identity ordered by creation time."""
    return connection.execute(
        """
        SELECT
            id,
            repo,
            tag,
            release_type,
            status,
            attempt_count,
            next_check_time,
            created_at,
            expected_commit,
            downloaded_count,
            skipped_count,
            total_items,
            working_dir,
            last_result
        FROM job_queue
        WHERE status = 'PENDING'
          AND repo = ?
          AND tag = ?
          AND (
                release_type = ?
                OR (release_type IS NULL AND ? IS NULL)
              )
        ORDER BY created_at ASC, id ASC
        """,
        (repo, tag, release_type, release_type),
    ).fetchall()


def supersede_pending_jobs_for_release(
    connection,
    repo,
    tag,
    release_type=None,
    replacement_expected_commit=None,
    last_result="SUPERSEDED_REPLACED_BY_NEW_NOTIFICATION",
):
    """Supersede pending jobs for a release identity and return affected row count."""
    cursor = connection.execute(
        """
        UPDATE job_queue
        SET status = 'SUPERSEDED',
            expected_commit = COALESCE(?, expected_commit),
            last_result = ?,
            updated_at = CAST(strftime('%s', 'now') AS REAL),
            completed_at = CAST(strftime('%s', 'now') AS REAL)
        WHERE status = 'PENDING'
          AND repo = ?
          AND tag = ?
          AND (
                release_type = ?
                OR (release_type IS NULL AND ? IS NULL)
              )
        """,
        (
            replacement_expected_commit,
            last_result,
            repo,
            tag,
            release_type,
            release_type,
        ),
    )
    connection.commit()
    return int(cursor.rowcount or 0)


def get_due_jobs(connection, now_timestamp, limit=None):
    """Return jobs that are ready to run, sorted by schedule time then id."""
    if limit is not None:
        return connection.execute(
            """
            SELECT
                id,
                repo,
                tag,
                release_type,
                status,
                attempt_count,
                next_check_time,
                created_at,
                expected_commit,
                downloaded_count,
                skipped_count,
                total_items,
                working_dir,
                last_result
            FROM job_queue
            WHERE status = 'PENDING' AND next_check_time <= ?
            ORDER BY next_check_time ASC, id ASC
            LIMIT ?
            """,
            (float(now_timestamp), int(limit)),
        ).fetchall()

    return connection.execute(
        """
        SELECT
            id,
            repo,
            tag,
            release_type,
            status,
            attempt_count,
            next_check_time,
            created_at,
            expected_commit,
            downloaded_count,
            skipped_count,
            total_items,
            working_dir,
            last_result
        FROM job_queue
        WHERE status = 'PENDING' AND next_check_time <= ?
        ORDER BY next_check_time ASC, id ASC
        """,
        (float(now_timestamp),),
    ).fetchall()


def get_pending_jobs(connection):
    """Return all pending queue jobs ordered by creation time."""
    return connection.execute(
        """
        SELECT
            id,
            repo,
            tag,
            release_type,
            status,
            attempt_count,
            next_check_time,
            created_at,
            expected_commit,
            downloaded_count,
            skipped_count,
            total_items,
            working_dir,
            last_result
        FROM job_queue
        WHERE status = 'PENDING'
        ORDER BY created_at ASC, id ASC
        """
    ).fetchall()


def get_next_pending_job(connection):
    """Return the next scheduled pending job, or None when queue is empty."""
    return connection.execute(
        """
        SELECT
            id,
            repo,
            tag,
            release_type,
            status,
            attempt_count,
            next_check_time,
            created_at,
            expected_commit,
            downloaded_count,
            skipped_count,
            total_items,
            working_dir,
            last_result
        FROM job_queue
        WHERE status = 'PENDING'
        ORDER BY next_check_time ASC, id ASC
        LIMIT 1
        """
    ).fetchone()


_LIMIT_WARNING_REPO = re.compile(r"Folder limit warning: (\S+) currently has ")


def purge_limit_warnings_for_repo(connection, repo, dry_run=False):
    """Delete the folder-limit warnings written for one repository; returns the count.

    The rows hold the repository only in their text, so they are matched on the message's fixed
    start (case-insensitive, no wildcard characters involved: repo names contain "_").
    """
    prefix = f"Folder limit warning: {repo} currently has "
    where = "category = 'LIMIT' AND LOWER(SUBSTR(message, 1, ?)) = LOWER(?)"
    params = (len(prefix), prefix)
    if dry_run:
        return int(connection.execute(f"SELECT COUNT(*) FROM lifecycle_events WHERE {where}", params).fetchone()[0])
    cursor = connection.execute(f"DELETE FROM lifecycle_events WHERE {where}", params)
    connection.commit()
    return cursor.rowcount


def get_repos_with_limit_warnings(connection):
    """Lower-cased owner/repo names that have at least one folder-limit warning stored (read-only).

    The warning rows carry the repository only in their text ("Folder limit warning: owner/repo currently
    has ..."), so it is read from there.
    """
    rows = connection.execute(
        "SELECT DISTINCT message FROM lifecycle_events WHERE category = 'LIMIT' AND message IS NOT NULL"
    ).fetchall()
    found = set()
    for (message,) in rows:
        match = _LIMIT_WARNING_REPO.match(message)
        if match:
            found.add(match.group(1).lower())
    return found


def get_repo_job_summaries(connection):
    """Return one read-only job_queue summary per repo, keyed by lower-cased owner/repo.

    Used by the GUI Mappings table. Per repo: latest_* come from its newest
    non-SUPERSEDED job (None when it only has superseded ones), pending_next_check / pending_attempts from its earliest PENDING job (None
    when nothing is pending), last_activity is the newest updated_at of any job.
    """
    summaries = {}

    rows = connection.execute(
        """
        SELECT repo, MAX(updated_at) AS last_activity
        FROM job_queue
        GROUP BY repo
        """
    ).fetchall()
    for row in rows:
        summaries[row["repo"].lower()] = {
            "last_activity": row["last_activity"],
            "latest_tag": None,
            "latest_release_type": None,
            "latest_status": None,
            "latest_updated_at": None,
            "latest_downloaded": 0,
            "latest_skipped": 0,
            "latest_total": 0,
            "pending_next_check": None,
            "pending_attempts": 0,
        }

    latest_rows = connection.execute(
        """
        SELECT repo, tag, release_type, status, updated_at, downloaded_count, skipped_count, total_items
        FROM job_queue
        WHERE id IN (
            SELECT MAX(id) FROM job_queue WHERE status != 'SUPERSEDED' GROUP BY repo
        )
        """
    ).fetchall()
    for row in latest_rows:
        summary = summaries[row["repo"].lower()]
        summary["latest_tag"] = row["tag"]
        summary["latest_release_type"] = row["release_type"]
        summary["latest_status"] = row["status"]
        summary["latest_updated_at"] = row["updated_at"]
        summary["latest_downloaded"] = row["downloaded_count"]
        summary["latest_skipped"] = row["skipped_count"]
        summary["latest_total"] = row["total_items"]

    pending_rows = connection.execute(
        """
        SELECT repo, next_check_time, attempt_count
        FROM job_queue
        WHERE status = 'PENDING'
        ORDER BY next_check_time DESC, id DESC
        """
    ).fetchall()
    # Ordered descending so the earliest pending job per repo is written last and wins.
    for row in pending_rows:
        summary = summaries[row["repo"].lower()]
        summary["pending_next_check"] = row["next_check_time"]
        summary["pending_attempts"] = row["attempt_count"]

    return summaries


def get_jobs_by_ids(connection, job_ids: Iterable[int]):
    """Return queue jobs for the provided IDs."""
    normalized_ids = sorted({int(job_id) for job_id in job_ids})
    if not normalized_ids:
        return []

    placeholders = ",".join(["?"] * len(normalized_ids))
    return connection.execute(
        f"""
        SELECT
            id,
            repo,
            tag,
            release_type,
            status,
            attempt_count,
            next_check_time,
            created_at,
            expected_commit,
            downloaded_count,
            skipped_count,
            total_items,
            working_dir,
            last_result
        FROM job_queue
        WHERE id IN ({placeholders})
        ORDER BY id ASC
        """,
        tuple(normalized_ids),
    ).fetchall()


def supersede_pending_jobs_by_ids(
    connection,
    job_ids: Iterable[int],
    last_result="SUPERSEDED_MANUAL_REMOVE",
):
    """Mark specific pending jobs as SUPERSEDED and return removed rows."""
    normalized_ids = sorted({int(job_id) for job_id in job_ids})
    if not normalized_ids:
        return []

    placeholders = ",".join(["?"] * len(normalized_ids))
    pending_rows = connection.execute(
        f"""
        SELECT
            id,
            repo,
            tag,
            release_type,
            status,
            attempt_count,
            expected_commit,
                        downloaded_count,
                        skipped_count,
                        total_items,
                        working_dir,
            last_result,
            next_check_time
        FROM job_queue
        WHERE status = 'PENDING'
          AND id IN ({placeholders})
        ORDER BY id ASC
        """,
        tuple(normalized_ids),
    ).fetchall()

    if not pending_rows:
        return []

    removable_ids = [int(row["id"]) for row in pending_rows]
    remove_placeholders = ",".join(["?"] * len(removable_ids))
    connection.execute(
        f"""
        UPDATE job_queue
        SET status = 'SUPERSEDED',
            last_result = ?,
            updated_at = CAST(strftime('%s', 'now') AS REAL),
            completed_at = CAST(strftime('%s', 'now') AS REAL)
        WHERE status = 'PENDING'
          AND id IN ({remove_placeholders})
        """,
        (last_result, *removable_ids),
    )
    connection.commit()
    return pending_rows


def supersede_duplicate_pending_jobs(connection):
    """Mark duplicate pending jobs as SUPERSEDED, keeping the oldest per release+commit."""
    pending_rows = connection.execute(
        """
        SELECT
            id,
            repo,
            tag,
            release_type,
            expected_commit,
            attempt_count,
            downloaded_count,
            skipped_count,
            total_items
        FROM job_queue
        WHERE status = 'PENDING'
        ORDER BY created_at ASC, id ASC
        """
    ).fetchall()

    seen_release_keys = set()
    duplicate_rows = []

    for row in pending_rows:
        release_key = (
            row["repo"],
            row["tag"],
            row["release_type"],
            row["expected_commit"],
        )
        if release_key in seen_release_keys:
            duplicate_rows.append(row)
            continue
        seen_release_keys.add(release_key)

    if not duplicate_rows:
        return 0

    for row in duplicate_rows:
        connection.execute(
            """
            UPDATE job_queue
            SET status = 'SUPERSEDED',
                attempt_count = ?,
                expected_commit = ?,
                downloaded_count = ?,
                skipped_count = ?,
                total_items = ?,
                last_result = 'SUPERSEDED_DUPLICATE_PENDING',
                updated_at = CAST(strftime('%s', 'now') AS REAL),
                completed_at = CAST(strftime('%s', 'now') AS REAL)
            WHERE id = ?
            """,
            (
                int(row["attempt_count"] or 0),
                row["expected_commit"],
                int(row["downloaded_count"] or 0),
                int(row["skipped_count"] or 0),
                int(row["total_items"] or 0),
                int(row["id"]),
            ),
        )

    connection.commit()
    return len(duplicate_rows)


def mark_job_completed(
    connection,
    job_id,
    attempt_count=None,
    downloaded_count=0,
    skipped_count=0,
    total_items=0,
    last_result="SUCCESS",
):
    """Mark a queued job as completed."""
    if attempt_count is None:
        connection.execute(
            """
            UPDATE job_queue
            SET status = 'COMPLETED',
                downloaded_count = ?,
                skipped_count = ?,
                total_items = ?,
                last_result = ?,
                updated_at = CAST(strftime('%s', 'now') AS REAL),
                completed_at = CAST(strftime('%s', 'now') AS REAL)
            WHERE id = ?
            """,
            (int(downloaded_count), int(skipped_count), int(total_items), last_result, job_id),
        )
    else:
        connection.execute(
            """
            UPDATE job_queue
            SET status = 'COMPLETED',
                attempt_count = ?,
                downloaded_count = ?,
                skipped_count = ?,
                total_items = ?,
                last_result = ?,
                updated_at = CAST(strftime('%s', 'now') AS REAL),
                completed_at = CAST(strftime('%s', 'now') AS REAL)
            WHERE id = ?
            """,
            (
                int(attempt_count),
                int(downloaded_count),
                int(skipped_count),
                int(total_items),
                last_result,
                job_id,
            ),
        )
    connection.commit()


def mark_job_failed(
    connection,
    job_id,
    attempt_count=None,
    expected_commit=None,
    downloaded_count=0,
    skipped_count=0,
    total_items=0,
    last_result="FAILED",
):
    """Mark a queued job as failed (no remaining retry intervals)."""
    if attempt_count is None:
        connection.execute(
            """
            UPDATE job_queue
            SET status = 'FAILED',
                expected_commit = ?,
                downloaded_count = ?,
                skipped_count = ?,
                total_items = ?,
                last_result = ?,
                updated_at = CAST(strftime('%s', 'now') AS REAL),
                completed_at = CAST(strftime('%s', 'now') AS REAL)
            WHERE id = ?
            """,
            (
                expected_commit,
                int(downloaded_count),
                int(skipped_count),
                int(total_items),
                last_result,
                job_id,
            ),
        )
    else:
        connection.execute(
            """
            UPDATE job_queue
            SET status = 'FAILED',
                attempt_count = ?,
                expected_commit = ?,
                downloaded_count = ?,
                skipped_count = ?,
                total_items = ?,
                last_result = ?,
                updated_at = CAST(strftime('%s', 'now') AS REAL),
                completed_at = CAST(strftime('%s', 'now') AS REAL)
            WHERE id = ?
            """,
            (
                int(attempt_count),
                expected_commit,
                int(downloaded_count),
                int(skipped_count),
                int(total_items),
                last_result,
                job_id,
            ),
        )
    connection.commit()


def mark_job_superseded(
    connection,
    job_id,
    attempt_count=None,
    expected_commit=None,
    downloaded_count=0,
    skipped_count=0,
    total_items=0,
    last_result="SUPERSEDED",
):
    """Mark a queued job as superseded by a newer commit/job."""
    if attempt_count is None:
        connection.execute(
            """
            UPDATE job_queue
            SET status = 'SUPERSEDED',
                expected_commit = ?,
                downloaded_count = ?,
                skipped_count = ?,
                total_items = ?,
                last_result = ?,
                updated_at = CAST(strftime('%s', 'now') AS REAL),
                completed_at = CAST(strftime('%s', 'now') AS REAL)
            WHERE id = ?
            """,
            (
                expected_commit,
                int(downloaded_count),
                int(skipped_count),
                int(total_items),
                last_result,
                job_id,
            ),
        )
    else:
        connection.execute(
            """
            UPDATE job_queue
            SET status = 'SUPERSEDED',
                attempt_count = ?,
                expected_commit = ?,
                downloaded_count = ?,
                skipped_count = ?,
                total_items = ?,
                last_result = ?,
                updated_at = CAST(strftime('%s', 'now') AS REAL),
                completed_at = CAST(strftime('%s', 'now') AS REAL)
            WHERE id = ?
            """,
            (
                int(attempt_count),
                expected_commit,
                int(downloaded_count),
                int(skipped_count),
                int(total_items),
                last_result,
                job_id,
            ),
        )
    connection.commit()


def reschedule_job(
    connection,
    job_id,
    next_check_time,
    attempt_count,
    expected_commit=None,
    downloaded_count=0,
    skipped_count=0,
    total_items=0,
    working_dir=None,
    last_result="RETRY",
):
    """Update a queued job with a new schedule and attempt counter."""
    connection.execute(
        """
        UPDATE job_queue
        SET status = 'PENDING',
            next_check_time = ?,
            attempt_count = ?,
            expected_commit = ?,
            downloaded_count = ?,
            skipped_count = ?,
            total_items = ?,
            working_dir = COALESCE(?, working_dir),
            last_result = ?,
            updated_at = CAST(strftime('%s', 'now') AS REAL),
            completed_at = NULL
        WHERE id = ?
        """,
        (
            float(next_check_time),
            int(attempt_count),
            expected_commit,
            int(downloaded_count),
            int(skipped_count),
            int(total_items),
            working_dir,
            last_result,
            job_id,
        ),
    )
    connection.commit()


def update_job_for_manual_check(
    connection,
    job_id,
    expected_commit=None,
    downloaded_count=0,
    skipped_count=0,
    total_items=0,
    working_dir=None,
    last_result="MANUAL_CHECK",
):
    """Update a pending job's counters and result without changing its retry schedule."""
    connection.execute(
        """
        UPDATE job_queue
        SET status = 'PENDING',
            expected_commit = ?,
            downloaded_count = ?,
            skipped_count = ?,
            total_items = ?,
            working_dir = COALESCE(?, working_dir),
            last_result = ?,
            updated_at = CAST(strftime('%s', 'now') AS REAL),
            completed_at = NULL
        WHERE id = ?
        """,
        (
            expected_commit,
            int(downloaded_count),
            int(skipped_count),
            int(total_items),
            working_dir,
            last_result,
            job_id,
        ),
    )
    connection.commit()


def save_job_skip_details(connection, job_id, attempt_count, skipped_items):
    """Persist skipped item details for a queue job attempt."""
    if not skipped_items:
        return

    rows = []
    for item in skipped_items:
        if not isinstance(item, dict):
            continue
        rows.append(
            (
                int(job_id),
                int(attempt_count),
                item.get("item_key"),
                item.get("file_name"),
                item.get("reason") or "unknown",
            )
        )

    if not rows:
        return

    connection.executemany(
        """
        INSERT INTO job_skip_details (
            job_id,
            attempt_count,
            item_key,
            file_name,
            reason
        )
        VALUES (?, ?, ?, ?, ?)
        """,
        rows,
    )
    connection.commit()


def get_previous_successful_completed_job(
    connection, repo, tag, release_type, exclude_job_id, match_tag=True, finished_before=None
):
    """Return the previous successful completed job for the same repo and release_type.

    With match_tag=True (the original behaviour) it must also have the same tag; with False the
    newest earlier successful job of the repository counts, whatever its tag. finished_before (a
    completed_at timestamp of the excluded job) limits the search to jobs that finished before it, which a
    report about an old job needs so it is not compared with a newer one.
    """
    tag_condition = "AND (tag = ? OR (tag IS NULL AND ? IS NULL))" if match_tag else ""
    params = [int(exclude_job_id), repo]
    if match_tag:
        params += [tag, tag]
    params += [release_type, release_type]
    time_condition = ""
    if finished_before is not None:
        # Jobs finishing in the same second are told apart by id (a later job has a higher id).
        time_condition = "AND (completed_at < ? OR (completed_at = ? AND id < ?))"
        params += [float(finished_before), float(finished_before), int(exclude_job_id)]
    return connection.execute(
        f"""
        SELECT
            id,
            repo,
            tag,
            release_type,
            total_items,
            downloaded_count,
            skipped_count,
            completed_at
        FROM job_queue
        WHERE id <> ?
          AND status = 'COMPLETED'
          AND last_result = 'SUCCESS'
          AND repo = ?
          {tag_condition}
          AND (
                release_type = ?
                OR (release_type IS NULL AND ? IS NULL)
              )
          {time_condition}
        ORDER BY completed_at DESC, id DESC
        LIMIT 1
        """,
        params,
    ).fetchone()


# Lifecycle event helpers.

def insert_lifecycle_event(
    connection,
    event_type,
    message,
    category=None,
    repo=None,
    tag=None,
    commit_hash=None,
    destination_path=None,
):
    """Insert one lifecycle event row and return its row id."""
    cursor = connection.execute(
        """
        INSERT INTO lifecycle_events (
            event_type, category, repo, tag, commit_hash, destination_path, message, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, CAST(strftime('%s', 'now') AS REAL))
        """,
        (event_type, category, repo, tag, commit_hash, destination_path, message),
    )
    connection.commit()
    return cursor.lastrowid


def get_lifecycle_events(connection, limit: Optional[int] = 20, event_type=None, repo_filter=None):
    """Return recent lifecycle events, newest first, optionally filtered."""
    conditions = []
    params: list = []

    if event_type:
        conditions.append("event_type = ?")
        params.append(event_type)

    if repo_filter:
        conditions.append("LOWER(repo) LIKE ?")
        params.append(f"%{repo_filter.lower()}%")

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    limit_clause = "" if limit is None else "LIMIT ?"
    if limit is not None:
        params.append(int(limit))

    query = f"""
        SELECT id, event_type, category, repo, tag, commit_hash, destination_path, message, created_at
        FROM lifecycle_events
        {where_clause}
        ORDER BY created_at DESC, id DESC
        {limit_clause}
    """
    return connection.execute(query, params).fetchall()


def get_existing_event_ids(connection, ids):
    """The subset of `ids` that still exists in lifecycle_events (read-only primary-key lookup)."""
    ids = [int(i) for i in ids]
    found = set()
    for start in range(0, len(ids), 500):
        chunk = ids[start:start + 500]
        rows = connection.execute(
            f"SELECT id FROM lifecycle_events WHERE id IN ({','.join('?' * len(chunk))})", chunk
        ).fetchall()
        found.update(row[0] for row in rows)
    return found


def _tab_event_filter(event_types, categories, exclude_categories):
    """WHERE conditions and params selecting the events of one GUI status tab."""
    conditions = [f"event_type IN ({','.join('?' * len(event_types))})"]
    params = list(event_types)
    if categories:
        conditions.append(f"category IN ({','.join('?' * len(categories))})")
        params.extend(categories)
    if exclude_categories:
        conditions.append(f"(category IS NULL OR category NOT IN ({','.join('?' * len(exclude_categories))}))")
        params.extend(exclude_categories)
    return conditions, params


def purge_events_for_tab(connection, event_types, categories=None, exclude_categories=(), dry_run=False):
    """Delete exactly the events a GUI status tab lists (same filter as get_events_for_tab); returns the count.

    With dry_run=True nothing is deleted and the number of matching events is returned.
    """
    conditions, params = _tab_event_filter(event_types, categories, exclude_categories)
    where = " AND ".join(conditions)
    if dry_run:
        return int(connection.execute(f"SELECT COUNT(*) FROM lifecycle_events WHERE {where}", params).fetchone()[0])
    cursor = connection.execute(f"DELETE FROM lifecycle_events WHERE {where}", params)
    connection.commit()
    return cursor.rowcount


def get_events_for_tab(
    connection,
    event_types,
    categories=None,
    exclude_categories=(),
    after_id=None,
    limit=500,
):
    """Return lifecycle events for one GUI status tab, newest first (read-only).

    event_types/categories are allow-lists (categories=None means any), exclude_categories drops
    events of those categories (events without a category are kept), after_id returns only events
    with a larger id, so a poll that finds nothing new is a cheap indexed query.
    """
    conditions, params = _tab_event_filter(event_types, categories, exclude_categories)
    if after_id is not None:
        conditions.append("id > ?")
        params.append(int(after_id))
    params.append(int(limit))
    rows = connection.execute(
        f"""
        SELECT id, event_type, category, repo, tag, destination_path, message, created_at
        FROM lifecycle_events
        WHERE {' AND '.join(conditions)}
        ORDER BY id DESC
        LIMIT ?
        """,
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def get_storage_stats(connection):
    """Return read-only size and growth figures for state.db (used by the --perf-report diagnostics)."""
    def scalar(sql, params=()):
        row = connection.execute(sql, params).fetchone()
        return None if row is None or row[0] is None else row[0]

    def grouped(sql):
        return {str(row[0]): int(row[1]) for row in connection.execute(sql).fetchall()}

    week_ago = scalar("SELECT CAST(strftime('%s', 'now') AS REAL) - 7 * 86400")
    return {
        "tables": {
            name: int(scalar(f"SELECT COUNT(*) FROM {name}") or 0)
            for name in ("job_queue", "job_skip_details", "lifecycle_events", "asset_state")
        },
        "jobs_by_status": grouped("SELECT status, COUNT(*) FROM job_queue GROUP BY status"),
        "events_by_type": grouped("SELECT event_type, COUNT(*) FROM lifecycle_events GROUP BY event_type"),
        "oldest_job_at": scalar("SELECT MIN(created_at) FROM job_queue"),
        "oldest_event_at": scalar("SELECT MIN(created_at) FROM lifecycle_events"),
        "jobs_last_7_days": int(scalar("SELECT COUNT(*) FROM job_queue WHERE created_at >= ?", (week_ago,)) or 0),
        "events_last_7_days": int(scalar("SELECT COUNT(*) FROM lifecycle_events WHERE created_at >= ?", (week_ago,)) or 0),
        "page_size": int(scalar("PRAGMA page_size") or 0),
        "page_count": int(scalar("PRAGMA page_count") or 0),
        "freelist_pages": int(scalar("PRAGMA freelist_count") or 0),
        "indexes": sorted(
            f"{row['tbl_name']}.{row['name']}"
            for row in connection.execute(
                "SELECT name, tbl_name FROM sqlite_master WHERE type = 'index' AND name NOT LIKE 'sqlite_autoindex%'"
            ).fetchall()
        ),
    }


def get_max_event_id(connection):
    """Return the newest lifecycle event id (0 when there are no events)."""
    row = connection.execute("SELECT MAX(id) FROM lifecycle_events").fetchone()
    return int(row[0] or 0)


def purge_lifecycle_events(connection, event_type=None, repo_filter=None, min_age_days=None, dry_run=False):
    """Delete lifecycle events matching the given filters and return the number removed.

    min_age_days=0 matches every event created up to now (i.e. no age floor);
    larger values only match events at least that many days old. When
    dry_run is True, no rows are deleted - the matching count is returned
    instead, computed via SELECT COUNT(*).
    """
    conditions = []
    params: list = []

    if event_type:
        conditions.append("event_type = ?")
        params.append(event_type)

    if repo_filter:
        conditions.append("LOWER(repo) LIKE ?")
        params.append(f"%{repo_filter.lower()}%")

    if min_age_days is not None:
        conditions.append("created_at <= CAST(strftime('%s', 'now') AS REAL) - ?")
        params.append(float(min_age_days) * 86400)

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    if dry_run:
        row = connection.execute(
            f"SELECT COUNT(*) AS matched_count FROM lifecycle_events {where_clause}", params
        ).fetchone()
        return int(row["matched_count"])

    cursor = connection.execute(f"DELETE FROM lifecycle_events {where_clause}", params)
    connection.commit()
    return cursor.rowcount


def purge_job_queue_rows(connection, status=None, repo_filter=None, min_age_days=None, oldest_count=None, dry_run=False):
    """Delete terminal (non-PENDING) job_queue rows matching the given filters.

    PENDING jobs are always excluded, regardless of filters, so an active
    queue can never be purged by accident. Deleting a job_queue row cascades
    to its job_skip_details rows via ON DELETE CASCADE. Exactly one of
    min_age_days or oldest_count is expected to be provided by the caller:
    min_age_days deletes rows at least that many days old (by
    completed_at/updated_at/created_at, 0 matches every terminal job up to
    now), while oldest_count deletes the N oldest matching rows by that same
    age fallback regardless of age. When dry_run is True, no rows are
    deleted - the matching count is returned instead.
    """
    conditions = ["status != 'PENDING'"]
    params: list = []

    if status:
        conditions.append("status = ?")
        params.append(status)

    if repo_filter:
        conditions.append("LOWER(repo) LIKE ?")
        params.append(f"%{repo_filter.lower()}%")

    if min_age_days is not None:
        conditions.append("COALESCE(completed_at, updated_at, created_at) <= CAST(strftime('%s', 'now') AS REAL) - ?")
        params.append(float(min_age_days) * 86400)

    where_clause = f"WHERE {' AND '.join(conditions)}"

    if oldest_count is not None:
        oldest_ids_query = (
            f"SELECT id FROM job_queue {where_clause} "
            "ORDER BY COALESCE(completed_at, updated_at, created_at) ASC, id ASC LIMIT ?"
        )
        oldest_params = params + [int(oldest_count)]
        if dry_run:
            row = connection.execute(
                f"SELECT COUNT(*) AS matched_count FROM ({oldest_ids_query})", oldest_params
            ).fetchone()
            return int(row["matched_count"])

        cursor = connection.execute(f"DELETE FROM job_queue WHERE id IN ({oldest_ids_query})", oldest_params)
        connection.commit()
        return cursor.rowcount

    if dry_run:
        row = connection.execute(
            f"SELECT COUNT(*) AS matched_count FROM job_queue {where_clause}", params
        ).fetchone()
        return int(row["matched_count"])

    cursor = connection.execute(f"DELETE FROM job_queue {where_clause}", params)
    connection.commit()
    return cursor.rowcount


# Daemon control helpers (single row, id = 1).
def get_daemon_control(connection):
    """Return (paused, poll_now_request, log_override); defaults when the row doesn't exist yet.

    log_override is None (follow config.json), True (force on) or False (force off).
    """
    row = connection.execute(
        "SELECT paused, poll_now_request, log_override FROM daemon_control WHERE id = 1"
    ).fetchone()
    if row is None:
        return False, None, None
    log_override = row["log_override"]
    return bool(row["paused"]), row["poll_now_request"], None if log_override is None else bool(log_override)


def set_daemon_log_override(connection, override: Optional[bool]):
    """Set the terminal-log override (None = follow config.json), leaving the rest untouched."""
    connection.execute(
        """
        INSERT INTO daemon_control (id, log_override) VALUES (1, ?)
        ON CONFLICT(id) DO UPDATE SET log_override = excluded.log_override
        """,
        (None if override is None else int(override),),
    )
    connection.commit()


def set_daemon_paused(connection, paused: bool):
    """Set the pause flag, leaving any pending poll-now request untouched."""
    connection.execute(
        """
        INSERT INTO daemon_control (id, paused) VALUES (1, ?)
        ON CONFLICT(id) DO UPDATE SET paused = excluded.paused
        """,
        (1 if paused else 0,),
    )
    connection.commit()


def set_daemon_poll_now_request(connection, stamp: float) -> float:
    """Store a poll-now request stamp, guaranteed to differ from the previous one.

    Returns the stamp actually stored.
    """
    connection.execute("INSERT OR IGNORE INTO daemon_control (id) VALUES (1)")
    connection.execute(
        """
        UPDATE daemon_control
        SET poll_now_request = CASE
            WHEN poll_now_request = ? THEN ? + 0.000001
            ELSE ?
        END
        WHERE id = 1
        """,
        (stamp, stamp, stamp),
    )
    stored = connection.execute(
        "SELECT poll_now_request FROM daemon_control WHERE id = 1"
    ).fetchone()[0]
    connection.commit()
    return stored


def get_daemon_check_folders_request(connection):
    """Return the epoch stamp of the latest check-folders request, or None when never requested."""
    row = connection.execute("SELECT check_folders_request FROM daemon_control WHERE id = 1").fetchone()
    return None if row is None else row["check_folders_request"]


def set_daemon_check_folders_request(connection, stamp: float) -> float:
    """Store a check-folders request stamp, guaranteed to differ from the previous one. Returns the stored stamp."""
    connection.execute("INSERT OR IGNORE INTO daemon_control (id) VALUES (1)")
    connection.execute(
        """
        UPDATE daemon_control
        SET check_folders_request = CASE
            WHEN check_folders_request = ? THEN ? + 0.000001
            ELSE ?
        END
        WHERE id = 1
        """,
        (stamp, stamp, stamp),
    )
    stored = connection.execute("SELECT check_folders_request FROM daemon_control WHERE id = 1").fetchone()[0]
    connection.commit()
    return stored


def get_daemon_stop_request(connection):
    """Return the epoch stamp of the latest graceful-stop request, or None when never requested."""
    row = connection.execute("SELECT stop_request FROM daemon_control WHERE id = 1").fetchone()
    return None if row is None else row["stop_request"]


def set_daemon_stop_request(connection, stamp: float) -> float:
    """Store a stop request stamp, guaranteed to differ from the previous one. Returns the stored stamp."""
    connection.execute("INSERT OR IGNORE INTO daemon_control (id) VALUES (1)")
    connection.execute(
        """
        UPDATE daemon_control
        SET stop_request = CASE
            WHEN stop_request = ? THEN ? + 0.000001
            ELSE ?
        END
        WHERE id = 1
        """,
        (stamp, stamp, stamp),
    )
    stored = connection.execute("SELECT stop_request FROM daemon_control WHERE id = 1").fetchone()[0]
    connection.commit()
    return stored
