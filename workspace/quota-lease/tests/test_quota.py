"""需求覆盖测试：

  层级分配 / 父级额度约束 / 并发不超卖 / 幂等扣减 / 归还与到期只释放未消费 /
  父级缩容 / 客户端离线 / 撤销级联且消费不消失 / 重启从账本恢复 / 一致性审计
"""
import os
import sqlite3
import tempfile
import threading
import unittest

from app.service import ConflictError, QuotaService, ValidationError


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "test.db")
        self.clock = Clock()
        self.svc = QuotaService(self.db, now_fn=self.clock)
        self.svc.configure_root(1_000_000)
        self.org = self.svc.create_node("root", "acme-org", "org")
        self.proj = self.svc.create_node(self.org["id"], "search", "project")
        self.cli = self.svc.create_node(self.proj["id"], "ios-app", "client")

    def tearDown(self):
        self.svc.close()
        self.tmp.cleanup()

    def grant_chain(self, org_amt=100_000, proj_amt=60_000, cli_amt=30_000,
                    ttl=3600):
        lo = self.svc.request_lease(self.org["id"], org_amt, ttl)
        lp = self.svc.request_lease(self.proj["id"], proj_amt, ttl)
        lc = self.svc.request_lease(self.cli["id"], cli_amt, ttl)
        return lo, lp, lc

    def available(self, node_id):
        return self.svc.node_view(node_id)["available"]


class TestAllocation(Base):
    def test_grant_within_parent_available(self):
        lo, lp, lc = self.grant_chain()
        self.assertEqual(self.svc.root_view()["unissued"], 900_000)
        self.assertEqual(self.available(self.org["id"]), 40_000)
        self.assertEqual(self.available(self.proj["id"]), 30_000)
        self.assertEqual(self.available(self.cli["id"]), 30_000)

    def test_oversell_rejected(self):
        self.svc.request_lease(self.org["id"], 100_000, 3600)
        with self.assertRaises(ConflictError):
            self.svc.request_lease(self.org["id"], 900_001, 3600)
        # 项目只能花组织租约里的钱
        with self.assertRaises(ConflictError):
            self.svc.request_lease(self.proj["id"], 100_001, 3600)

    def test_hierarchy_kind_enforced(self):
        with self.assertRaises(ValidationError):
            self.svc.create_node("root", "bad", "client")
        with self.assertRaises(ValidationError):
            self.svc.create_node(self.proj["id"], "bad", "org")

    def test_lease_idempotency_key(self):
        a = self.svc.request_lease(self.org["id"], 5_000, 60, idem_key="req-1")
        b = self.svc.request_lease(self.org["id"], 5_000, 60, idem_key="req-1")
        self.assertEqual(a["id"], b["id"])
        self.assertTrue(b["dedup"])
        self.assertEqual(self.svc.root_view()["unissued"], 995_000)  # 只发了一次

    def test_root_shrink_limited_by_issued(self):
        self.svc.request_lease(self.org["id"], 100_000, 3600)
        # 已发行未回收 10 万，全局容量最多缩到 10 万
        with self.assertRaises(ConflictError):
            self.svc.configure_root(99_999)
        view = self.svc.configure_root(100_000)  # 恰好等于已发行，允许
        self.assertEqual(view["unissued"], 0)
        view = self.svc.configure_root(2_000_000)  # 扩容 ok
        self.assertEqual(view["unissued"], 1_900_000)


class TestConcurrency(Base):
    def test_cross_level_concurrent_no_oversell(self):
        """跨层并发申请：组织/项目/客户端同时抢，总额不得超过父级可用。"""
        self.svc.request_lease(self.org["id"], 100_000, 3600)
        self.svc.request_lease(self.proj["id"], 60_000, 3600)

        results, lock = {"proj_ok": 0, "cli_ok": 0, "fail": 0}, threading.Lock()

        def grab_project():
            try:
                self.svc.request_lease(self.proj["id"], 10_000, 3600)
                with lock:
                    results["proj_ok"] += 1
            except ConflictError:
                with lock:
                    results["fail"] += 1

        def grab_client():
            try:
                self.svc.request_lease(self.cli["id"], 10_000, 3600)
                with lock:
                    results["cli_ok"] += 1
            except ConflictError:
                with lock:
                    results["fail"] += 1

        threads = ([threading.Thread(target=grab_project) for _ in range(8)] +
                   [threading.Thread(target=grab_client) for _ in range(12)])
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 项目只能从组织租约剩余的 40k 里拿 → 恰好 4 笔（与交织顺序无关）
        self.assertEqual(results["proj_ok"], 4)
        # 客户端从项目持有的租约里拿：项目最终持有 60k+40k，
        # 但客户端申请可能发生在项目增资之前 → 成功数 ∈ [6, 10]
        self.assertGreaterEqual(results["cli_ok"], 6)
        self.assertLessEqual(results["cli_ok"], 6 + results["proj_ok"])
        # 核心不变量：任何时刻都不超卖
        self.assertEqual(self.available(self.org["id"]), 0)
        self.assertGreaterEqual(self.available(self.proj["id"]), 0)
        snap = self.svc.snapshot()
        cli_total = sum(l["amount"] for l in snap["leases"]
                        if l["holder_id"] == self.cli["id"] and l["status"] == "active")
        proj_total = sum(l["amount"] for l in snap["leases"]
                         if l["holder_id"] == self.proj["id"] and l["status"] == "active")
        self.assertLessEqual(cli_total, proj_total)  # 下级总额 ≤ 上级持有
        audit = self.svc.audit()
        self.assertTrue(audit["ok"], audit)


