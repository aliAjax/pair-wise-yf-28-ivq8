"""HTTP 展示层：路由、请求解析与 JSON 响应。

业务规则见 service.RandomizationService，数据存取见 repository.RandomizationStore。
为兼容历史导入（测试与脚本仍 `from app import RandomizationStore/BusinessError`），
这里保留同名转发。
"""
from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import repository as repo
from errors import BusinessError
from repository import BASE_DIR, DEFAULT_DB, MAX_ARM_LENGTH
from service import RandomizationService

# 兼容历史用法：旧脚本/测试直接在 RandomizationStore 上调用业务方法。
# 新代码应分别使用 repo.RandomizationStore（数据）与 RandomizationService（事务）。
RandomizationStore = type(
    "RandomizationStore", (RandomizationService,),
    {"__init__": lambda self, db_path=DEFAULT_DB: RandomizationService.__init__(self, repo.RandomizationStore(db_path))},
)

__all__ = ["BusinessError", "RandomizationStore", "RandomizationService", "Handler",
           "RandomizationServer", "main", "DEFAULT_DB", "BASE_DIR", "MAX_ARM_LENGTH"]


class Handler(BaseHTTPRequestHandler):
    server_version = "Randomization/1.0"

    def _service(self):
        return self.server.service  # type: ignore[attr-defined]

    def _body(self):
        length = int(self.headers.get("Content-Length", "0"))
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise BusinessError("请求体必须是合法 JSON", 400, "invalid_json")
        if not isinstance(data, dict):
            raise BusinessError("JSON 顶层必须是对象", 422, "invalid_json")
        return data

    def _send(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _dispatch(self, method):
        path = urlparse(self.path).path.rstrip("/") or "/"
        parts = [p for p in path.split("/") if p]
        user = self.headers.get("X-User-Id", "")
        svc = self._service()
        if method == "GET" and path == "/":
            body = (BASE_DIR / "web" / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if method == "GET" and path == "/health":
            return self._send(200, {"ok": True})
        if parts == ["api", "trials"] and method == "POST":
            d = self._body()
            return self._send(201, svc.create_trial(user, d.get("name", ""), d.get("protocol_version", ""), d.get("arms"), d.get("strata_factors"), d.get("block_size"), d.get("seed", "")))
        if len(parts) >= 3 and parts[:2] == ["api", "trials"]:
            trial_id = int(parts[2])
            if len(parts) == 4 and parts[3] == "protocol" and method == "POST":
                d = self._body()
                return self._send(200, svc.update_protocol(user, trial_id, d.get("protocol_version", ""), d.get("arms"), d.get("strata_factors"), d.get("block_size"), d.get("seed")))
            if len(parts) == 4 and parts[3] == "start" and method == "POST":
                return self._send(200, svc.start_trial(user, trial_id))
            if len(parts) == 4 and parts[3] == "participants" and method == "GET":
                return self._send(200, {"items": svc.list_participants(user, trial_id)})
            if len(parts) == 4 and parts[3] == "enroll" and method == "POST":
                d = self._body()
                return self._send(201, svc.enroll(user, trial_id, d.get("external_id", ""), d.get("factors", {})))
            if len(parts) == 4 and parts[3] == "summary" and method == "GET":
                return self._send(200, svc.trial_summary(user, trial_id))
            if len(parts) == 4 and parts[3] == "lock-batches" and method == "GET":
                return self._send(200, {"items": svc.list_lock_batches(user, trial_id)})
            if len(parts) == 5 and parts[3] == "lock-batches" and parts[4] == "initiate" and method == "POST":
                d = self._body()
                return self._send(201, svc.initiate_lock_batch(user, trial_id, d.get("reason", "")))
        if len(parts) == 3 and parts[:2] == ["api", "lock-batches"] and parts[2].isdigit():
            batch_id = int(parts[2])
            if method == "GET":
                return self._send(200, svc.get_lock_batch(user, batch_id))
        if len(parts) == 4 and parts[:2] == ["api", "lock-batches"] and parts[2].isdigit() and parts[3] == "review" and method == "POST":
            d = self._body()
            return self._send(200, svc.review_lock_batch(user, int(parts[2]), bool(d.get("approve")), d.get("note", "")))
        if len(parts) == 3 and parts[:2] == ["api", "participants"] and method == "GET":
            return self._send(200, svc.get_participant(user, int(parts[2])))
        if len(parts) == 4 and parts[:2] == ["api", "participants"] and parts[3] == "unblinding-requests" and method == "POST":
            d = self._body()
            return self._send(201, svc.request_unblinding(user, int(parts[2]), d.get("reason", "")))
        if len(parts) == 4 and parts[:2] == ["api", "unblinding-requests"] and parts[3] == "approve" and method == "POST":
            return self._send(200, svc.approve_unblinding(user, int(parts[2])))
        raise BusinessError("接口不存在", 404, "not_found")

    def _handle(self, method):
        try:
            self._dispatch(method)
        except BusinessError as exc:
            self._send(exc.status, {"error": {"code": exc.code, "message": exc.message}})
        except (ValueError, TypeError):
            self._send(400, {"error": {"code": "invalid_path", "message": "路径参数格式错误"}})
        except Exception as exc:
            self._send(500, {"error": {"code": "internal_error", "message": str(exc)}})

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def log_message(self, fmt, *args):
        print(f"{self.address_string()} - {fmt % args}")


class RandomizationServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, service: RandomizationService):
        self.service = service
        self.store = service.store
        super().__init__(address, Handler)


def main():
    parser = argparse.ArgumentParser(description="临床试验随机分配与盲法服务")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--port", type=int, default=8104)
    parser.add_argument("--init", action="store_true")
    parser.add_argument("--seed", action="store_true")
    args = parser.parse_args()
    service = RandomizationService(repo.RandomizationStore(args.db))
    service.init_schema()
    if args.seed:
        service.seed()
    if args.init or args.seed:
        print(f"数据库已初始化: {args.db}")
        return
    server = RandomizationServer(("127.0.0.1", args.port), service)
    print(f"随机化服务运行于 http://127.0.0.1:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
