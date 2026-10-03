"""事件账本：所有状态变更的唯一日志来源（append-only, SQLite）。

服务重启后通过顺序重放 events 表重建全部内存状态。
usage_dedup / lease_idem 两张去重表与事件同事务写入，
既作为幂等约束（唯一索引），也作为重启后的去重索引来源。
"""
from __future__ import annotations

import json
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS events(
  seq      INTEGER PRIMARY KEY AUTOINCREMENT,
  ts       REAL NOT NULL,
  type     TEXT NOT NULL,
  payload  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS usage_dedup(
  event_id TEXT PRIMARY KEY,
  lease_id TEXT NOT NULL,
  amount   INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS lease_idem(
  idem_key TEXT PRIMARY KEY,
  lease_id TEXT NOT NULL
);
"""


class Ledger:
    def __init__(self, path: str):
        self.path = path
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")  # 宕机不丢已确认事件
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def append(self, typ: str, payload: dict,
               usage_dedup: tuple | None = None,
               lease_idem: tuple | None = None) -> None:
        """单事件写入（与去重表同事务）。"""
        with self._conn:
            self._conn.execute(
                "INSERT INTO events(ts,type,payload) VALUES(?,?,?)",
                (time.time(), typ, json.dumps(payload, ensure_ascii=False)))
            if usage_dedup is not None:
                self._conn.execute(
                    "INSERT INTO usage_dedup(event_id,lease_id,amount) VALUES(?,?,?)",
                    usage_dedup)
            if lease_idem is not None:
                self._conn.execute(
                    "INSERT INTO lease_idem(idem_key,lease_id) VALUES(?,?)",
                    lease_idem)

    def replay(self):
        """按 seq 顺序产出 (seq, ts, type, payload)。"""
        cur = self._conn.execute(
            "SELECT seq,ts,type,payload FROM events ORDER BY seq")
        for row in cur:
            yield row["seq"], row["ts"], row["type"], json.loads(row["payload"])

    def tail(self, limit: int = 100) -> list[dict]:
        cur = self._conn.execute(
            "SELECT seq,ts,type,payload FROM events ORDER BY seq DESC LIMIT ?",
            (limit,))
        return [{"seq": r["seq"], "ts": r["ts"], "type": r["type"],
                 "payload": json.loads(r["payload"])} for r in cur.fetchall()]

    def close(self) -> None:
        self._conn.close()
