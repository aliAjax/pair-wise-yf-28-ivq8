"""中期锁库批次流程测试。"""
import tempfile
import unittest
from pathlib import Path

from locking import LockBatchService
from store import BusinessError, RandomizationStore


class LockBatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = RandomizationStore(Path(self.tmp.name) / "test.db")
        self.store.seed()
        self.locking = LockBatchService(self.store)
        self.trial = self.store.create_trial(
            "coord", "多中心锁库研究", "v1.0", ["A", "B"], ["risk"], 4, "seed-2026-lock"
        )
        self.trial_id = self.trial["id"]
        self.store.start_trial("coord", self.trial_id)

    def tearDown(self):
        self.tmp.cleanup()

    def _enroll_both_sites(self):
        p1 = self.store.enroll("site1", self.trial_id, "S001-001", {"risk": "low"})
        p2 = self.store.enroll("site2", self.trial_id, "S002-001", {"risk": "high"})
        return p1, p2

    def test_initiate_pauses_enrollment_but_allows_unblinding(self):
        p1, p2 = self._enroll_both_sites()
        # 切点前受理、尚未审批完的紧急揭盲
        req = self.store.request_unblinding("site1", p1["id"], "受试者发生严重不良事件需要紧急处理")
        batch = self.locking.initiate("coord", self.trial_id, "中期分析锁库")
        self.assertEqual(batch["status"], "pending_review")
        self.assertEqual(batch["batch_no"], 1)
        self.assertEqual(batch["cutoff_participant_id"], p2["id"])

        # 新入组被暂停
        with self.assertRaises(BusinessError) as ctx:
            self.store.enroll("site1", self.trial_id, "S001-002", {"risk": "low"})
        self.assertEqual(ctx.exception.code, "enrollment_paused")
        # 已受理受试者的重试仍然幂等可查
        again = self.store.enroll("site1", self.trial_id, "S001-001", {"risk": "low"})
        self.assertTrue(again["idempotent"])
        # 紧急揭盲照常走完双人审批
        self.store.approve_unblinding("monitor1", req["id"])
        approved = self.store.approve_unblinding("monitor2", req["id"])
        self.assertEqual(approved["status"], "approved")

    def test_confirm_builds_snapshot_and_resumes_enrollment(self):
        p1, p2 = self._enroll_both_sites()
        batch = self.locking.initiate("coord", self.trial_id)
        confirmed = self.locking.review("monitor1", batch["id"], "confirm", "复核无误")
        self.assertEqual(confirmed["status"], "confirmed")
        snap = confirmed["snapshot"]
        self.assertEqual(snap["participant_count"], 2)
        self.assertEqual({x["site_id"]: x["count"] for x in snap["site_distribution"]}, {"S001": 1, "S002": 1})
        # 切点快照只含切点之前的审计，且至少包含发起动作
        actions = [x["action"] for x in snap["audit_records"]]
        self.assertIn("lock_batch.initiate", actions)
        self.assertNotIn("lock_batch.confirm", actions)
        self.assertEqual({p["id"] for p in snap["participants"]}, {p1["id"], p2["id"]})
        # 恢复入组，新受试者不进旧快照
        p3 = self.store.enroll("site1", self.trial_id, "S001-002", {"risk": "low"})
        stale = self.locking.get_batch("monitor1", batch["id"])
        self.assertEqual(stale["snapshot"]["participant_count"], 2)
        self.assertNotIn(p3["id"], {p["id"] for p in stale["snapshot"]["participants"]})

    def test_late_unblinding_after_cutoff_not_written_into_old_batch(self):
        p1, _ = self._enroll_both_sites()
        batch = self.locking.initiate("coord", self.trial_id)
        # 复核期间出现的新揭盲申请和审批
        req = self.store.request_unblinding("site1", p1["id"], "锁库期间发生严重不良事件需紧急揭盲")
        self.store.approve_unblinding("monitor1", req["id"])
        self.store.approve_unblinding("monitor2", req["id"])
        confirmed = self.locking.review("monitor1", batch["id"], "confirm")
        late_actions = [x["action"] for x in confirmed["snapshot"]["audit_records"]
                        if "unblinding" in x["action"]]
        self.assertEqual(late_actions, [])

    def test_reject_keeps_no_snapshot_and_resumes_enrollment(self):
        self._enroll_both_sites()
        batch = self.locking.initiate("coord", self.trial_id)
        rejected = self.locking.review("monitor1", batch["id"], "reject", "数据有疑问")
        self.assertEqual(rejected["status"], "rejected")
        self.assertNotIn("snapshot", rejected)
        self.store.enroll("site1", self.trial_id, "S001-002", {"risk": "low"})  # 入组已恢复
        # 驳回后可以重新发起，批次号递增
        second = self.locking.initiate("coord", self.trial_id)
        self.assertEqual(second["batch_no"], 2)

    def test_only_coordinator_initiates_only_monitor_reviews(self):
        batch = self.locking.initiate("coord", self.trial_id)
        with self.assertRaises(BusinessError) as ctx:
            self.locking.initiate("monitor1", self.trial_id)
        self.assertEqual(ctx.exception.code, "forbidden")
        with self.assertRaises(BusinessError) as ctx:
            self.locking.review("coord", batch["id"], "confirm")
        self.assertEqual(ctx.exception.code, "forbidden")
        # 复核中的批次不能重复发起
        with self.assertRaises(BusinessError) as ctx:
            self.locking.initiate("coord", self.trial_id)
        self.assertEqual(ctx.exception.code, "lock_batch_open")

    def test_history_and_site_visibility(self):
        self._enroll_both_sites()
        b1 = self.locking.initiate("coord", self.trial_id)
        self.locking.review("monitor1", b1["id"], "confirm")
        b2 = self.locking.initiate("coord", self.trial_id)
        history = self.locking.list_batches("coord", self.trial_id)
        self.assertEqual([b["batch_no"] for b in history], [1, 2])
        self.assertEqual(history[0]["status"], "confirmed")
        self.assertEqual(history[1]["status"], "pending_review")
        # 中心用户可看批次状态，但不返回完整快照（中心隔离）
        site_view = self.locking.list_batches("site1", self.trial_id)
        self.assertEqual(site_view[0]["status"], "confirmed")
        self.assertNotIn("snapshot", site_view[0])


if __name__ == "__main__":
    unittest.main()
