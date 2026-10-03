#!/usr/bin/env python3
"""端到端演示：分配 → 租约 → 幂等扣减 → 缩容/并发/离线/撤销 → 审计 → 重启恢复。

运行：python3 demo.py
"""
import os
import tempfile
import threading
import time

from app.service import ConflictError, QuotaService

DB = os.path.join(tempfile.gettempdir(), "quota-demo.db")
for suffix in ("", "-wal", "-shm"):
    if os.path.exists(DB + suffix):
        os.remove(DB + suffix)


def show(svc, *node_ids):
    root = svc.root_view()
    print(f"    平台: 容量 {root['capacity']:,} | 未发行 {root['unissued']:,} | "
          f"已消费 {root['burned']:,}")
    for nid, label in node_ids:
        v = svc.node_view(nid)
        print(f"    {label}: 可用 {v['available']:,} | 已授予下级 {v['granted_out']:,} | "
              f"子树消费 {v['burned_subtree']:,}")


def main():
    svc = QuotaService(DB)

    print("== 1. 平台配置全局容量 1,000,000，建立 组织→项目→客户端 层级 ==")
    svc.configure_root(1_000_000)
    org = svc.create_node("root", "acme-org", "org")
    proj = svc.create_node(org["id"], "search-api", "project")
    cli = svc.create_node(proj["id"], "ios-app", "client")
    nodes = ((org["id"], "组织  "), (proj["id"], "项目  "), (cli["id"], "客户端"))

    print("\n== 2. 逐级申请租约：组织 100k/60m ← 项目 60k/50m ← 客户端 30k/10m ==")
    print("    （子租约有效期不得超过父租约剩余有效期）")
    svc.request_lease(org["id"], 100_000, 3600)
    svc.request_lease(proj["id"], 60_000, 3000)
    lc = svc.request_lease(cli["id"], 30_000, 600)
    show(svc, *nodes)

    print("\n== 3. 超卖拦截：客户端再申请 30,001（超出父级可用额度）==")
    try:
        svc.request_lease(cli["id"], 30_001, 600)
    except ConflictError as e:
        print(f"    409 拒绝: {e.message}")

    print("\n== 4. 幂等扣减：上报用量 evt-001 = 8,000，重复回执只扣一次 ==")
    r1 = svc.record_usage("evt-001", lc["id"], 8_000)
    r2 = svc.record_usage("evt-001", lc["id"], 8_000)
    print(f"    首次: remaining={r1['remaining']:,} | 重复回执: dedup={r2['dedup']}")
    show(svc, *nodes)

    print("\n== 5. 父级缩容：客户端租约 30k → 10k（已消费 8k，差额 20k 退回项目）==")
    svc.reduce_lease(lc["id"], 10_000)
    show(svc, *nodes)
    print("    试图缩到 5,000（低于已消费 8,000）:")
    try:
        svc.reduce_lease(lc["id"], 5_000)
    except ConflictError as e:
        print(f"    409 拒绝: {e.message}")

    print("\n== 6. 跨层并发：20 个线程同时向项目申请 5,000（项目可用 50,000）==")
    ok = {"n": 0}
    lock = threading.Lock()

    def grab():
        try:
            svc.request_lease(cli["id"], 5_000, 300)
            with lock:
                ok["n"] += 1
        except ConflictError:
            pass

    threads = [threading.Thread(target=grab) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    print(f"    成功 {ok['n']} 笔，拒绝 {20 - ok['n']} 笔（不超卖）")
    assert ok["n"] == 10
    show(svc, *nodes)

    print("\n== 7. 客户端离线：租约到期，未消费部分自动归还项目 ==")
    try:
        offline = svc.request_lease(cli["id"], 4_000, 1)
    except ConflictError as e:
        print(f"    项目可用已耗尽（{e.message}），先归还一笔 5k 租约 ……")
        extra = [l for l in svc.snapshot()["leases"]
                 if l["holder_id"] == cli["id"] and l["status"] == "active"
                 and l["amount"] == 5_000][0]
        svc.close_lease(extra["id"], "returned")
        offline = svc.request_lease(cli["id"], 4_000, 1)
    svc.record_usage("evt-offline-1", offline["id"], 1_500)
    print(f"    离线客户端租约 {offline['id'][:12]}… 消费 1,500 后掉线")
    time.sleep(1.3)
    svc.sweep()
    v = svc.lease_view(offline["id"])
    print(f"    租约状态: {v['status']}({v['close_reason']})，退回未消费 {v['released']:,}")
    show(svc, *nodes)

    print("\n== 8. 撤销项目租约：级联关闭全部下级，已确认消费不消失 ==")
    lp = [l for l in svc.snapshot()["leases"]
          if l["holder_id"] == proj["id"] and l["status"] == "active"][0]
    svc.close_lease(lp["id"], "revoked")
    show(svc, *nodes)

    print("\n== 9. 一致性审计 ==")
    report = svc.audit()
    for c in report["checks"]:
        print(f"    {'✓' if c['ok'] else '✗'} {c['name']}: {c['detail']}")
    assert report["ok"]

    before = svc.snapshot()
    svc.close()

    print("\n== 10. 模拟服务重启：从事件账本重建余额 ==")
    svc2 = QuotaService(DB)
    after = svc2.snapshot()
    same = (before["root"] == after["root"] and
            {n["id"]: n["available"] for n in before["nodes"]} ==
            {n["id"]: n["available"] for n in after["nodes"]})
    print(f"    重启前后余额一致: {same}")
    print(f"    账本事件数: {svc2.audit()['event_count']}")
    show(svc2, *nodes)
    assert same
    svc2.close()
    print("\n演示完成 ✓")


if __name__ == "__main__":
    main()
