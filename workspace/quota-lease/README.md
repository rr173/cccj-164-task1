# 多租户 API 配额租约服务

平台将全局容量沿 **组织(org) → 项目(project) → 客户端(client)** 三级向下分配。
子级只能在父级可用额度内申请**带有效期的租约**；实际调用按**幂等事件**扣减；
归还/到期/撤销只释放**未消费部分**；服务重启后从**事件账本**重放重建全部余额。

零依赖（Python 3.8+ 标准库）：`http.server` + `sqlite3` + `unittest`。

## 快速开始

```bash
cd quota-lease
python3 server.py                 # 启动服务（默认 127.0.0.1:8000，账本 quota-ledger.db）
# 打开 http://127.0.0.1:8000/       租约控制台（总览 + 租约页 /leases + 审计）

python3 demo.py                   # 端到端演示（含并发、离线、撤销、重启恢复）
python3 -m unittest discover -s tests -t .   # 20 个需求覆盖测试
```

## 核心模型（纯租约制 / 代币转移）

- **租约即分配**：授予租约 = 把 `amount` 个代币从授予方余额转入租约，
  父级可用额度即时扣减，从源头杜绝超卖。
- **消费即销毁**：用量事件从租约扣减（`remaining -= x`），代币永久销毁，
  `total_burned` 只增不减 —— 已确认消费不会因租约撤销/到期而消失。
- **关闭即退回**：归还/到期/撤销只把 `remaining`（未消费部分）退回资金源。
- **资金链**：每张非平台租约记录 `funding_lease_id`（父节点持有的资金租约），
  关闭时代币沿资金链路逐级退回；**TTL 嵌套**（子租约到期 ≤ 资金租约到期）
  与**级联关闭**（先子后父）保证退回路径始终有效。

**守恒不变量**（审计核心）：

```
root_unissued + Σ(活跃租约 remaining) + total_burned == root_capacity
```

## 关键设计

| 需求 | 机制 |
|---|---|
| 父级额度内申请 | 授予前校验父级可用余额；资金租约剩余有效期须覆盖子租约 TTL |
| 幂等扣减 | 用量事件携带 `event_id`，内存索引 + SQLite 唯一约束双保险；重复回执返回首次结果 `dedup:true` |
| 重复申请防护 | 租约申请支持 `idempotency_key`，重试返回原租约 |
| 父级缩容 | `reduce`：面额不得缩到 `已消费 + 已转授下级` 之下；差额立即退回资金源。平台容量同样只能缩到"已发行未回收"之上 |
| 客户端离线 | 后台清算线程（0.5s）关闭到期租约，未消费部分自动归还；服务启动时先清算宕机期间的到期租约 |
| 跨层并发 | 单写者：所有变更在全局锁内完成"检查 → 写账本 → 应用"，无检查-应用窗口期 |
| 消费不消失 | 消费在事件发生时即销毁代币；撤销/到期只影响未消费余额 |
| 重启恢复 | 全部状态由事件账本推导；启动时顺序重放 `events` 表重建 |

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` `/leases` | 租约控制台页面（层级余额 / 租约表 / 操作 / 审计 / 账本） |
| GET | `/api/state` | 全局快照（节点余额 + 全部租约） |
| GET | `/api/audit` | 一致性审计报告 |
| GET | `/api/events?limit=N` | 事件账本（最近 N 条） |
| POST | `/api/root/capacity` | `{capacity}` 配置/调整全局容量 |
| POST | `/api/nodes` | `{parent_id, name, kind}` 创建组织/项目/客户端 |
| POST | `/api/leases` | `{holder_id, amount, ttl_seconds[, idempotency_key]}` 申请租约 |
| POST | `/api/leases/{id}/reduce` | `{new_amount}` 父级缩容 |
| POST | `/api/leases/{id}/revoke` | 撤销（级联关闭下级） |
| POST | `/api/leases/{id}/return` | 归还（只释放未消费部分） |
| POST | `/api/usage` | `{event_id, lease_id, amount}` 幂等扣减 |
| POST | `/api/sweep` | 手动清算到期租约 |

错误：统一 `{"error": {"code", "message"}}`；额度不足/超卖/已关闭 → `409`，参数非法 → `400`，不存在 → `404`。

## 一致性审计（7 项检查）

1. `ledger_replay_matches_memory` — 账本独立重放结果与在线状态一致
2. `global_conservation` — 未发行 + 活跃租约余额 + 已消费 == 全局容量
3. `non_negative_balances` — 无负余额（不超卖的直接证据）
4. `lease_budget_bounds` — 每张租约：已消费 + 余额 + 已转授 ≤ 面额
5. `usage_idempotency` — 无重复 `event_id`；逐租约用量合计 == 已消费
6. `ttl_nesting_and_close_order` — 子租约到期不晚于资金租约；关闭先子后父
7. `funding_integrity` — 资金租约持有者与授予方一致

测试中含**账本篡改用例**：伪造超额用量事件后，审计能定位 `non_negative_balances` 违例。

## 目录结构

```
quota-lease/
├── server.py            # HTTP 服务（API + 页面 + 后台清算线程）
├── demo.py              # 端到端演示脚本
├── app/
│   ├── ledger.py        # 事件账本（SQLite, WAL, 同步提交）
│   ├── service.py       # 领域核心：层级/租约/扣减/关闭/恢复（State + QuotaService）
│   ├── audit.py         # 一致性审计
│   └── web.py           # 控制台单页（原生 JS）
└── tests/test_quota.py  # 20 个需求覆盖测试
```

## 设计取舍

- **单写者**：单进程内全局锁 + SQLite 事务已保证不超卖；水平扩展时需把
  "检查-写入"下沉到数据库唯一约束/条件更新或按父节点分片，模型不变。
- **重放恢复**：演示规模下全量重放足够快；事件量增长后可在账本中定期写入
  快照事件，重放时从最近快照开始。
- **多资金租约**：节点可同时持有多张租约，转授时优先消耗最快到期的一张；
  每张子租约只记录单一资金源，保证退回路径无歧义。
