from __future__ import annotations
from fastapi import APIRouter
from pydantic import BaseModel
from app.db import db
from app.services import contention as cz
from app.services.common import now_iso, row_dict, rows

router = APIRouter(tags=["重启策"])


class PolicyIn(BaseModel):
    mode: str
    backoff_sec: int | None = None


class StrategyIn(BaseModel):
    strategy: str  # defer | preempt


@router.get("/重启策")
def get_policy():
    with db() as conn:
        cz.sweep_bag_timeouts(conn)
        data = row_dict(conn.execute("SELECT * FROM restart_policy WHERE id=1").fetchone())
        data["contention_strategy"] = cz.get_strategy(conn)
        data["bag_timeout_sec"] = cz.get_bag_timeout_sec(conn)
        return data


@router.post("/重启策")
def set_policy(body: PolicyIn):
    """翻转铜钮。对砸结局由 contention_strategy 决定，绝不只弹争用提示。"""
    with db() as conn:
        result = cz.flip_policy(conn, body.mode, body.backoff_sec)
        conn.execute(
            "INSERT INTO events(kind, ref, detail, at) VALUES (?,?,?,?)",
            ("重启策", str(result["flip_id"]),
             f"铜钮翻转 → {body.mode}：{result['detail']}", now_iso()),
        )
        return {"ok": True, **result}


@router.post("/重启策/路由")
def set_strategy(body: StrategyIn):
    """二选一切换抢点路由：defer（先后袋）或 preempt（抢先改裁决表）。"""
    if body.strategy not in cz.VALID_STRATEGIES:
        return {"错误": f"路由只许 {sorted(cz.VALID_STRATEGIES)}"}
    with db() as conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (cz.STRATEGY_KEY, body.strategy),
        )
        conn.execute(
            "INSERT INTO events(kind, ref, detail, at) VALUES (?,?,?,?)",
            ("舱规", cz.STRATEGY_KEY, f"抢点路由切换为 {body.strategy}", now_iso()),
        )
        return {"ok": True, "strategy": body.strategy}


@router.get("/先后袋")
def get_bag():
    with db() as conn:
        cz.sweep_bag_timeouts(conn)
        return rows(
            conn.execute(
                """
                SELECT q.id, q.flip_id, q.state, q.enqueued_at, q.digested_at,
                       q.timeout_at, f.from_mode, f.to_mode
                FROM policy_flip_queue q
                JOIN policy_flips f ON f.id = q.flip_id
                ORDER BY q.id
                """
            )
        )


@router.get("/裁决表")
def get_adjudications():
    """读侧争用结局：凭 op_log 序号可重放。"""
    with db() as conn:
        return rows(
            conn.execute(
                """
                SELECT a.*, p.name AS pod_name
                FROM adjudications a
                LEFT JOIN pods p ON p.id = a.pod_id
                ORDER BY a.seq
                """
            )
        )


@router.get("/次序")
def get_op_log():
    """同一次序：铜钮翻转痕、启动闸信封、舱体态、争用格统一取号。"""
    with db() as conn:
        return rows(conn.execute("SELECT * FROM op_log ORDER BY seq"))


@router.get("/翻转痕")
def get_flips():
    with db() as conn:
        return rows(conn.execute("SELECT * FROM policy_flips ORDER BY id"))
