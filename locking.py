"""中期锁库批次：事务编排层。

流程：协调员发起 -> 记录数据切点并暂停新入组（紧急揭盲不受影响）
-> 监查员复核：通过则固化切点快照（人数、中心分布、审计记录）并恢复入组；
不通过则不生成批次快照并恢复入组。切点之后的揭盲等事件不会写入旧批次。
"""
from __future__ import annotations

import json

from store import BusinessError, now

STATUS_LABELS = {
    "pending_review": "待监查复核",
    "confirmed": "复核通过已固化",
    "rejected": "复核不通过",
}


class LockBatchRepository:
    """数据记录读写，不自行决定事务边界。"""

    def insert_pending(self, conn, trial_id, batch_no, initiated_by, cutoff_at, cutoff_participant_id):
        cur = conn.execute(
            """INSERT INTO db_lock_batches(trial_id,batch_no,status,cutoff_at,initiated_by,initiated_at,cutoff_participant_id)
               VALUES(?,?,'pending_review',?,?,?,?)""",
            (trial_id, batch_no, cutoff_at, initiated_by, now(), cutoff_participant_id),
        )
        return cur.lastrowid

    def set_cutoff_audit_id(self, conn, batch_id, cutoff_audit_id):
        conn.execute("UPDATE db_lock_batches SET cutoff_audit_id=? WHERE id=?", (cutoff_audit_id, batch_id))

    def get(self, conn, batch_id):
        return conn.execute("SELECT * FROM db_lock_batches WHERE id=?", (batch_id,)).fetchone()

    def open_batch_for_trial(self, conn, trial_id):
        return conn.execute(
            "SELECT * FROM db_lock_batches WHERE trial_id=? AND status='pending_review' ORDER BY id LIMIT 1",
            (trial_id,),
        ).fetchone()

    def list_for_trial(self, conn, trial_id):
        return conn.execute(
            "SELECT * FROM db_lock_batches WHERE trial_id=? ORDER BY batch_no", (trial_id,)
        ).fetchall()

    def next_batch_no(self, conn, trial_id):
        row = conn.execute(
            "SELECT COALESCE(MAX(batch_no),0) AS max_no FROM db_lock_batches WHERE trial_id=?", (trial_id,)
        ).fetchone()
        return row["max_no"] + 1

    def mark_reviewed(self, conn, batch_id, status, reviewer, reviewed_at, note, snapshot):
        conn.execute(
            """UPDATE db_lock_batches
               SET status=?,reviewed_by=?,reviewed_at=?,review_note=?,snapshot_json=? WHERE id=?""",
            (status, reviewer, reviewed_at, note,
             json.dumps(snapshot, ensure_ascii=False, sort_keys=True) if snapshot is not None else None, batch_id),
        )


