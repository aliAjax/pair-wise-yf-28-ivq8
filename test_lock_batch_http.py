"""锁库批次的 HTTP 端到端测试（真实线程服务器）。"""
import json
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from pathlib import Path

from app import RandomizationServer, RandomizationStore, RandomizationService
from repository import RandomizationStore as RepositoryStore


class HttpLockBatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        store = RepositoryStore(Path(self.tmp.name) / "http.db")
        service = RandomizationService(store)
        service.seed()
        self.trial_id = service.create_trial(
            "coord", "中期锁库接口试验", "v1.0", ["A", "B"], ["risk"], 4, "seed-http-0001"
        )["id"]
        service.start_trial("coord", self.trial_id)
        service.enroll("site1", self.trial_id, "S001-001", {"risk": "low"})
        self.server = RandomizationServer(("127.0.0.1", 0), service)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def _call(self, method, path, user, payload=None):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method,
            headers={"X-User-Id": user, **({"Content-Type": "application/json"} if data else {})},
        )
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_lock_batch_flow_over_http(self):
        # 中心用户看不到批次列表
        status, body = self._call("GET", f"/api/trials/{self.trial_id}/lock-batches", "site1")
        self.assertEqual(status, 403)
        self.assertEqual(body["error"]["code"], "forbidden")

        # 协调员发起
        status, batch = self._call("POST", f"/api/trials/{self.trial_id}/lock-batches/initiate", "coord", {"reason": "中期分析"})
        self.assertEqual(status, 201)
        self.assertEqual(batch["status"], "pending")
        bid = batch["id"]

        # 暂停期间入组 423
        status, body = self._call("POST", f"/api/trials/{self.trial_id}/enroll", "site1",
                                  {"external_id": "S001-002", "factors": {"risk": "low"}})
        self.assertEqual(status, 423)
        self.assertEqual(body["error"]["code"], "enrollment_paused")

        # 历史列表与详情
        status, body = self._call("GET", f"/api/trials/{self.trial_id}/lock-batches", "monitor1")
        self.assertEqual(status, 200)
        self.assertEqual(len(body["items"]), 1)
        status, body = self._call("GET", f"/api/lock-batches/{bid}", "monitor2")
        self.assertEqual(status, 200)
        self.assertEqual(body["id"], bid)

        # 监查员复核通过
        status, body = self._call("POST", f"/api/lock-batches/{bid}/review", "monitor1", {"approve": True, "note": "一致"})
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "completed")
        self.assertEqual(body["snapshot"]["participant_count"], 1)
        self.assertEqual(body["snapshot"]["sites"], [{"site_id": "S001", "count": 1}])

        # 入组恢复
        status, body = self._call("POST", f"/api/trials/{self.trial_id}/enroll", "site1",
                                  {"external_id": "S001-002", "factors": {"risk": "low"}})
        self.assertEqual(status, 201)

        # 404
        status, body = self._call("GET", "/api/lock-batches/9999", "coord")
        self.assertEqual(status, 404)

    def test_compat_store_facade_still_works(self):
        # 历史代码直接使用 app.RandomizationStore 的路径不被破坏
        store = RandomizationStore(Path(self.tmp.name) / "compat.db")
        store.seed()
        trial = store.create_trial("coord", "兼容门面试验", "v1", ["A", "B"], ["risk"], 4, "seed-compat-01")
        store.start_trial("coord", trial["id"])
        self.assertEqual(store.enroll("site1", trial["id"], "X1", {"risk": "low"})["external_id"], "X1")


if __name__ == "__main__":
    unittest.main()
