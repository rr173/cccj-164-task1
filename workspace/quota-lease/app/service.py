"""领域核心：层级分配 + 租约生命周期 + 幂等扣减 + 事件溯源恢复。

模型（纯租约制 / 代币转移）：
  - 平台(root)持有全局容量；容量只通过"带有效期的租约"沿
    组织(org) → 项目(project) → 客户端(client) 三级向下分配。
  - 授予租约：把 amount 个代币从授予方余额转入租约（父级可用额度即时扣减）。
  - 实际调用：按幂等事件从租约扣减（remaining -= x，代币销毁，total_burned += x）。
  - 归还 / 到期 / 撤销：只把未消费的 remaining 退回资金源；已确认消费永久保留。
  - 子租约的资金来源是父节点持有的某张租约（funding_lease_id），
    关闭时代币退回该租约；TTL 嵌套（子租约到期不晚于资金租约）+
    级联关闭（先子后父）保证退回路径始终有效。

守恒不变量（审计核心）：
  root_unissued + Σ(活跃租约 remaining) + total_burned == root_capacity

并发：单写者。所有变更在 self._lock 内完成 "检查 → 写账本 → 应用"，
检查与应用之间没有窗口期，跨层并发申请不会超卖。
"""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import asdict, dataclass

from .ledger import Ledger

# ---------------------------------------------------------------- 错误类型


class DomainError(Exception):
    status = 400
    code = "bad_request"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class ValidationError(DomainError):
    status = 400
    code = "validation_error"


class NotFoundError(DomainError):
    status = 404
    code = "not_found"


class ConflictError(DomainError):
    status = 409
    code = "conflict"


# ---------------------------------------------------------------- 数据模型

KINDS = ("root", "org", "project", "client")
CHILD_KIND = {"root": "org", "org": "project", "project": "client"}

# 事件类型
EV_ROOT = "root_configured"
EV_NODE = "node_created"
EV_GRANT = "lease_granted"
EV_USAGE = "usage_recorded"
EV_REDUCED = "lease_reduced"
EV_CLOSED = "lease_closed"


@dataclass
class Node:
    id: str
    parent_id: str | None
    name: str
    kind: str  # root | org | project | client
    created_at: float


@dataclass
class Lease:
    id: str
    granter_id: str                 # 授予方节点 id（"root" 表示平台）
    holder_id: str                  # 持有方节点 id
    funding_lease_id: str | None    # 资金源租约；None = 平台资金池出资
    amount: int                     # 租约面额
    remaining: int                  # 未消费且未转授的余额
    consumed: int                   # 已被实际调用销毁的额度（不可逆）
    status: str                     # active | closed
    created_at: float
    expires_at: float
    closed_at: float | None = None
    close_reason: str | None = None  # returned | expired | revoked | cascade
    released: int = 0               # 关闭时退回资金源的额度
    idem_key: str | None = None


