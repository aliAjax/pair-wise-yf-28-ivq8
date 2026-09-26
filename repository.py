"""数据记录层：SQLite 连接、建表迁移与纯数据访问。

本模块不做角色/业务规则判断，只负责把 SQL 存取封装成语义明确的方法；
事务编排与业务校验在 service.RandomizationService 中完成。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB = BASE_DIR / "randomization.db"
MAX_ARM_LENGTH = 40


class RandomizationStore:
    def __init__(self, db_path=DEFAULT_DB):
        self.db_path = str(db_path)
        self._lock = threading.Lock()

    def connect(self):
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=15000")
        return conn

    def init_schema(self):
        with self._lock, self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users(
                    id TEXT PRIMARY KEY, name TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('site','coordinator','monitor')),
                    site_id TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1))
                );
                CREATE TABLE IF NOT EXISTS trials(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE,
                    protocol_version TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'draft'
                        CHECK(status IN ('draft','running','stopped')),
                    arms_json TEXT NOT NULL, strata_factors_json TEXT NOT NULL,
                    block_size INTEGER NOT NULL CHECK(block_size >= 2),
                    seed TEXT NOT NULL, created_by TEXT NOT NULL REFERENCES users(id),
                    created_at TEXT NOT NULL, started_at TEXT
                );
                CREATE TABLE IF NOT EXISTS strata(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trial_id INTEGER NOT NULL REFERENCES trials(id),
                    stratum_key TEXT NOT NULL, factors_json TEXT NOT NULL, created_at TEXT NOT NULL,
                    UNIQUE(trial_id,stratum_key)
                );
                CREATE TABLE IF NOT EXISTS allocations(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trial_id INTEGER NOT NULL REFERENCES trials(id),
                    stratum_id INTEGER NOT NULL REFERENCES strata(id),
                    sequence INTEGER NOT NULL, block_no INTEGER NOT NULL,
                    arm TEXT NOT NULL, used_by INTEGER, used_at TEXT,
                    UNIQUE(stratum_id,sequence)
                );
                CREATE TABLE IF NOT EXISTS participants(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trial_id INTEGER NOT NULL REFERENCES trials(id),
                    site_id TEXT NOT NULL, external_id TEXT NOT NULL,
                    stratum_id INTEGER NOT NULL REFERENCES strata(id),
                    allocation_id INTEGER NOT NULL UNIQUE REFERENCES allocations(id),
                    allocation_code TEXT NOT NULL UNIQUE, status TEXT NOT NULL DEFAULT 'enrolled'
                        CHECK(status IN ('enrolled','withdrawn','completed')),
                    enrolled_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL,
                    UNIQUE(trial_id,external_id)
                );
                CREATE TABLE IF NOT EXISTS unblinding_requests(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    participant_id INTEGER NOT NULL REFERENCES participants(id),
                    requester_id TEXT NOT NULL REFERENCES users(id), reason TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','rejected')),
                    first_approver TEXT REFERENCES users(id), second_approver TEXT REFERENCES users(id),
                    decided_at TEXT, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS lock_batches(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trial_id INTEGER NOT NULL REFERENCES trials(id),
                    batch_no INTEGER NOT NULL UNIQUE,
                    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','completed','rejected')),
                    initiated_by TEXT NOT NULL REFERENCES users(id),
                    reviewed_by TEXT REFERENCES users(id),
                    initiated_at TEXT NOT NULL, reviewed_at TEXT,
                    cutoff_at TEXT NOT NULL,
                    participant_cutoff_id INTEGER NOT NULL,
                    audit_cutoff_id INTEGER NOT NULL,
                    snapshot_json TEXT, review_note TEXT
                );
                CREATE TABLE IF NOT EXISTS audit_log(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, trial_id INTEGER REFERENCES trials(id),
                    actor_id TEXT NOT NULL REFERENCES users(id), action TEXT NOT NULL,
                    detail TEXT NOT NULL, created_at TEXT NOT NULL
                );
                """
            )
            self._migrate(conn)

    @staticmethod
    def _migrate(conn):
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(trials)").fetchall()}
        if "enrollment_paused" not in cols:
            conn.execute("ALTER TABLE trials ADD COLUMN enrollment_paused INTEGER NOT NULL DEFAULT 0")

    def seed(self):
        self.init_schema()
        with self.connect() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO users(id,name,role,site_id) VALUES(?,?,?,?)",
                [
                    ("site1", "中心一协调员", "site", "S001"),
                    ("site2", "中心二协调员", "site", "S002"),
                    ("coord", "项目协调员", "coordinator", "CENTER"),
                    ("monitor1", "独立监查员甲", "monitor", "CENTER"),
                    ("monitor2", "独立监查员乙", "monitor", "CENTER"),
                ],
            )

    # ---- 基础读取 ----
    def get_active_user(self, conn, user_id):
        return conn.execute("SELECT * FROM users WHERE id=? AND active=1", (user_id,)).fetchone()

    def get_trial(self, conn, trial_id):
        return conn.execute("SELECT * FROM trials WHERE id=?", (trial_id,)).fetchone()

    def participant_count(self, conn, trial_id):
        return conn.execute("SELECT COUNT(*) FROM participants WHERE trial_id=?", (trial_id,)).fetchone()[0]

    def get_participant(self, conn, participant_id):
        return conn.execute("SELECT * FROM participants WHERE id=?", (participant_id,)).fetchone()

    def find_participant(self, conn, trial_id, external_id):
        return conn.execute(
            "SELECT * FROM participants WHERE trial_id=? AND external_id=?", (trial_id, external_id)
        ).fetchone()

    def list_participants(self, conn, trial_id, site_id=None):
        if site_id:
            return conn.execute(
                "SELECT * FROM participants WHERE trial_id=? AND site_id=? ORDER BY id", (trial_id, site_id)
            ).fetchall()
        return conn.execute("SELECT * FROM participants WHERE trial_id=? ORDER BY id", (trial_id,)).fetchall()

    def participants_up_to(self, conn, trial_id, max_id, site_id=None):
        if site_id:
            return conn.execute(
                "SELECT * FROM participants WHERE trial_id=? AND id<=? AND site_id=? ORDER BY id",
                (trial_id, max_id, site_id),
            ).fetchall()
        return conn.execute(
            "SELECT * FROM participants WHERE trial_id=? AND id<=? ORDER BY id", (trial_id, max_id)
        ).fetchall()

    def get_stratum(self, conn, trial_id, key):
        return conn.execute("SELECT * FROM strata WHERE trial_id=? AND stratum_key=?", (trial_id, key)).fetchone()

    def insert_stratum(self, conn, trial_id, key, factors_json, ts):
        cur = conn.execute(
            "INSERT INTO strata(trial_id,stratum_key,factors_json,created_at) VALUES(?,?,?,?)",
            (trial_id, key, factors_json, ts),
        )
        return cur.lastrowid

    def block_count(self, conn, stratum_id, block_no):
        return conn.execute(
            "SELECT COUNT(*) FROM allocations WHERE stratum_id=? AND block_no=?", (stratum_id, block_no)
        ).fetchone()[0]

    def max_sequence(self, conn, stratum_id):
        return conn.execute(
            "SELECT COALESCE(MAX(sequence),0) FROM allocations WHERE stratum_id=?", (stratum_id,)
        ).fetchone()[0]

    def insert_allocations(self, conn, rows):
        conn.executemany(
            "INSERT INTO allocations(trial_id,stratum_id,sequence,block_no,arm) VALUES(?,?,?,?,?)", rows
        )

    def free_allocation(self, conn, stratum_id):
        return conn.execute(
            "SELECT * FROM allocations WHERE stratum_id=? AND used_by IS NULL ORDER BY sequence LIMIT 1",
            (stratum_id,),
        ).fetchone()

    def allocation_arm(self, conn, allocation_id):
        return conn.execute("SELECT arm FROM allocations WHERE id=?", (allocation_id,)).fetchone()["arm"]

    def insert_participant(self, conn, trial_id, site_id, external_id, stratum_id, allocation_id, code, user_id, ts):
        cur = conn.execute(
            """INSERT INTO participants(trial_id,site_id,external_id,stratum_id,allocation_id,allocation_code,enrolled_by,created_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (trial_id, site_id, external_id, stratum_id, allocation_id, code, user_id, ts),
        )
        return cur.lastrowid

    def mark_allocation_used(self, conn, allocation_id, participant_id, ts):
        conn.execute("UPDATE allocations SET used_by=?,used_at=? WHERE id=?", (participant_id, ts, allocation_id))

    # ---- 揭盲 ----
    def open_unblinding_request(self, conn, participant_id):
        return conn.execute(
            "SELECT id FROM unblinding_requests WHERE participant_id=? AND status='pending'", (participant_id,)
        ).fetchone()

    def insert_unblinding_request(self, conn, participant_id, user_id, reason, ts):
        cur = conn.execute(
            "INSERT INTO unblinding_requests(participant_id,requester_id,reason,created_at) VALUES(?,?,?,?)",
            (participant_id, user_id, reason, ts),
        )
        return cur.lastrowid

    def get_unblinding_request(self, conn, request_id):
        return conn.execute("SELECT * FROM unblinding_requests WHERE id=?", (request_id,)).fetchone()

    def set_first_approver(self, conn, request_id, user_id):
        conn.execute("UPDATE unblinding_requests SET first_approver=? WHERE id=?", (user_id, request_id))

    def approve_unblinding_request(self, conn, request_id, user_id, ts):
        conn.execute(
            "UPDATE unblinding_requests SET second_approver=?,status='approved',decided_at=? WHERE id=?",
            (user_id, ts, request_id),
        )

    def approved_unblinding_exists(self, conn, participant_id):
        return conn.execute(
            "SELECT 1 FROM unblinding_requests WHERE participant_id=? AND status='approved'", (participant_id,)
        ).fetchone() is not None

    # ---- 锁库批次 ----
    def pending_lock_batch(self, conn, trial_id):
        return conn.execute(
            "SELECT * FROM lock_batches WHERE trial_id=? AND status='pending' ORDER BY id", (trial_id,)
        ).fetchone()

    def get_lock_batch(self, conn, batch_id):
        return conn.execute("SELECT * FROM lock_batches WHERE id=?", (batch_id,)).fetchone()

    def list_lock_batches(self, conn, trial_id):
        return conn.execute(
            "SELECT * FROM lock_batches WHERE trial_id=? ORDER BY id", (trial_id,)
        ).fetchall()

    def next_lock_batch_no(self, conn, trial_id):
        row = conn.execute(
            "SELECT COALESCE(MAX(batch_no),0)+1 AS next FROM lock_batches WHERE trial_id=?", (trial_id,)
        ).fetchone()
        return row["next"]

    def insert_lock_batch(self, conn, trial_id, batch_no, user_id, ts, cutoff_participant_id, cutoff_audit_id):
        cur = conn.execute(
            """INSERT INTO lock_batches(trial_id,batch_no,status,initiated_by,initiated_at,cutoff_at,
                   participant_cutoff_id,audit_cutoff_id)
               VALUES(?,?, 'pending', ?, ?, ?, ?, ?)""",
            (trial_id, batch_no, user_id, ts, ts, cutoff_participant_id, cutoff_audit_id),
        )
        return cur.lastrowid

    def complete_lock_batch(self, conn, batch_id, user_id, ts, snapshot_json):
        conn.execute(
            """UPDATE lock_batches SET status='completed', reviewed_by=?, reviewed_at=?, snapshot_json=?
               WHERE id=?""",
            (user_id, ts, snapshot_json, batch_id),
        )

    def reject_lock_batch(self, conn, batch_id, user_id, ts, note):
        conn.execute(
            """UPDATE lock_batches SET status='rejected', reviewed_by=?, reviewed_at=?, review_note=?
               WHERE id=?""",
            (user_id, ts, note, batch_id),
        )

    def set_enrollment_paused(self, conn, trial_id, paused):
        conn.execute("UPDATE trials SET enrollment_paused=? WHERE id=?", (1 if paused else 0, trial_id))

    def cutoff_participant_id(self, conn, trial_id):
        return conn.execute("SELECT COALESCE(MAX(id),0) FROM participants WHERE trial_id=?", (trial_id,)).fetchone()[0]

    def cutoff_audit_id(self, conn, trial_id):
        return conn.execute("SELECT COALESCE(MAX(id),0) FROM audit_log WHERE trial_id=?", (trial_id,)).fetchone()[0]

    def participants_at_cutoff(self, conn, trial_id, max_id):
        return conn.execute(
            "SELECT id, site_id FROM participants WHERE trial_id=? AND id<=? ORDER BY id", (trial_id, max_id)
        ).fetchall()

    def audit_log_at_cutoff(self, conn, trial_id, max_id):
        return conn.execute(
            "SELECT * FROM audit_log WHERE trial_id=? AND id<=? ORDER BY id", (trial_id, max_id)
        ).fetchall()

    def audit_log(self, conn, trial_id):
        return conn.execute("SELECT * FROM audit_log WHERE trial_id=? ORDER BY id", (trial_id,)).fetchall()

    # ---- 试验写入与汇总 ----
    def insert_trial(self, conn, name, protocol_version, arms_json, strata_json, block_size, seed, user_id, ts):
        cur = conn.execute(
            """INSERT INTO trials(name,protocol_version,arms_json,strata_factors_json,block_size,seed,created_by,created_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (name, protocol_version, arms_json, strata_json, block_size, seed, user_id, ts),
        )
        return cur.lastrowid

    def update_protocol_row(self, conn, trial_id, protocol_version, arms_json, strata_json, block_size, seed):
        conn.execute(
            """UPDATE trials SET protocol_version=?,arms_json=?,strata_factors_json=?,block_size=?,seed=? WHERE id=?""",
            (protocol_version, arms_json, strata_json, block_size, seed, trial_id),
        )

    def start_trial_row(self, conn, trial_id, ts):
        conn.execute("UPDATE trials SET status='running',started_at=? WHERE id=?", (ts, trial_id))

    def site_counts(self, conn, trial_id, site_id=None):
        if site_id:
            return conn.execute(
                "SELECT site_id,COUNT(*) AS count FROM participants WHERE trial_id=? AND site_id=? GROUP BY site_id",
                (trial_id, site_id),
            ).fetchall()
        return conn.execute(
            "SELECT site_id,COUNT(*) AS count FROM participants WHERE trial_id=? GROUP BY site_id", (trial_id,)
        ).fetchall()

    def insert_audit(self, conn, trial_id, actor, action, detail, ts):
        conn.execute(
            "INSERT INTO audit_log(trial_id,actor_id,action,detail,created_at) VALUES(?,?,?,?,?)",
            (trial_id, actor, action, json.dumps(detail, ensure_ascii=False, sort_keys=True), ts),
        )
