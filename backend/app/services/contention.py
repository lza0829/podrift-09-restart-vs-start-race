"""铜钮（重启策）翻转与启停单抢点的并发裁决服务。

只处理「铜钮翻转 ↔ 启动信封」这一类抢点；在途推进、拨杆等不在本题范围。

两条互斥路由，由舱规 contention_strategy 决定，只许二选一：

* defer   —— 先后袋：启动闸信封落库完成前，铜钮翻转先入先后袋挂起；
             信封未落库就不按新铜钮裁决，袋中等落库后再消化。
* preempt —— 抢先改裁决表：新铜钮可先写入裁决表，但启停态与舱体必须在同一
             事务里补齐可核；对读屏争用格不得装成「无在途却已按新铜钮收口」。

所有写操作都在单事务里提交，两路提交要么一起成，要么一起回到铜钮前快照。
铜钮翻转痕、启动闸信封、舱体运行态、对读屏争用格通过 op_log 的单调序号
落到同一次序，读侧争用结局凭序号可重放。
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from app.services.common import now_iso

# 舱规键：铜钮与启动抢点时谁让路
STRATEGY_KEY = "contention_strategy"
STRATEGY_DEFER = "defer"        # 先后袋为准
STRATEGY_PREEMPT = "preempt"    # 铜钮抢先改写裁决表为准
DEFAULT_STRATEGY = STRATEGY_DEFER

# 舱规键：先后袋超时秒数（挂起翻转超过该值须可回滚）
BAG_TIMEOUT_KEY = "policy_bag_timeout_sec"
DEFAULT_BAG_TIMEOUT_SEC = 30

# 信封在途状态
ENVELOPE_INFLIGHT = "启动中"
ENVELOPE_DONE = "已完成"
ENVELOPE_FAILED = "失败"

# 翻转袋状态
FLIP_PENDING = "挂起"
FLIP_DIGESTED = "已消化"
FLIP_TIMEOUT = "已超时回滚"

# 争用格状态
DISPUTE_OPEN = "在途"
DISPUTE_CLOSED = "已收口"

VALID_STRATEGIES = {STRATEGY_DEFER, STRATEGY_PREEMPT}


class ContentionError(RuntimeError):
    """抢点裁决无法成立，调用方必须整体回滚。"""


@dataclass
class Snapshot:
    """铜钮前快照：任一路提交失败都回到这里。"""

    mode: str
    backoff_sec: int
    policy_version: int


def ensure_contention_schema(conn: sqlite3.Connection) -> None:
    """建抢点裁决所需的表（幂等）。"""
    conn.executescript(
        """
        -- 全局次序表：铜钮翻转痕 / 启动信封 / 争用格都在此取号
        CREATE TABLE IF NOT EXISTS op_log (
          seq INTEGER PRIMARY KEY AUTOINCREMENT,
          kind TEXT NOT NULL,
          ref TEXT,
          detail TEXT NOT NULL,
          at TEXT NOT NULL
        );

        -- 铜钮翻转痕：每一次翻转都留痕，带版本号，可重放
        CREATE TABLE IF NOT EXISTS policy_flips (
          id INTEGER PRIMARY KEY,
          seq INTEGER NOT NULL UNIQUE,
          from_mode TEXT NOT NULL,
          to_mode TEXT NOT NULL,
          backoff_sec INTEGER NOT NULL,
          version_before INTEGER NOT NULL,
          version_after INTEGER NOT NULL,
          state TEXT NOT NULL,
          at TEXT NOT NULL,
          FOREIGN KEY(seq) REFERENCES op_log(seq)
        );

        -- 先后袋：defer 路由下，信封在途期间到达的翻转先入袋
        CREATE TABLE IF NOT EXISTS policy_flip_queue (
          id INTEGER PRIMARY KEY,
          flip_id INTEGER NOT NULL UNIQUE,
          state TEXT NOT NULL,
          enqueued_at TEXT NOT NULL,
          digested_at TEXT,
          timeout_at TEXT NOT NULL,
          FOREIGN KEY(flip_id) REFERENCES policy_flips(id)
        );

        -- 裁决表：每次铜钮 ↔ 启动抢点的可重放结局
        CREATE TABLE IF NOT EXISTS adjudications (
          id INTEGER PRIMARY KEY,
          seq INTEGER NOT NULL UNIQUE,
          pod_id INTEGER NOT NULL,
          order_id INTEGER,
          flip_id INTEGER,
          strategy TEXT NOT NULL,
          policy_version_at_tick INTEGER NOT NULL,
          mode_at_tick TEXT NOT NULL,
          inflight_envelope INTEGER NOT NULL,
          outcome TEXT NOT NULL,
          state TEXT NOT NULL,
          detail TEXT NOT NULL,
          created_at TEXT NOT NULL,
          closed_at TEXT,
          CHECK (inflight_envelope IN (0, 1))
        );
        """
    )
    # restart_policy 增加版本列（老库平滑升级）
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(restart_policy)").fetchall()}
    if "version" not in cols:
        conn.execute("ALTER TABLE restart_policy ADD COLUMN version INTEGER NOT NULL DEFAULT 1")
    # start_orders 增加信封列
    ocols = {r["name"] for r in conn.execute("PRAGMA table_info(start_orders)").fetchall()}
    if "envelope_state" not in ocols:
        conn.execute("ALTER TABLE start_orders ADD COLUMN envelope_state TEXT")
    if "policy_version" not in ocols:
        conn.execute("ALTER TABLE start_orders ADD COLUMN policy_version INTEGER")
    if "seq" not in ocols:
        conn.execute("ALTER TABLE start_orders ADD COLUMN seq INTEGER")


# ---------- 基础工具 ----------

def _next_seq(conn: sqlite3.Connection, kind: str, ref: str | None, detail: str) -> int:
    cur = conn.execute(
        "INSERT INTO op_log(kind, ref, detail, at) VALUES (?,?,?,?)",
        (kind, ref, detail, now_iso()),
    )
    return int(cur.lastrowid)


def _get_setting(conn: sqlite3.Connection, key: str, default: str) -> str:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def get_strategy(conn: sqlite3.Connection) -> str:
    s = _get_setting(conn, STRATEGY_KEY, DEFAULT_STRATEGY)
    if s not in VALID_STRATEGIES:
        raise ContentionError(f"未知抢点路由舱规:{s}")
    return s


def get_bag_timeout_sec(conn: sqlite3.Connection) -> int:
    try:
        return max(1, int(_get_setting(conn, BAG_TIMEOUT_KEY, str(DEFAULT_BAG_TIMEOUT_SEC))))
    except ValueError:
        return DEFAULT_BAG_TIMEOUT_SEC


def _policy_snapshot(conn: sqlite3.Connection) -> Snapshot:
    row = conn.execute(
        "SELECT mode, backoff_sec, version FROM restart_policy WHERE id=1"
    ).fetchone()
    return Snapshot(row["mode"], int(row["backoff_sec"]), int(row["version"]))


def policy_snapshot(conn: sqlite3.Connection) -> Snapshot:
    """公开别名：供路由层取铜钮前快照。"""
    return _policy_snapshot(conn)


def _inflight_envelope_count(conn: sqlite3.Connection, pod_id: int | None = None) -> int:
    """启动闸信封在途数（启动中且未收口）。"""
    if pod_id is None:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM start_orders "
            "WHERE action='启动' AND envelope_state=?",
            (ENVELOPE_INFLIGHT,),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM start_orders "
            "WHERE pod_id=? AND action='启动' AND envelope_state=?",
            (pod_id, ENVELOPE_INFLIGHT),
        ).fetchone()
    return int(row["c"])


def inflight_envelope_count(conn: sqlite3.Connection, pod_id: int | None = None) -> int:
    """公开别名：在途启动闸信封数。"""
    return _inflight_envelope_count(conn, pod_id)


# ---------- 先后袋超时清扫（两条路由都须可回滚） ----------

def sweep_bag_timeouts(conn: sqlite3.Connection) -> list[int]:
    """把先后袋中已超时的挂起翻转回滚，返回被回滚的 flip_id 列表。

    回滚 = 翻转痕与袋记录都置为「已超时回滚」；若该翻转已经把铜钮改写，
    则恢复到 version_before 的模式。整个清扫在调用方事务内完成。
    """
    expired = conn.execute(
        """
        SELECT q.flip_id, f.version_before, f.from_mode, f.backoff_sec
        FROM policy_flip_queue q
        JOIN policy_flips f ON f.id = q.flip_id
        WHERE q.state = ? AND q.timeout_at <= ?
        """,
        (FLIP_PENDING, now_iso()),
    ).fetchall()
    rolled: list[int] = []
    for r in expired:
        flip_id = int(r["flip_id"])
        # 只有当铜钮当前版本正是这次翻转造成的，才恢复铜钮前快照
        pol = _policy_snapshot(conn)
        if pol.policy_version == int(r["version_before"]) + 1:
            conn.execute(
                "UPDATE restart_policy SET mode=?, backoff_sec=?, version=? WHERE id=1",
                (r["from_mode"], int(r["backoff_sec"]), int(r["version_before"])),
            )
        conn.execute(
            "UPDATE policy_flips SET state=? WHERE id=?", (FLIP_TIMEOUT, flip_id)
        )
        conn.execute(
            "UPDATE policy_flip_queue SET state=?, digested_at=? WHERE flip_id=?",
            (FLIP_TIMEOUT, now_iso(), flip_id),
        )
        _close_open_flip_disputes(conn, flip_id, "先后袋超时回滚，争用格一并回滚收口")
        _next_seq(conn, "铜钮翻转超时回滚", str(flip_id),
                  f"flip={flip_id} 恢复至 version={r['version_before']}")
        rolled.append(flip_id)
    return rolled


# ---------- 铜钮翻转 ----------

def flip_policy(conn: sqlite3.Connection, to_mode: str, backoff_sec: int | None) -> dict:
    """翻转铜钮。两条路由的入口统一在此，结局可重放。

    严禁只抛争用提示：defer 下入袋挂起，preempt 下先写裁决表再补齐可核状态。
    """
    snap = _policy_snapshot(conn)
    new_backoff = snap.backoff_sec if backoff_sec is None else int(backoff_sec)
    strategy = get_strategy(conn)

    seq = _next_seq(conn, "铜钮翻转请求", None,
                    f"{snap.mode}(v{snap.policy_version}) -> {to_mode} 路由={strategy}")
    flip_cur = conn.execute(
        """
        INSERT INTO policy_flips(seq, from_mode, to_mode, backoff_sec,
                                 version_before, version_after, state, at)
        VALUES (?,?,?,?,?,?,?,?)
        """,
        (seq, snap.mode, to_mode, new_backoff, snap.policy_version,
         snap.policy_version + 1, FLIP_PENDING, now_iso()),
    )
    flip_id = int(flip_cur.lastrowid)

    inflight = _inflight_envelope_count(conn)

    if strategy == STRATEGY_DEFER:
        # 路由一：信封落库完成前，翻转先丢进先后袋；信封未落库不按新铜钮裁决
        if inflight > 0:
            timeout_at = _iso_after(conn, get_bag_timeout_sec(conn))
            conn.execute(
                "INSERT INTO policy_flip_queue(flip_id, state, enqueued_at, timeout_at) "
                "VALUES (?,?,?,?)",
                (flip_id, FLIP_PENDING, now_iso(), timeout_at),
            )
            _open_flip_disputes(conn, flip_id=flip_id, strategy=strategy,
                                snap=snap, outcome="翻转入先后袋，等信封落库后消化")
            return {"flip_id": flip_id, "state": FLIP_PENDING,
                    "detail": "启动闸信封在途，铜钮翻转已入先后袋挂起"}
        # 无在途信封：直接翻转并消化
        _apply_flip(conn, flip_id, to_mode, new_backoff, snap)
        _digest_queue(conn, strategy)
        return {"flip_id": flip_id, "state": FLIP_DIGESTED,
                "detail": "无在途信封，铜钮翻转立即生效"}

    # 路由二：铜钮可抢先改写裁决表，但启停态与舱体必须补齐可核
    # 先写裁决表（可在信封在途时存在），再翻转铜钮
    _apply_flip(conn, flip_id, to_mode, new_backoff, snap)
    if inflight > 0:
        _open_flip_disputes(conn, flip_id=flip_id, strategy=strategy,
                            snap=snap,
                            outcome="铜钮抢先写入裁决表，待在途信封收口补齐可核")
    return {"flip_id": flip_id, "state": FLIP_DIGESTED,
            "detail": "铜钮已抢先翻转并登记裁决表"
                      + ("（存在在途信封，收口前争用格保持在途）" if inflight else "")}


def _apply_flip(conn, flip_id: int, to_mode: str, backoff: int, snap: Snapshot) -> None:
    conn.execute(
        "UPDATE restart_policy SET mode=?, backoff_sec=?, version=? WHERE id=1",
        (to_mode, backoff, snap.policy_version + 1),
    )
    conn.execute(
        "UPDATE policy_flips SET state=? WHERE id=?", (FLIP_DIGESTED, flip_id)
    )
    _next_seq(conn, "铜钮翻转生效", str(flip_id),
              f"flip={flip_id} mode={to_mode} version={snap.policy_version + 1}")


def _iso_after(conn: sqlite3.Connection, secs: int) -> str:
    row = conn.execute(
        "SELECT strftime('%Y-%m-%dT%H:%M:%SZ', 'now', ?) AS t",
        (f"+{secs} seconds",),
    ).fetchone()
    return row["t"]


def _digest_queue(conn: sqlite3.Connection, strategy: str) -> list[int]:
    """信封收口后消化先后袋：按入袋次序逐个应用挂起翻转。"""
    pending = conn.execute(
        """
        SELECT q.flip_id, f.to_mode, f.backoff_sec, f.version_before
        FROM policy_flip_queue q
        JOIN policy_flips f ON f.id = q.flip_id
        WHERE q.state = ?
        ORDER BY q.id
        """,
        (FLIP_PENDING,),
    ).fetchall()
    digested: list[int] = []
    for r in pending:
        flip_id = int(r["flip_id"])
        snap = _policy_snapshot(conn)
        # 袋中翻转的 version_before 可能已过期，以当前版本为准推进
        conn.execute(
            "UPDATE restart_policy SET mode=?, backoff_sec=?, version=? WHERE id=1",
            (r["to_mode"], int(r["backoff_sec"]), snap.policy_version + 1),
        )
        conn.execute(
            "UPDATE policy_flips SET state=?, version_before=?, version_after=? WHERE id=?",
            (FLIP_DIGESTED, snap.policy_version, snap.policy_version + 1, flip_id),
        )
        conn.execute(
            "UPDATE policy_flip_queue SET state=?, digested_at=? WHERE flip_id=?",
            (FLIP_DIGESTED, now_iso(), flip_id),
        )
        _next_seq(conn, "先后袋消化", str(flip_id),
                  f"flip={flip_id} 信封落库后消化 mode={r['to_mode']}")
        _close_open_flip_disputes(conn, flip_id, "先后袋消化，争用格随信封收口")
        digested.append(flip_id)
    return digested


# ---------- 裁决表（争用格） ----------

def _open_dispute(conn, *, pod_id: int, order_id: int | None,
                  flip_id: int | None, strategy: str, snap: Snapshot,
                  inflight: int, outcome: str) -> int:
    seq = _next_seq(conn, "争用格开启", str(order_id) if order_id else None, outcome)
    cur = conn.execute(
        """
        INSERT INTO adjudications(seq, pod_id, order_id, flip_id, strategy,
                                  policy_version_at_tick, mode_at_tick,
                                  inflight_envelope, outcome, state, detail,
                                  created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (seq, pod_id, order_id, flip_id, strategy,
         snap.policy_version, snap.mode, 1 if inflight else 0, outcome,
         DISPUTE_OPEN, outcome, now_iso()),
    )
    return int(cur.lastrowid)