class State:
    """由事件账本完全推导出的内存状态（服务与审计共用同一套应用逻辑）。"""

    def __init__(self):
        self.nodes: dict[str, Node] = {}
        self.leases: dict[str, Lease] = {}
        self.root_capacity = 0
        self.root_unissued = 0
        self.total_burned = 0
        self.usage_dedup: dict[str, dict] = {}
        self.lease_idem: dict[str, str] = {}

    # -- 事件应用（重放与在线共用；只做事实应用，不做校验） ----------------

    def apply(self, typ: str, p: dict) -> None:
        if typ == EV_ROOT:
            if "root" not in self.nodes:
                self.nodes["root"] = Node("root", None, "platform", "root",
                                          p.get("ts", 0.0))
            delta = p["capacity"] - self.root_capacity
            self.root_capacity = p["capacity"]
            self.root_unissued += delta
        elif typ == EV_NODE:
            self.nodes[p["id"]] = Node(p["id"], p["parent_id"], p["name"],
                                       p["kind"], p["created_at"])
        elif typ == EV_GRANT:
            self.leases[p["id"]] = Lease(
                id=p["id"], granter_id=p["granter_id"], holder_id=p["holder_id"],
                funding_lease_id=p["funding_lease_id"], amount=p["amount"],
                remaining=p["amount"], consumed=0, status="active",
                created_at=p["created_at"], expires_at=p["expires_at"],
                idem_key=p.get("idem_key"))
            if p["funding_lease_id"] is None:
                self.root_unissued -= p["amount"]
            else:
                self.leases[p["funding_lease_id"]].remaining -= p["amount"]
            if p.get("idem_key"):
                self.lease_idem[p["idem_key"]] = p["id"]
        elif typ == EV_USAGE:
            lease = self.leases[p["lease_id"]]
            lease.remaining -= p["amount"]
            lease.consumed += p["amount"]
            self.total_burned += p["amount"]
            self.usage_dedup[p["event_id"]] = {
                "event_id": p["event_id"], "lease_id": p["lease_id"],
                "amount": p["amount"]}
        elif typ == EV_REDUCED:
            lease = self.leases[p["lease_id"]]
            lease.amount = p["new_amount"]
            lease.remaining -= p["released"]
            self._credit(lease, p["released"])
        elif typ == EV_CLOSED:
            lease = self.leases[p["lease_id"]]
            lease.status = "closed"
            lease.closed_at = p["closed_at"]
            lease.close_reason = p["reason"]
            lease.released = p["released"]
            lease.remaining = 0
            self._credit(lease, p["released"])
        else:
            raise ValueError(f"未知事件类型: {typ}")

    def _credit(self, lease: Lease, amount: int) -> None:
        """把退回的代币还给资金源（父租约或平台资金池）。"""
        if amount <= 0:
            return
        if lease.funding_lease_id is None:
            self.root_unissued += amount
            return
        funding = self.leases.get(lease.funding_lease_id)
        if funding is not None and funding.status == "active":
            funding.remaining += amount
        else:
            # 正常流程不会走到（级联关闭保证先子后父）；
            # 账本被篡改时兜底计入平台池，由审计的守恒检查发现。
            self.root_unissued += amount

    def to_plain(self) -> dict:
        return {
            "root_capacity": self.root_capacity,
            "root_unissued": self.root_unissued,
            "total_burned": self.total_burned,
            "nodes": {k: asdict(v) for k, v in sorted(self.nodes.items())},
            "leases": {k: asdict(v) for k, v in sorted(self.leases.items())},
            "usage_dedup": {k: self.usage_dedup[k]
                            for k in sorted(self.usage_dedup)},
            "lease_idem": dict(sorted(self.lease_idem.items())),
        }


# ---------------------------------------------------------------- 服务


def _as_int(value, field: str) -> int:
    if isinstance(value, bool):
        raise ValidationError(f"{field} 必须是整数")
    try:
        ivalue = int(value)
    except (TypeError, ValueError):
        raise ValidationError(f"{field} 必须是整数")
    if isinstance(value, float) and value != ivalue:
        raise ValidationError(f"{field} 必须是整数")
    return ivalue


