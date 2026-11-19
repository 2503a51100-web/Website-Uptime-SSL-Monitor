"""SQLite storage for Website Uptime and SSL Monitor.

All database access lives in this file. Nothing else in the project talks to
SQLite directly, so the storage layer can be swapped for something else
later (for example DynamoDB on AWS) without touching the monitoring code.
"""

import os
import sqlite3
from contextlib import closing

# The database file sits next to this file, so the app works no matter which
# folder it is started from. MONITOR_DB_PATH can override it (used by tests).
DB_PATH = os.environ.get("MONITOR_DB_PATH") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "monitor.db"
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS checks (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    url                TEXT    NOT NULL,
    status_code        INTEGER,
    response_time_ms   REAL,
    status             TEXT    NOT NULL CHECK (status IN ('UP', 'DOWN')),
    ssl_status         TEXT    NOT NULL,
    ssl_expiry_date    TEXT,
    ssl_days_remaining INTEGER,
    ssl_message        TEXT,
    error_message      TEXT,
    checked_at         TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_checks_checked_at ON checks (checked_at);
CREATE INDEX IF NOT EXISTS idx_checks_url        ON checks (url);
"""

COLUMNS = (
    "id, url, status_code, response_time_ms, status, ssl_status, "
    "ssl_expiry_date, ssl_days_remaining, ssl_message, error_message, checked_at"
)


def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Create the database file and tables if they do not exist yet."""
    with closing(get_connection()) as conn:
        with conn:
            conn.executescript(SCHEMA)


def insert_check(result):
    """Save one monitoring result (a dict from monitor.check_website). Returns the new id."""
    with closing(get_connection()) as conn:
        with conn:
            cur = conn.execute(
                """
                INSERT INTO checks (url, status_code, response_time_ms, status,
                                    ssl_status, ssl_expiry_date, ssl_days_remaining,
                                    ssl_message, error_message, checked_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    result["url"],
                    result.get("status_code"),
                    result.get("response_time_ms"),
                    result["status"],
                    result["ssl_status"],
                    result.get("ssl_expiry_date"),
                    result.get("ssl_days_remaining"),
                    result.get("ssl_message"),
                    result.get("error_message"),
                    result["checked_at"],
                ),
            )
            return cur.lastrowid


def get_check(check_id):
    with closing(get_connection()) as conn:
        row = conn.execute(
            f"SELECT {COLUMNS} FROM checks WHERE id = ?", (check_id,)
        ).fetchone()
    return dict(row) if row else None


def get_history(limit=100, url=None):
    """Newest checks first. Optionally only for one website URL."""
    sql = f"SELECT {COLUMNS} FROM checks"
    params = []
    if url:
        sql += " WHERE url = ?"
        params.append(url)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    with closing(get_connection()) as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def delete_check(check_id):
    """Delete one record. Returns True if a record was deleted."""
    with closing(get_connection()) as conn:
        with conn:
            cur = conn.execute("DELETE FROM checks WHERE id = ?", (check_id,))
            return cur.rowcount > 0


def get_sites():
    """Distinct website URLs that have been checked, alphabetical."""
    with closing(get_connection()) as conn:
        rows = conn.execute("SELECT DISTINCT url FROM checks ORDER BY url").fetchall()
    return [r["url"] for r in rows]


def get_stats(url=None):
    """Uptime statistics calculated from the saved records.

    Uptime % = successful checks / total checks * 100.
    uptime_percentage is None when there are no records yet (nothing to calculate).
    """
    sql = "SELECT COUNT(*) AS total, COALESCE(SUM(status = 'UP'), 0) AS up FROM checks"
    params = []
    if url:
        sql += " WHERE url = ?"
        params.append(url)
    with closing(get_connection()) as conn:
        row = conn.execute(sql, params).fetchone()
    total, up = row["total"], row["up"]
    return {
        "total_checks": total,
        "successful_checks": up,
        "failed_checks": total - up,
        "uptime_percentage": round(up / total * 100, 2) if total else None,
    }

def get_check_by_url_and_checked_at(url, checked_at):
    """Find an existing local record using its URL and check timestamp."""
    with closing(get_connection()) as conn:
        row = conn.execute(
            """
            SELECT id FROM checks
            WHERE url = ? AND checked_at = ?
            LIMIT 1
            """,
            (url, checked_at),
        ).fetchone()

    return row["id"] if row else None