#!/usr/bin/env python3
"""HTTP 服务：分配 API + 租约页面 + 一致性审计。

用法：
  python3 server.py [--db quota-ledger.db] [--host 127.0.0.1] [--port 8000]

API 一览：
  GET  / 或 /leases                租约控制台页面
  GET  /api/state                  全局快照（节点余额 + 租约）
  GET  /api/audit                  一致性审计报告
  GET  /api/events?limit=N         事件账本
  POST /api/root/capacity          {capacity}                配置/调整全局容量
  POST /api/nodes                  {parent_id, name, kind}   创建组织/项目/客户端
  POST /api/leases                 {holder_id, amount, ttl_seconds[, idempotency_key]}
  POST /api/leases/{id}/reduce     {new_amount}              父级缩容
  POST /api/leases/{id}/revoke     撤销（级联，消费不消失）
  POST /api/leases/{id}/return     归还（只释放未消费部分）
  POST /api/usage                  {event_id, lease_id, amount}  幂等扣减
  POST /api/sweep                  手动清算到期租约
"""
from __future__ import annotations

import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from app.service import DomainError, QuotaService
from app.web import INDEX_HTML


def make_handler(service: QuotaService):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        # ---------- 基础工具 ----------
        def _send(self, code: int, body: bytes, ctype: str):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, code: int, obj):
            self._send(code, json.dumps(obj, ensure_ascii=False).encode(),
                       "application/json; charset=utf-8")

        def _html(self, html: str):
            self._send(200, html.encode(), "text/html; charset=utf-8")

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            return json.loads(self.rfile.read(length) or b"{}")

        def _handle(self, method: str):
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            parts = [p for p in path.split("/") if p]
            try:
                if method == "GET":
                    self._get(path, parts, parse_qs(parsed.query))
                else:
                    self._post(parts, self._body())
            except DomainError as exc:
                self._json(exc.status,
                           {"error": {"code": exc.code, "message": exc.message}})
            except json.JSONDecodeError:
                self._json(400, {"error": {"code": "bad_json",
                                           "message": "请求体不是合法 JSON"}})
            except Exception as exc:  # noqa: BLE001
                self._json(500, {"error": {"code": "internal",
                                           "message": str(exc)}})

        # ---------- 路由 ----------
        def _get(self, path, parts, query):
            if path in ("/", "/leases"):
                return self._html(INDEX_HTML)
            if parts == ["api", "state"]:
                return self._json(200, service.snapshot())
            if parts == ["api", "audit"]:
                return self._json(200, service.audit())
            if parts == ["api", "events"]:
                limit = int(query.get("limit", ["100"])[0])
                return self._json(200, service.events_tail(min(limit, 1000)))
            self._json(404, {"error": {"code": "not_found",
                                       "message": f"未知路径: {path}"}})

        def _post(self, parts, body):
            if parts == ["api", "root", "capacity"]:
                return self._json(200, service.configure_root(body.get("capacity")))
            if parts == ["api", "nodes"]:
                return self._json(201, service.create_node(
                    body.get("parent_id", ""), body.get("name", ""),
                    body.get("kind", "")))
            if parts == ["api", "leases"]:
                return self._json(201, service.request_lease(
                    body.get("holder_id", ""), body.get("amount"),
                    body.get("ttl_seconds"), body.get("idempotency_key")))
            if parts == ["api", "usage"]:
                return self._json(200, service.record_usage(
                    body.get("event_id", ""), body.get("lease_id", ""),
                    body.get("amount")))
            if parts == ["api", "sweep"]:
                return self._json(200, service.sweep())
            if len(parts) == 4 and parts[:2] == ["api", "leases"]:
                lease_id, action = parts[2], parts[3]
                if action == "reduce":
                    return self._json(200, service.reduce_lease(
                        lease_id, body.get("new_amount")))
                if action == "revoke":
                    return self._json(200, service.close_lease(lease_id, "revoked"))
                if action == "return":
                    return self._json(200, service.close_lease(lease_id, "returned"))
            self._json(404, {"error": {"code": "not_found",
                                       "message": "未知接口"}})

        do_GET = lambda self: self._handle("GET")      # noqa: E731
        do_POST = lambda self: self._handle("POST")    # noqa: E731

        def log_message(self, fmt, *args):  # 静默访问日志
            pass

    return Handler


def sweeper_loop(service: QuotaService, stop: threading.Event):
    """后台清算线程：客户端离线 → 租约到期 → 自动释放未消费部分。"""
    while not stop.wait(0.5):
        try:
            service.sweep()
        except Exception:  # noqa: BLE001
            pass


def main():
    parser = argparse.ArgumentParser(description="多租户 API 配额租约服务")
    parser.add_argument("--db", default="quota-ledger.db", help="事件账本 SQLite 文件")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    service = QuotaService(args.db)  # 启动即重放事件账本，恢复余额
    stop = threading.Event()
    sweeper = threading.Thread(target=sweeper_loop, args=(service, stop),
                               daemon=True)
    sweeper.start()

    server = ThreadingHTTPServer((args.host, args.port), make_handler(service))
    print(f"事件账本: {args.db}")
    print(f"租约控制台: http://{args.host}:{args.port}/  (租约页: /leases)")
    print(f"审计报告:   http://{args.host}:{args.port}/api/audit")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.shutdown()
        service.close()


if __name__ == "__main__":
    main()
