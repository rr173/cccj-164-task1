"""一致性审计：从事件账本独立重放，校验全部不变量。

审计项：
  1. ledger_replay_matches_memory  账本重放结果与在线内存状态一致
  2. global_conservation           全局守恒：未发行 + 活跃租约余额 + 已消费 == 全局容量
  3. non_negative_balances         无负余额（不超卖的直接证据）
  4. lease_budget_bounds           每张租约：已消费+余额+已转授 ≤ 面额
  5. usage_idempotency             用量事件无重复扣减；逐租约用量合计 == 已消费
  6. ttl_nesting_and_close_order   子租约到期不晚于资金租约；关闭顺序先子后父
  7. funding_integrity             资金租约的持有者必须是租约授予方
"""
from __future__ import annotations

from .service import EV_CLOSED, EV_GRANT, EV_USAGE, State


def _check(name: str, ok: bool, detail: str) -> dict:
    return {"name": name, "ok": ok, "detail": detail}


def run_audit(live: State, events: list[tuple[str, dict]], now: float) -> dict:
    checks: list[dict] = []

    # 1. 独立重放账本，与在线状态比对
    replayed = State()
    replay_error = None
    try:
        for typ, payload in events:
            replayed.apply(typ, payload)
    except Exception as exc:  # 账本损坏
        replay_error = exc
    if replay_error is not None:
        checks.append(_check("ledger_replay_matches_memory", False,
                             f"账本重放失败: {replay_error}"))
        return _report(checks, events, now)
    replay_ok = replayed.to_plain() == live.to_plain()
    checks.append(_check(
        "ledger_replay_matches_memory", replay_ok,
        "账本重放状态与在线状态一致" if replay_ok
        else "账本重放状态与在线状态不一致（存在未入账的状态变更）"))

    s = live
    active = [l for l in s.leases.values() if l.status == "active"]
    closed = [l for l in s.leases.values() if l.status == "closed"]

    # 2. 全局守恒
    active_remaining = sum(l.remaining for l in active)
    total = s.root_unissued + active_remaining + s.total_burned
    ok = total == s.root_capacity
    checks.append(_check(
        "global_conservation", ok,
        f"未发行 {s.root_unissued} + 活跃租约余额 {active_remaining} "
        f"+ 已消费 {s.total_burned} = {total}，全局容量 {s.root_capacity}"))

    # 3. 无负余额
    negatives = [l.id for l in s.leases.values()
                 if l.remaining < 0 or l.consumed < 0]
    if s.root_unissued < 0:
        negatives.append("root_unissued")
    checks.append(_check(
        "non_negative_balances", not negatives,
        "全部余额非负" if not negatives else f"负余额: {negatives}"))

    # 4. 租约面额约束
    def children_amount(lease_id: str) -> int:
        return sum(l.amount for l in active
                   if l.funding_lease_id == lease_id)

    violations = []
    for l in s.leases.values():
        if l.status == "active":
            used = l.consumed + l.remaining + children_amount(l.id)
            if used > l.amount:
                violations.append(f"{l.id}(占用{used}>面额{l.amount})")
        else:
            if l.consumed + l.released > l.amount:
                violations.append(f"{l.id}(消费{l.consumed}+退回{l.released}"
                                  f">面额{l.amount})")
    checks.append(_check(
        "lease_budget_bounds", not violations,
        "每张租约：已消费+余额+已转授 ≤ 面额" if not violations
        else f"违例: {violations}"))

    # 5. 用量幂等 + 逐租约合计
    usage_events = [p for t, p in events if t == EV_USAGE]
    event_ids = [p["event_id"] for p in usage_events]
    dup = len(event_ids) != len(set(event_ids))
    per_lease: dict[str, int] = {}
    for p in usage_events:
        per_lease[p["lease_id"]] = per_lease.get(p["lease_id"], 0) + p["amount"]
    mismatch = [lid for lid, total_used in per_lease.items()
                if lid in s.leases and s.leases[lid].consumed != total_used]
    ok = not dup and not mismatch
    detail = f"{len(usage_events)} 条用量事件，event_id 无重复，逐租约合计一致"
    if dup:
        detail = "存在重复入账的 event_id"
    elif mismatch:
        detail = f"租约消费额与用量事件合计不一致: {mismatch}"
    checks.append(_check("usage_idempotency", ok, detail))

    # 6. TTL 嵌套与关闭顺序
    bad_ttl, bad_order = [], []
    for l in s.leases.values():
        if l.funding_lease_id is None:
            continue
        funding = s.leases.get(l.funding_lease_id)
        if funding is None:
            bad_ttl.append(l.id)
            continue
        if l.expires_at > funding.expires_at + 1e-6:
            bad_ttl.append(l.id)
        if l.closed_at is not None and funding.closed_at is not None \
                and l.closed_at > funding.closed_at + 1e-6:
            bad_order.append(l.id)
    ok = not bad_ttl and not bad_order
    detail = "子租约到期均不晚于资金租约，关闭顺序先子后父"
    if bad_ttl:
        detail = f"TTL 超出资金租约: {bad_ttl}"
    elif bad_order:
        detail = f"关闭顺序违例（父先于子）: {bad_order}"
    checks.append(_check("ttl_nesting_and_close_order", ok, detail))

    # 7. 资金完整性
    bad_funding = []
    for l in s.leases.values():
        if l.funding_lease_id is None:
            if l.granter_id != "root":
                bad_funding.append(l.id)
        else:
            funding = s.leases.get(l.funding_lease_id)
            if funding is None or funding.holder_id != l.granter_id:
                bad_funding.append(l.id)
    checks.append(_check(
        "funding_integrity", not bad_funding,
        "资金租约持有者与授予方一致" if not bad_funding
        else f"资金来源异常: {bad_funding}"))

    return _report(checks, events, now)


def _report(checks: list[dict], events: list[tuple[str, dict]],
            now: float) -> dict:
    return {
        "ok": all(c["ok"] for c in checks),
        "checks": checks,
        "event_count": len(events),
        "generated_at": now,
    }
