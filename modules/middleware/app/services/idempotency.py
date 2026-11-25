import sqlite3
import threading


class IdempotencyManager:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False) if db_path == ":memory:" else None
        self._init_db()

    def _get_conn(self):
        if self._conn is not None:
            return self._conn
        return sqlite3.connect(self.db_path)

    def _init_db(self):
        with self._lock:
            conn = self._get_conn()
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS idempotency (
                    job_id TEXT NOT NULL,
                    sku TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    PRIMARY KEY (job_id, sku)
                )
                """
            )
            conn.commit()
            if self._conn is None:
                conn.close()

    def check_idempotency(self, job_id: str, sku: str, content_hash: str) -> bool:
        with self._lock:
            conn = self._get_conn()
            try:
                row = conn.execute(
                    "SELECT 1 FROM idempotency WHERE job_id = ? AND sku = ? AND content_hash = ?",
                    (job_id, sku, content_hash),
                ).fetchone()
                return row is not None
            finally:
                if self._conn is None:
                    conn.close()

    def mark_processed(self, job_id: str, sku: str, content_hash: str) -> bool:
        with self._lock:
            conn = self._get_conn()
            try:
                conn.execute(
                    """
                    INSERT INTO idempotency (job_id, sku, content_hash)
                    VALUES (?, ?, ?)
                    ON CONFLICT(job_id, sku) DO UPDATE SET content_hash = excluded.content_hash
                    """,
                    (job_id, sku, content_hash),
                )
                conn.commit()
                return True
            finally:
                if self._conn is None:
                    conn.close()