def _open_flip_disputes(conn, *, flip_id: int, strategy: str,
                        snap: Snapshot, outcome: str) -> list[int]:
    """翻转与在途信封撞车：给每一个在途信封开一个争用格并挂到该信封上。

    这样信封收口时争用格必然随同一事务收口，不会留下「无主」开放格，
    也不可能装成无在途却已按新铜钮收口。
    """
    inflight_orders = conn.execute(
        "SELECT id, pod_id FROM start_orders "
        "WHERE action='启动' AND envelope_state=? ORDER BY id",
        (ENVELOPE_INFLIGHT,),
    ).fetchall()
    ids = []
    for o in inflight_orders:
        ids.append(_open_dispute(conn, pod_id=int(o["pod_id"]),
                                 order_id=int(o["id"]), flip_id=flip_id,
                                 strategy=strategy, snap=snap, inflight=1,
                                 outcome=outcome))
    return ids


def _close_open_flip_disputes(conn, flip_id: int, detail: str) -> None:
    """翻转被消化或超时回滚时，关闭仍挂在它名下的开放争用格。"""
    conn.execute(
        "UPDATE adjudications SET state=?, closed_at=?, detail=? "
        "WHERE flip_id=? AND state=?",
        (DISPUTE_CLOSED, now_iso(), detail, flip_id, DISPUTE_OPEN),
    )


