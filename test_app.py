import tempfile
import unittest
from collections import Counter
from pathlib import Path

from app import BusinessError, RandomizationStore


class RandomizationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = RandomizationStore(Path(self.tmp.name) / "test.db")
        self.store.seed()
        self.trial = self.store.create_trial(
            "coord", "多中心降压研究", "v1.0", ["A", "B"], ["risk"], 4, "seed-2026-001"
        )
        self.store.start_trial("coord", self.trial["id"])

    def tearDown(self):
        self.tmp.cleanup()

    def test_stratified_block_randomization_and_two_person_unblinding(self):
        participants = [
            self.store.enroll("site1", self.trial["id"], f"S001-{i:03d}", {"risk": "low"})
            for i in range(1, 5)
        ]
        self.assertNotIn("arm", participants[0])
        with self.store.connect() as conn:
            arms = [r["arm"] for r in conn.execute(
                "SELECT a.arm FROM allocations a JOIN participants p ON p.allocation_id=a.id WHERE p.trial_id=? ORDER BY p.id",
                (self.trial["id"],),
            ).fetchall()]
        self.assertEqual(Counter(arms), Counter({"A": 2, "B": 2}))
        request = self.store.request_unblinding("site1", participants[0]["id"], "受试者发生严重不良事件需要紧急处理")
        first = self.store.approve_unblinding("monitor1", request["id"])
        self.assertEqual(first["status"], "pending")
        with self.assertRaises(BusinessError) as ctx:
            self.store.approve_unblinding("monitor1", request["id"])
        self.assertEqual(ctx.exception.code, "distinct_approver_required")
        second = self.store.approve_unblinding("monitor2", request["id"])
        self.assertEqual(second["status"], "approved")
        self.assertIn(second["arm"], {"A", "B"})

    def test_idempotent_enrollment_site_isolation_and_protocol_lock(self):
        first = self.store.enroll("site1", self.trial["id"], "S001-001", {"risk": "high"})
        again = self.store.enroll("site1", self.trial["id"], "S001-001", {"risk": "high"})
        self.assertEqual(first["id"], again["id"])
        self.assertTrue(again["idempotent"])
        with self.store.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM participants").fetchone()[0], 1)
        with self.assertRaises(BusinessError) as ctx:
            self.store.get_participant("site2", first["id"])
        self.assertEqual(ctx.exception.code, "site_isolation")
        with self.assertRaises(BusinessError) as ctx:
            self.store.update_protocol("coord", self.trial["id"], "v2")
        self.assertEqual(ctx.exception.code, "protocol_locked")

    def _enroll(self, user, code):
        return self.store.enroll(user, self.trial["id"], code, {"risk": "low"})

    def test_lock_batch_pauses_enrollment_and_snapshot_uses_cutoff(self):
        # 切点前：site1 入组 3 人、site2 入组 1 人；其中一例已办完紧急揭盲
        p1 = self._enroll("site1", "S001-001")
        self._enroll("site1", "S001-002")
        self._enroll("site1", "S001-003")
        self._enroll("site2", "S002-001")
        req = self.store.request_unblinding("site1", p1["id"], "受试者发生严重不良事件需要紧急处理")
        self.store.approve_unblinding("monitor1", req["id"])
        self.store.approve_unblinding("monitor2", req["id"])

        batch = self.store.initiate_lock_batch("coord", self.trial["id"], "中期分析")
        self.assertEqual(batch["status"], "pending")
        self.assertEqual(batch["participant_cutoff_id"], 4)
        self.assertIsNone(batch["snapshot"])

        # 锁库期间新入组被暂停
        with self.assertRaises(BusinessError) as ctx:
            self._enroll("site1", "S001-004")
        self.assertEqual(ctx.exception.code, "enrollment_paused")
        self.assertEqual(ctx.exception.status, 423)

        # 幂等：重复发起返回同一个待复核批次
        again = self.store.initiate_lock_batch("coord", self.trial["id"])
        self.assertEqual(again["id"], batch["id"])

        # 中心用户/监查员不能发起；监查员不能发起
        with self.assertRaises(BusinessError) as ctx:
            self.store.initiate_lock_batch("monitor1", self.trial["id"])
        self.assertEqual(ctx.exception.code, "forbidden")

        # 锁库期间已受理的紧急揭盲照常办完（晚到揭盲）
        p2 = next(p for p in self.store.list_participants("coord", self.trial["id"]) if p["external_id"] == "S001-002")
        late_req = self.store.request_unblinding("site1", p2["id"], "锁库期间发生新的严重不良事件")
        self.store.approve_unblinding("monitor1", late_req["id"])
        self.store.approve_unblinding("monitor2", late_req["id"])

        # 协调员不能复核
        with self.assertRaises(BusinessError) as ctx:
            self.store.review_lock_batch("coord", batch["id"], True)
        self.assertEqual(ctx.exception.code, "forbidden")

        done = self.store.review_lock_batch("monitor1", batch["id"], True, "数据一致")
        self.assertEqual(done["status"], "completed")
        snap = done["snapshot"]
        self.assertEqual(snap["participant_count"], 4)
        self.assertEqual({x["site_id"]: x["count"] for x in snap["sites"]}, {"S001": 3, "S002": 1})
        audit_actions = [a["action"] for a in snap["audit"]]
        self.assertIn("unblinding.approve.second", audit_actions)  # 切点前揭盲在快照中
        self.assertNotIn("lock_batch.initiate", audit_actions)     # 锁库自身动作不在切点
        self.assertFalse(any(a["detail"].get("request_id") == late_req["id"] for a in snap["audit"]))

        # 恢复入组
        new_p = self._enroll("site1", "S001-004")
        self.assertFalse(new_p["idempotent"])

        summary = self.store.trial_summary("coord", self.trial["id"])
        self.assertFalse(summary["trial"]["enrollment_paused"])
        self.assertIsNone(summary["pending_lock_batch_id"])

    def test_rejected_review_creates_no_batch_snapshot_and_resumes(self):
        self._enroll("site1", "S001-010")
        batch = self.store.initiate_lock_batch("coord", self.trial["id"])
        with self.assertRaises(BusinessError) as ctx:
            self.store.review_lock_batch("monitor1", batch["id"], False)
        self.assertEqual(ctx.exception.code, "review_note_required")

        rejected = self.store.review_lock_batch("monitor1", batch["id"], False, "数据不一致")
        self.assertEqual(rejected["status"], "rejected")
        self.assertIsNone(rejected["snapshot"])
        self.assertEqual(rejected["review_note"], "数据不一致")

        # 不能重复复核
        with self.assertRaises(BusinessError) as ctx:
            self.store.review_lock_batch("monitor1", batch["id"], True, "x")
        self.assertEqual(ctx.exception.code, "lock_batch_decided")

        # 入组已恢复，且可以发起下一批次
        self._enroll("site2", "S002-010")
        second = self.store.initiate_lock_batch("coord", self.trial["id"])
        self.assertEqual(second["batch_no"], batch["batch_no"] + 1)

    def test_late_unblinding_belongs_to_next_batch_not_old_one(self):
        self._enroll("site1", "S001-100")
        first = self.store.initiate_lock_batch("coord", self.trial["id"])
        done = self.store.review_lock_batch("monitor1", first["id"], True)
        self.assertEqual(done["snapshot"]["participant_count"], 1)

        # 旧批次已冻结：之后办完的揭盲不进旧批次
        p = self.store.list_participants("coord", self.trial["id"])[0]
        req = self.store.request_unblinding("site1", p["id"], "随访期间出现需要揭盲的紧急情况")
        self.store.approve_unblinding("monitor1", req["id"])
        self.store.approve_unblinding("monitor2", req["id"])

        history = self.store.list_lock_batches("coord", self.trial["id"])
        old = next(b for b in history if b["id"] == first["id"])
        self.assertFalse(any(a["detail"].get("request_id") == req["id"] for a in old["snapshot"]["audit"]))

        second = self.store.review_lock_batch(
            "monitor1", self.store.initiate_lock_batch("coord", self.trial["id"])["id"], True
        )
        # 新批次切点包含晚到揭盲
        self.assertTrue(any(a["detail"].get("request_id") == req["id"] for a in second["snapshot"]["audit"]))


if __name__ == "__main__":
    unittest.main()