class TestUsage(Base):
    def test_idempotent_deduction(self):
        _, _, lc = self.grant_chain()
        r1 = self.svc.record_usage("evt-1", lc["id"], 5_000)
        self.assertFalse(r1["dedup"])
        r2 = self.svc.record_usage("evt-1", lc["id"], 5_000)  # 重复回执
        self.assertTrue(r2["dedup"])
        lease = self.svc.lease_view(lc["id"])
        self.assertEqual(lease["consumed"], 5_000)  # 只扣了一次
        self.assertEqual(self.svc.root_view()["burned"], 5_000)

    def test_over_remaining_rejected(self):
        _, _, lc = self.grant_chain()
        with self.assertRaises(ConflictError):
            self.svc.record_usage("evt-x", lc["id"], 30_001)

    def test_usage_on_closed_lease_rejected(self):
        _, _, lc = self.grant_chain()
        self.svc.close_lease(lc["id"], "returned")
        with self.assertRaises(ConflictError):
            self.svc.record_usage("evt-y", lc["id"], 1)


class TestReleaseAndExpiry(Base):
    def test_return_releases_only_unconsumed(self):
        lo, lp, lc = self.grant_chain()
        self.svc.record_usage("evt-1", lc["id"], 8_000)
        view = self.svc.close_lease(lc["id"], "returned")
        self.assertEqual(view["released"], 22_000)          # 只释放未消费部分
        self.assertEqual(self.available(self.proj["id"]), 52_000)  # 30k+22k
        self.assertEqual(self.svc.root_view()["burned"], 8_000)    # 消费不消失

    def test_offline_client_expiry_releases_unconsumed(self):
        """客户端离线：租约到期后未消费部分自动回到项目余额。"""
        lo = self.svc.request_lease(self.org["id"], 100_000, 3600)
        lp = self.svc.request_lease(self.proj["id"], 60_000, 3600)
        lc = self.svc.request_lease(self.cli["id"], 30_000, 10)  # 10 秒 TTL
        self.svc.record_usage("evt-1", lc["id"], 4_000)
        self.clock.advance(11)  # 客户端掉线，租约到期
        self.svc.sweep()
        self.assertEqual(self.svc.lease_view(lc["id"])["close_reason"], "expired")
        self.assertEqual(self.available(self.proj["id"]), 56_000)  # 30k+26k
        self.assertEqual(self.svc.root_view()["burned"], 4_000)

    def test_revoke_cascades_and_consumption_survives(self):
        """父级撤销 → 级联关闭下级；已确认消费不消失，未消费逐级退回。"""
        lo, lp, lc = self.grant_chain()
        self.svc.record_usage("evt-1", lc["id"], 8_000)
        self.svc.close_lease(lo["id"], "revoked")  # 撤销组织租约
        self.assertEqual(self.svc.lease_view(lc["id"])["close_reason"], "cascade")
        self.assertEqual(self.svc.lease_view(lp["id"])["close_reason"], "cascade")
        self.assertEqual(self.svc.lease_view(lo["id"])["close_reason"], "revoked")
        root = self.svc.root_view()
        self.assertEqual(root["burned"], 8_000)               # 消费仍在
        self.assertEqual(root["unissued"], 1_000_000 - 8_000)  # 其余全部回收
        self.assertTrue(self.svc.audit()["ok"])

    def test_close_is_idempotent(self):
        _, _, lc = self.grant_chain()
        self.svc.close_lease(lc["id"], "revoked")
        again = self.svc.close_lease(lc["id"], "revoked")  # 重复撤销不报错
        self.assertEqual(again["status"], "closed")
        # 30k 退回到项目租约，root 未发行额不变（仍是 1M - 100k 组织租约）
        self.assertEqual(self.svc.root_view()["unissued"], 900_000)
        self.assertEqual(self.available(self.proj["id"]), 60_000)