def _close_disputes_for_order(conn, order_id: int, detail: str) -> None:
    """信封收口时，把与该信封相关的在途争用格收口（必须仍有在途事实才可收口）。"""
    conn.execute(
        "UPDATE adjudications SET state=?, closed_at=?, detail=? "
        "WHERE order_id=? AND state=?",
        (DISPUTE_CLOSED, now_iso(), detail, order_id, DISPUTE_OPEN),
    )


# ---------- 启动闸信封 ----------

def open_start_envelope(conn: sqlite3.Connection, pod_id: int) -> dict:
    """启动按钮砸下：先开在途信封（启动中），返回信封上下文。

    信封未落库前，defer 路由下新铜钮只能入袋、不得参与本笔裁决。
    """
    strategy = get_strategy(conn)
    snap = _policy_snapshot(conn)
    seq = _next_seq(conn, "启动闸信封开启", str(pod_id),
                    f"pod={pod_id} 铜钮版本 v{snap.policy_version}({snap.mode}) 路由={strategy}")
    return {"seq": seq, "strategy": strategy, "snapshot": snap}


def close_start_envelope(conn: sqlite3.Connection, *, order_id: int, pod_id: int,
                         envelope_state: str, ctx: dict) -> None:
    """信封收口：登记裁决、消化先后袋、收口争用格，全部同事务。

    双灯并存（启动中与已完成两套灯）在事务提交后不可能被读到：
    信封状态与舱体运行态在同一事务里翻转。
    """
    snap: Snapshot = ctx["snapshot"]
    strategy: str = ctx["strategy"]
    inflight_before = _inflight_envelope_count(conn, pod_id)
    # 本笔信封仍在在途计数内（即将收口），preempt 下若有翻转抢先写表，
    # 争用格必须带着「曾有在途」事实收口，不许装成无在途却按新铜钮收口。
    current = _policy_snapshot(conn)
    preempted = current.policy_version != snap.policy_version
    outcome = (
        "按信封开启时铜钮版本裁决，抢先翻转不回溯本笔"
        if strategy == STRATEGY_PREEMPT and preempted
        else f"信封收口 envelope={envelope_state}"
    )
    seq = _next_seq(conn, "启动闸信封收口", str(order_id),
                    f"order={order_id} {outcome}")
    conn.execute(
        """
        INSERT INTO adjudications(seq, pod_id, order_id, flip_id, strategy,
                                  policy_version_at_tick, mode_at_tick,
                                  inflight_envelope, outcome, state, detail,
                                  created_at, closed_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (seq, pod_id, order_id, None, strategy, snap.policy_version, snap.mode,
         1 if (inflight_before > 0 or preempted) else 0, outcome,
         DISPUTE_CLOSED, outcome, now_iso(), now_iso()),
    )
    # preempt 路由下铜钮翻转时登记的开放争用格，此刻补齐收口
    _close_disputes_for_order(conn, order_id, f"信封 {order_id} 收口补齐可核")
    # defer 路由：信封落库后消化先后袋；只要还有任一在途信封，袋继续等，
    # 绝不在信封未落库时按新铜钮裁决。
    if strategy == STRATEGY_DEFER and _inflight_envelope_count(conn) == 0:
        _digest_queue(conn, strategy)


def no_inflight_open_dispute(conn: sqlite3.Connection) -> int:
    """完整性自检：不得存在「无在途信封却仍按在途处理」的开放争用格。"""
    row = conn.execute(
        """
        SELECT COUNT(*) AS c FROM adjudications a
        WHERE a.state = ? AND a.inflight_envelope = 1
          AND NOT EXISTS (
            SELECT 1 FROM start_orders o
            WHERE o.id = a.order_id AND o.envelope_state = ?
          )
          AND a.order_id IS NOT NULL
        """,
        (DISPUTE_OPEN, ENVELOPE_INFLIGHT),
    ).fetchone()
    return int(row["c"])
