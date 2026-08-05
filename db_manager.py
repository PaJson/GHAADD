import os
import sqlite3

STATE_DB_NAME = "state.db"


def get_state_db_path():
    """Return the SQLite state database path beside the app files."""
    app_dir = os.path.dirname(os.path.abspath(__file__))
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
    
    connection.commit()
    return connection


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


def save_state_entry(connection, release_key, item_key, file_name, file_path, expected_signature, remote_size, remote_last_modified, remote_etag):
    """Insert or update one asset-state record for a release item."""
    try:
        local_size = os.path.getsize(file_path)
        local_mtime = os.path.getmtime(file_path)
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
        (release_key, item_key, file_name, file_path, remote_size, remote_last_modified, remote_etag, expected_signature, local_size, local_mtime),
    )
    connection.commit()


def prune_release_state(connection, release_key, valid_item_keys):
    """Delete stale asset-state rows that are missing or no longer valid."""
    rows = connection.execute("SELECT item_key, file_path FROM asset_state WHERE release_key = ?", (release_key,)).fetchall()
    stale_keys = [row["item_key"] for row in rows if row["item_key"] not in valid_item_keys or not row["file_path"] or not os.path.exists(row["file_path"])]

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
            last_result,
            created_at,
            updated_at,
            completed_at
        )
        VALUES (?, ?, ?, 'PENDING', 0, ?, ?, 0, 0, 0, NULL, CAST(strftime('%s', 'now') AS REAL), CAST(strftime('%s', 'now') AS REAL), NULL)
        """,
        (repo, tag, release_type, float(next_check_time), expected_commit),
    )
    connection.commit()
    return cursor.lastrowid


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
                expected_commit,
                downloaded_count,
                skipped_count,
                total_items,
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
            expected_commit,
            downloaded_count,
            skipped_count,
            total_items,
            last_result
        FROM job_queue
        WHERE status = 'PENDING' AND next_check_time <= ?
        ORDER BY next_check_time ASC, id ASC
        """,
        (float(now_timestamp),),
    ).fetchall()


def mark_job_completed(connection, job_id, downloaded_count=0, skipped_count=0, total_items=0, last_result="SUCCESS"):
    """Mark a queued job as completed."""
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


def reschedule_job(
    connection,
    job_id,
    next_check_time,
    attempt_count,
    expected_commit=None,
    downloaded_count=0,
    skipped_count=0,
    total_items=0,
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