class QuotaService:
    """配额租约服务。所有公开方法线程安全。"""

    def __init__(self, db_path: str, now_fn=time.time):
        self._now = now_fn
        self._lock = threading.RLock()
        self.ledger = Ledger(db_path)
        self.state = State()
        for _seq, _ts, typ, payload in self.ledger.replay():
            self.state.apply(typ, payload)
        # 宕机期间到期的租约立即清算（客户端离线场景）
        self._sweep_locked()

    def close(self) -> None:
        self.ledger.close()

    # ------------------------------------------------------------ 写操作

    def configure_root(self, capacity) -> dict:
        """设置/调整平台全局容量。缩容不得小于已发行未回收部分。"""
        with self._lock:
            capacity = _as_int(capacity, "capacity")
            if capacity < 0:
                raise ValidationError("capacity 不能为负")
            delta = capacity - self.state.root_capacity
            if self.state.root_unissued + delta < 0:
                raise ConflictError(
                    f"全局容量不能缩到 {capacity}：已发行未回收 "
                    f"{self.state.root_capacity - self.state.root_unissued}")
            self._emit(EV_ROOT, {"capacity": capacity, "ts": self._now()})
            return self.root_view()

    def create_node(self, parent_id: str, name: str, kind: str) -> dict:
        """在层级中创建组织/项目/客户端节点。"""
        with self._lock:
            parent = self._node(parent_id)
            if kind not in KINDS[1:]:
                raise ValidationError(f"kind 必须是 {KINDS[1:]} 之一")
            expected = CHILD_KIND[parent.kind]
            if kind != expected:
                raise ValidationError(f"{parent.kind} 下只能创建 {expected}")
            name = (name or "").strip()
            if not name:
                raise ValidationError("name 不能为空")
            node_id = f"{kind}_{uuid.uuid4().hex[:16]}"
            self._emit(EV_NODE, {"id": node_id, "parent_id": parent.id,
                                 "name": name, "kind": kind,
                                 "created_at": self._now()})
            return self.node_view(node_id)

    def request_lease(self, holder_id: str, amount, ttl_seconds,
                      idem_key: str | None = None) -> dict:
        """子级向父级申请带有效期的租约（分配 API 核心）。

        约束：金额不得超过父级当前可用额度；子租约到期不得晚于资金租约。
        idem_key 用于申请幂等（网络重试不产生重复租约）。
        """
        with self._lock:
            amount = _as_int(amount, "amount")
            ttl = _as_int(ttl_seconds, "ttl_seconds")
            if amount <= 0:
                raise ValidationError("amount 必须为正整数")
            if ttl <= 0:
                raise ValidationError("ttl_seconds 必须为正整数")
            if idem_key and idem_key in self.state.lease_idem:
                dup = self.lease_view(self.state.lease_idem[idem_key])
                dup["dedup"] = True
                return dup
            holder = self._node(holder_id)
            if holder.kind == "root":
                raise ValidationError("root 不能持有租约")
            self._sweep_locked()  # 先清算到期租约，再判定可用额度
            now = self._now()
            expires_at = now + ttl
            granter = self.state.nodes[holder.parent_id]
            if granter.kind == "root":
                if amount > self.state.root_unissued:
                    raise ConflictError(
                        f"平台可用额度不足：剩余 {self.state.root_unissued}，"
                        f"申请 {amount}")
                funding_id = None
            else:
                candidates = [l for l in self._held_active(granter.id)
                              if l.remaining >= amount and l.expires_at >= expires_at]
                if not candidates:
                    raise ConflictError(
                        f"父级 {granter.name} 可用额度不足或资金租约存续期不够")
                # 多张资金租约时，先用最快到期的
                funding_id = min(candidates, key=lambda l: l.expires_at).id
            lease_id = f"lse_{uuid.uuid4().hex[:16]}"
            self._emit(EV_GRANT, {
                "id": lease_id, "granter_id": granter.id, "holder_id": holder.id,
                "funding_lease_id": funding_id, "amount": amount,
                "created_at": now, "expires_at": expires_at,
                "idem_key": idem_key},
                lease_idem=(idem_key, lease_id) if idem_key else None)
            view = self.lease_view(lease_id)
            view["dedup"] = False
            return view

    def record_usage(self, event_id: str, lease_id: str, amount) -> dict:
        """按幂等事件扣减实际调用量。

        同一 event_id 重复回执只扣一次，返回首次结果并标记 dedup。
        """
        with self._lock:
            event_id = (event_id or "").strip()
            if not event_id:
                raise ValidationError("event_id 必填（幂等键）")
            amount = _as_int(amount, "amount")
            if amount <= 0:
                raise ValidationError("amount 必须为正整数")
            if event_id in self.state.usage_dedup:
                result = dict(self.state.usage_dedup[event_id])
                lease = self.state.leases.get(result["lease_id"])
                result["remaining"] = lease.remaining if lease else None
                result["dedup"] = True
                return result
            lease = self._lease(lease_id)
            self._sweep_locked()
            if lease.status != "active":
                raise ConflictError(f"租约已关闭（{lease.close_reason}），拒绝扣减")
            if amount > lease.remaining:
                raise ConflictError(
                    f"租约剩余不足：remaining={lease.remaining}，请求扣减 {amount}")
            self._emit(EV_USAGE, {
                "event_id": event_id, "lease_id": lease.id, "amount": amount,
                "ts": self._now()},
                usage_dedup=(event_id, lease.id, amount))
            return {"event_id": event_id, "lease_id": lease.id, "amount": amount,
                    "remaining": lease.remaining, "dedup": False}

    def reduce_lease(self, lease_id: str, new_amount) -> dict:
        """父级缩容：下调租约面额，差额立即退回资金源。

        不得缩到 已消费 + 已转授给下级的活跃租约面额 之下（不得超卖）。
        """
        with self._lock:
            lease = self._lease(lease_id)
            new_amount = _as_int(new_amount, "new_amount")
            self._sweep_locked()
            if lease.status != "active":
                raise ConflictError("租约已关闭，无法缩容")
            if new_amount < 0 or new_amount >= lease.amount:
                raise ValidationError("new_amount 必须 ≥0 且小于当前面额")
            granted_out = sum(l.amount for l in self._funded_active(lease.id))
            committed = lease.consumed + granted_out
            if new_amount < committed:
                raise ConflictError(
                    f"不能缩到 {new_amount}：已承诺 {committed}"
                    f"（已消费 {lease.consumed} + 已转授下级 {granted_out}）")
            released = lease.amount - new_amount
            self._emit(EV_REDUCED, {"lease_id": lease.id,
                                    "new_amount": new_amount,
                                    "released": released, "ts": self._now()})
            return self.lease_view(lease.id)

    def close_lease(self, lease_id: str, reason: str) -> dict:
        """归还（holder 主动）或撤销（父级强制）。级联先关闭全部下级租约。

        只释放未消费部分；已确认消费永久保留。重复关闭幂等。
        """
        if reason not in ("returned", "revoked"):
            raise ValidationError("reason 必须是 returned 或 revoked")
        with self._lock:
            lease = self._lease(lease_id)
            self._sweep_locked()
            if lease.status == "closed":
                return self.lease_view(lease.id)  # 幂等
            self._close_tree(lease, reason, set())
            return self.lease_view(lease.id)

    def sweep(self) -> dict:
        """清算所有到期租约（后台线程周期调用，也可手动触发）。"""
        with self._lock:
            before = sum(1 for l in self.state.leases.values()
                         if l.status == "closed")
            self._sweep_locked()
            after = sum(1 for l in self.state.leases.values()
                        if l.status == "closed")
            return {"swept": after - before}

    # ------------------------------------------------------------ 读操作

    def root_view(self) -> dict:
        s = self.state
        return {"capacity": s.root_capacity, "unissued": s.root_unissued,
                "issued": s.root_capacity - s.root_unissued,
                "burned": s.total_burned}

    def node_view(self, node_id: str) -> dict:
        node = self._node(node_id)
        held = self._held_active(node_id)
        granted = self._granted_active(node_id)
        subtree = self._subtree_ids(node_id)
        burned_subtree = sum(l.consumed for l in self.state.leases.values()
                             if l.holder_id in subtree)
        return {
            "id": node.id, "parent_id": node.parent_id, "name": node.name,
            "kind": node.kind, "depth": self._depth(node_id),
            "available": (self.state.root_unissued if node.kind == "root"
                          else sum(l.remaining for l in held)),
            "granted_out": sum(l.amount for l in granted),
            "consumed_own": sum(l.consumed for l in held),
            "burned_subtree": burned_subtree,
            "held_lease_ids": [l.id for l in held],
        }

    def lease_view(self, lease_id: str) -> dict:
        lease = self._lease(lease_id)
        holder = self.state.nodes.get(lease.holder_id)
        granter = self.state.nodes.get(lease.granter_id)
        return {
            "id": lease.id, "granter_id": lease.granter_id,
            "granter_name": granter.name if granter else "?",
            "holder_id": lease.holder_id,
            "holder_name": holder.name if holder else "?",
            "funding_lease_id": lease.funding_lease_id,
            "amount": lease.amount, "remaining": lease.remaining,
            "consumed": lease.consumed,
            "granted_out": sum(l.amount for l in self._funded_active(lease.id)),
            "status": lease.status, "close_reason": lease.close_reason,
            "released": lease.released,
            "created_at": lease.created_at, "expires_at": lease.expires_at,
            "ttl_remaining": max(0.0, lease.expires_at - self._now())
            if lease.status == "active" else 0.0,
            "closed_at": lease.closed_at, "idem_key": lease.idem_key,
        }

    def snapshot(self) -> dict:
        with self._lock:
            nodes = sorted((self.node_view(n.id) for n in self.state.nodes.values()),
                           key=lambda v: (v["depth"], v["name"]))
            leases = sorted((self.lease_view(l.id)
                             for l in self.state.leases.values()),
                            key=lambda v: v["created_at"])
            return {"now": self._now(), "root": self.root_view(),
                    "nodes": nodes, "leases": leases}

    def events_tail(self, limit: int = 100) -> list[dict]:
        with self._lock:
            return self.ledger.tail(limit)

    def audit(self) -> dict:
        with self._lock:
            events = [(typ, payload)
                      for _s, _t, typ, payload in self.ledger.replay()]
            from .audit import run_audit
            return run_audit(self.state, events, self._now())

    # ------------------------------------------------------------ 内部

    def _emit(self, typ: str, payload: dict, **dedup) -> None:
        self.ledger.append(typ, payload, **dedup)
        self.state.apply(typ, payload)

    def _node(self, node_id: str) -> Node:
        node = self.state.nodes.get(node_id)
        if node is None:
            raise NotFoundError(f"节点不存在: {node_id}")
        return node

    def _lease(self, lease_id: str) -> Lease:
        lease = self.state.leases.get(lease_id)
        if lease is None:
            raise NotFoundError(f"租约不存在: {lease_id}")
        return lease

    def _held_active(self, node_id: str) -> list[Lease]:
        return [l for l in self.state.leases.values()
                if l.holder_id == node_id and l.status == "active"]

    def _granted_active(self, node_id: str) -> list[Lease]:
        return [l for l in self.state.leases.values()
                if l.granter_id == node_id and l.status == "active"]

    def _funded_active(self, lease_id: str) -> list[Lease]:
        return [l for l in self.state.leases.values()
                if l.funding_lease_id == lease_id and l.status == "active"]

    def _depth(self, node_id: str) -> int:
        depth, node = 0, self.state.nodes.get(node_id)
        while node is not None and node.parent_id is not None:
            node = self.state.nodes.get(node.parent_id)
            depth += 1
        return depth

    def _subtree_ids(self, node_id: str) -> set[str]:
        children: dict[str, list[str]] = {}
        for n in self.state.nodes.values():
            if n.parent_id:
                children.setdefault(n.parent_id, []).append(n.id)
        result, stack = set(), [node_id]
        while stack:
            current = stack.pop()
            if current in result:
                continue
            result.add(current)
            stack.extend(children.get(current, ()))
        return result

    def _close_tree(self, lease: Lease, reason: str, closing: set[str]) -> None:
        """自底向上关闭：先递归关闭该租约资金供养的全部下级租约。

        每张租约的关闭是独立的 "写账本 → 应用"：子租约退回的额度先入账，
        父租约的 released 才能基于最新余额计算（守恒依赖此顺序）。
        中途宕机只会留下"部分级联"，剩余活跃租约可由后续清算/关闭继续处理。
        """
        if lease.id in closing or lease.status != "active":
            return
        for child in sorted(self._funded_active(lease.id),
                            key=lambda l: l.created_at):
            self._close_tree(child, "cascade", closing)
        closing.add(lease.id)
        self._emit(EV_CLOSED, {
            "lease_id": lease.id, "reason": reason,
            "released": lease.remaining, "consumed": lease.consumed,
            "closed_at": self._now()})

    def _sweep_locked(self) -> None:
        now = self._now()
        expired = [l for l in self.state.leases.values()
                   if l.status == "active" and l.expires_at <= now]
        if not expired:
            return
        closing: set[str] = set()
        # 深的先关，保证子租约先于父租约结算
        for lease in sorted(expired, key=lambda l: -self._depth(l.holder_id)):
            self._close_tree(lease, "expired", closing)