class TestShrink(Base):
    def test_reduce_releases_difference(self):
        lo, lp, lc = self.grant_chain()
        self.svc.record_usage("evt-1", lc["id"], 8_000)
        view = self.svc.reduce_lease(lc["id"], 10_000)  # 30k → 10k
        self.assertEqual(view["remaining"], 2_000)      # 10k - 8k 已消费
        self.assertEqual(self.available(self.proj["id"]), 50_000)  # 30k+20k

    def test_reduce_below_committed_rejected(self):
        lo, lp, lc = self.grant_chain()
        self.svc.record_usage("evt-1", lc["id"], 8_000)
        with self.assertRaises(ConflictError):
            self.svc.reduce_lease(lc["id"], 5_000)  # 低于已消费 8k
        # 项目租约已转授 30k 给客户端，不能缩到 30k 以下
        with self.assertRaises(ConflictError):
            self.svc.reduce_lease(lp["id"], 20_000)
        self.svc.reduce_lease(lp["id"], 30_000)  # 恰好等于已转授，允许

    def test_ttl_nesting_enforced(self):
        self.svc.request_lease(self.org["id"], 100_000, 100)
        with self.assertRaises(ConflictError):
            self.svc.request_lease(self.proj["id"], 10_000, 200)  # 超过父租约


class TestRecovery(Base):
    def test_restart_rebuilds_balances(self):
        lo, lp, lc = self.grant_chain()
        self.svc.record_usage("evt-1", lc["id"], 8_000)
        self.svc.record_usage("evt-2", lc["id"], 3_000)
        self.svc.reduce_lease(lp["id"], 50_000)
        before = self.svc.snapshot()
        self.svc.close()

        # 模拟重启：同一账本文件，全新进程状态
        svc2 = QuotaService(self.db, now_fn=self.clock)
        after = svc2.snapshot()
        self.assertEqual(before["root"], after["root"])
        self.assertEqual({n["id"]: n["available"] for n in before["nodes"]},
                         {n["id"]: n["available"] for n in after["nodes"]})
        self.assertEqual({l["id"]: (l["remaining"], l["consumed"], l["status"])
                          for l in before["leases"]},
                         {l["id"]: (l["remaining"], l["consumed"], l["status"])
                          for l in after["leases"]})
        # 重启后幂等记录也恢复：重复回执不会二次扣减
        dup = svc2.record_usage("evt-1", lc["id"], 8_000)
        self.assertTrue(dup["dedup"])
        self.assertEqual(svc2.lease_view(lc["id"])["consumed"], 11_000)
        svc2.close()

    def test_expiry_during_downtime_swept_on_boot(self):
        lo = self.svc.request_lease(self.org["id"], 100_000, 10_000)
        lp = self.svc.request_lease(self.proj["id"], 60_000, 10_000)
        lc = self.svc.request_lease(self.cli["id"], 30_000, 50)
        self.svc.record_usage("evt-1", lc["id"], 7_000)
        self.svc.close()

        self.clock.advance(100)  # 宕机 100 秒，客户端租约早已到期
        svc2 = QuotaService(self.db, now_fn=self.clock)  # 启动即清算
        self.assertEqual(svc2.lease_view(lc["id"])["close_reason"], "expired")
        self.assertEqual(svc2.node_view(self.proj["id"])["available"], 53_000)
        self.assertEqual(svc2.root_view()["burned"], 7_000)
        svc2.close()


class TestAudit(Base):
    def test_audit_passes_after_full_scenario(self):
        lo, lp, lc = self.grant_chain()
        self.svc.record_usage("evt-1", lc["id"], 8_000)
        self.svc.record_usage("evt-1", lc["id"], 8_000)  # 重复
        self.svc.record_usage("evt-2", lc["id"], 2_000)
        self.svc.reduce_lease(lc["id"], 20_000)
        self.svc.close_lease(lp["id"], "revoked")  # 级联
        self.clock.advance(7200)
        self.svc.sweep()
        report = self.svc.audit()
        for check in report["checks"]:
            self.assertTrue(check["ok"], check)
        self.assertTrue(report["ok"])

    def test_audit_detects_ledger_tampering(self):
        _, _, lc = self.grant_chain()
        self.svc.record_usage("evt-1", lc["id"], 8_000)
        self.svc.close()
        # 篡改账本：伪造一条超额用量事件
        conn = sqlite3.connect(self.db)
        conn.execute(
            "INSERT INTO events(ts,type,payload) VALUES(?,?,?)",
            (self.clock(), "usage_recorded",
             '{"event_id":"evt-evil","lease_id":"%s","amount":999999}' % lc["id"]))
        conn.commit()
        conn.close()
        svc2 = QuotaService(self.db, now_fn=self.clock)
        report = svc2.audit()
        self.assertFalse(report["ok"])
        failed = {c["name"] for c in report["checks"] if not c["ok"]}
        self.assertIn("non_negative_balances", failed)
        svc2.close()


if __name__ == "__main__":
    unittest.main()
