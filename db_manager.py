import os
import sqlite3

STATE_DB_NAME = "state.db"

def get_state_db_path():
    """Returns the SQLite state database path beside the app files."""
    app_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(app_dir, STATE_DB_NAME)

def purge_state_database():
    """Deletes the local state database if it exists."""
    state_db_path = get_state_db_path()
    if not os.path.exists(state_db_path):
        return False

    os.remove(state_db_path)
    return True

def open_database():
    """
    Opens the central SQLite database, enables WAL mode for GUI concurrency,
    and ensures the schema exists.
    """
    connection = sqlite3.connect(get_state_db_path())
    connection.row_factory = sqlite3.Row
    
    # Enable Write-Ahead Logging (Crucial for Daemon + Qt GUI concurrency)
    connection.execute("PRAGMA journal_mode=WAL;")
    
    # 1. Existing Table: Long-term storage for duplicate file prevention
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
    
    # 2. NEW Table: The Job Queue for handling re-checks and retries
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS job_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            repo TEXT NOT NULL,
            tag TEXT NOT NULL,
            release_type TEXT,
            status TEXT NOT NULL DEFAULT 'PENDING',  -- PENDING, COMPLETED, FAILED, SUPERSEDED
            attempt_count INTEGER NOT NULL DEFAULT 0,
            next_check_time REAL NOT NULL,          -- Unix timestamp
            expected_commit TEXT                    -- 7-char hash to detect overwrites
        )
        """
    )
    
    connection.commit()
    return connection

# ---------------------------------------------------------
# Asset State Functions (Migrated from downloader.py)
# ---------------------------------------------------------

def load_release_state(connection, release_key):
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
    rows = connection.execute("SELECT item_key, file_path FROM asset_state WHERE release_key = ?", (release_key,)).fetchall()
    stale_keys = [row["item_key"] for row in rows if row["item_key"] not in valid_item_keys or not row["file_path"] or not os.path.exists(row["file_path"])]

    if stale_keys:
        connection.executemany(
            "DELETE FROM asset_state WHERE release_key = ? AND item_key = ?",
            [(release_key, item_key) for item_key in stale_keys],
        )
        connection.commit()