class LockBatchService:
    def __init__(self, store):
        self.store = store
        self.repo = LockBatchRepository()

    def _audit(self, conn, trial_id, actor, action, detail):
        conn.execute(
            "INSERT INTO audit_log(trial_id,actor_id,action,detail,created_at) VALUES(?,?,?,?,?)",
            (trial_id, actor, action, json.dumps(detail, ensure_ascii=False, sort_keys=True), now()),
        )
        return conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]

    def initiate(self, user_id, trial_id, note=None):
        """协调员发起锁库：在同一事务内确定数据切点并挂起入组。"""
        with self.store.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                self.store._user(conn, user_id, {"coordinator"})
                trial = self.store._trial(conn, trial_id)
                if trial["status"] != "running":
                    raise BusinessError("只有进行中的试验可以发起中期锁库", 409, "trial_not_running")
                if self.repo.open_batch_for_trial(conn, trial_id):
                    raise BusinessError("已有锁库批次等待复核，不能重复发起", 409, "lock_batch_open")
                batch_no = self.repo.next_batch_no(conn, trial_id)
                cutoff_at = now()
                cutoff_participant_id = conn.execute(
                    "SELECT COALESCE(MAX(id),0) AS max_id FROM participants WHERE trial_id=?", (trial_id,)
                ).fetchone()["max_id"]
                batch_id = self.repo.insert_pending(
                    conn, trial_id, batch_no, user_id, cutoff_at, cutoff_participant_id
                )
                cutoff_audit_id = self._audit(
                    conn, trial_id, user_id, "lock_batch.initiate",
                    {"batch_id": batch_id, "batch_no": batch_no, "cutoff_at": cutoff_at,
                     "cutoff_participant_id": cutoff_participant_id,
                     "note": note.strip() if isinstance(note, str) and note.strip() else None},
                )
                self.repo.set_cutoff_audit_id(conn, batch_id, cutoff_audit_id)
                row = self.repo.get(conn, batch_id)
                return self.serialize(row)
            except Exception:
                conn.rollback()
                raise

    @staticmethod
    def _build_snapshot(conn, batch):
        """按切点高水位复算：id <= 切点水位的记录才属于被锁批次。"""
        trial_id = batch["trial_id"]
        participant_rows = conn.execute(
            """SELECT id,site_id,external_id,allocation_code,status,created_at
               FROM participants WHERE trial_id=? AND id<=? ORDER BY id""",
            (trial_id, batch["cutoff_participant_id"]),
        ).fetchall()
        site_rows = conn.execute(
            """SELECT site_id,COUNT(*) AS count FROM participants
               WHERE trial_id=? AND id<=? GROUP BY site_id ORDER BY site_id""",
            (trial_id, batch["cutoff_participant_id"]),
        ).fetchall()
        audit_rows = conn.execute(
            """SELECT id,actor_id,action,detail,created_at FROM audit_log
               WHERE trial_id=? AND id<=? ORDER BY id""",
            (trial_id, batch["cutoff_audit_id"]),
        ).fetchall()
        pending_unblinding = conn.execute(
            """SELECT ur.id,ur.participant_id,ur.status,ur.created_at
               FROM unblinding_requests ur JOIN participants p ON p.id=ur.participant_id
               WHERE p.trial_id=? AND p.id<=? AND ur.status='pending' ORDER BY ur.id""",
            (trial_id, batch["cutoff_participant_id"]),
        ).fetchall()
        return {
            "cutoff_at": batch["cutoff_at"],
            "generated_at": now(),
            "participant_count": len(participant_rows),
            "site_distribution": [dict(r) for r in site_rows],
            "participants": [dict(r) for r in participant_rows],
            "pending_unblinding_at_cutoff": [dict(r) for r in pending_unblinding],
            "audit_records": [dict(r) | {"detail": json.loads(r["detail"])} for r in audit_rows],
        }

    def review(self, user_id, batch_id, decision, note=None):
        """监查员复核。通过才生成切点快照；不通过只结束批次，两种情况都恢复入组。"""
        if decision not in ("confirm", "reject"):
            raise BusinessError("decision 必须是 confirm 或 reject", 422, "invalid_decision")
        with self.store.connect() as conn:
            try:
                conn.execute("BEGIN IMMEDIATE")
                self.store._user(conn, user_id, {"monitor"})
                batch = self.repo.get(conn, batch_id)
                if not batch:
                    raise BusinessError("锁库批次不存在", 404, "not_found")
                if batch["status"] != "pending_review":
                    raise BusinessError("该锁库批次已经复核", 409, "lock_batch_decided")
                clean_note = note.strip() if isinstance(note, str) and note.strip() else None
                if decision == "confirm":
                    snapshot = self._build_snapshot(conn, batch)
                    self.repo.mark_reviewed(
                        conn, batch_id, "confirmed", user_id, now(), clean_note, snapshot
                    )
                    self._audit(
                        conn, batch["trial_id"], user_id, "lock_batch.confirm",
                        {"batch_id": batch_id, "batch_no": batch["batch_no"],
                         "participant_count": snapshot["participant_count"], "note": clean_note},
                    )
                else:
                    # 复核不通过：不生成批次快照，直接放开入组
                    self.repo.mark_reviewed(
                        conn, batch_id, "rejected", user_id, now(), clean_note, None
                    )
                    self._audit(
                        conn, batch["trial_id"], user_id, "lock_batch.reject",
                        {"batch_id": batch_id, "batch_no": batch["batch_no"], "note": clean_note},
                    )
                row = self.repo.get(conn, batch_id)
                return self.serialize(row)
            except Exception:
                conn.rollback()
                raise

    def list_batches(self, user_id, trial_id):
        with self.store.connect() as conn:
            actor = self.store._user(conn, user_id, {"site", "coordinator", "monitor"})
            self.store._trial(conn, trial_id)
            rows = self.repo.list_for_trial(conn, trial_id)
            return [self.serialize(r, include_snapshot=actor["role"] != "site") for r in rows]

    def get_batch(self, user_id, batch_id):
        with self.store.connect() as conn:
            actor = self.store._user(conn, user_id, {"site", "coordinator", "monitor"})
            batch = self.repo.get(conn, batch_id)
            if not batch:
                raise BusinessError("锁库批次不存在", 404, "not_found")
            return self.serialize(batch, include_snapshot=actor["role"] != "site")

    @staticmethod
    def serialize(row, include_snapshot=True):
        data = {
            "id": row["id"],
            "trial_id": row["trial_id"],
            "batch_no": row["batch_no"],
            "status": row["status"],
            "status_label": STATUS_LABELS.get(row["status"], row["status"]),
            "cutoff_at": row["cutoff_at"],
            "cutoff_participant_id": row["cutoff_participant_id"],
            "cutoff_audit_id": row["cutoff_audit_id"],
            "initiated_by": row["initiated_by"],
            "initiated_at": row["initiated_at"],
            "reviewed_by": row["reviewed_by"],
            "reviewed_at": row["reviewed_at"],
            "review_note": row["review_note"],
        }
        if include_snapshot and row["snapshot_json"]:
            data["snapshot"] = json.loads(row["snapshot_json"])
        return data
