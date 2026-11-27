from __future__ import annotations
from fastapi import APIRouter
from pydantic import BaseModel
from app.db import db
from app.services import contention as cz
from app.services.common import now_iso, rows

router = APIRouter(tags=["启停单"])


class OrderIn(BaseModel):
    pod_id: int
    action: str  # 启动 | 停止
    target_image_id: int | None = None


class GateIn(BaseModel):
    pod_id: int
    target_image_id: int | None = None


def _norm_action(action: str) -> str:
    action = (action or "").strip()
    if action in ("启动", "start", "Start"):
        return "启动"
    if action in ("停止", "stop", "Stop"):
        return "停止"
    return action


@router.get("/启停单")
def list_orders():
    with db() as conn:
        return rows(conn.execute("SELECT * FROM start_orders ORDER BY id DESC"))


@router.post("/启停单/开闸")
def open_gate(body: GateIn):
    """两阶段第一阶段：启动闸信封先落库为「启动中」，拿到回执句柄。

    信封落库这一刻的铜钮版本被盖在信封上；此后到达的铜钮翻转，
    defer 路由只能进先后袋，preempt 路由只能登记裁决表，都不得回溯改写本笔。
    """
    with db() as conn:
        cz.sweep_bag_timeouts(conn)
        pod = conn.execute("SELECT * FROM pods WHERE id=?", (body.pod_id,)).fetchone()
        if not pod:
            return {"错误": "舱体不存在"}
        img_id = body.target_image_id or pod["image_id"]
        ctx = cz.open_start_envelope(conn, body.pod_id)
        cur = conn.execute(
            """
            INSERT INTO start_orders(pod_id, action, target_image_id, status, note,
                                     created_at, envelope_state, policy_version, seq)
            VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (body.pod_id, "启动", img_id, cz.ENVELOPE_INFLIGHT, "信封已开闸，待收口",
             now_iso(), cz.ENVELOPE_INFLIGHT, ctx["snapshot"].policy_version,
             ctx["seq"]),
        )
        order_id = int(cur.lastrowid)
        conn.execute(
            "INSERT INTO events(kind, ref, detail, at) VALUES (?,?,?,?)",
            ("启停单", str(order_id), f"启动信封开闸（启动中）铜钮版本 v{ctx['snapshot'].policy_version}", now_iso()),
        )
        return {"id": order_id, "envelope_state": cz.ENVELOPE_INFLIGHT,
                "policy_version": ctx["snapshot"].policy_version, "seq": ctx["seq"]}


def _adjudicate_start(conn, pod, img_id) -> tuple[str, str]:
    """按信封盖印的铜钮版本下的镜像/配额事实裁决放行或硬拒。"""
    img = conn.execute("SELECT * FROM images WHERE id=?", (img_id,)).fetchone()
    if not img or img["pull_state"] != "已就绪":
        return cz.ENVELOPE_FAILED, "镜像未就绪"
    q = conn.execute("SELECT * FROM quotas WHERE id=1").fetchone()
    need_cpu = float(pod["cpu_quota"])
    need_mem = int(pod["mem_quota_mb"])
    if float(q["cpu_used"]) + need_cpu > float(q["cpu_limit"]) or \
       int(q["mem_used_mb"]) + need_mem > int(q["mem_limit_mb"]):
        return cz.ENVELOPE_FAILED, "配额不足"
    return cz.ENVELOPE_DONE, "启动成功"


@router.post("/启停单/{order_id}/收口")
def close_gate(order_id: int):
    """两阶段第二阶段：裁决 + 舱体运行态 + 信封收口在同一事务完成。"""
    with db() as conn:
        cz.sweep_bag_timeouts(conn)
        order = conn.execute("SELECT * FROM start_orders WHERE id=?", (order_id,)).fetchone()
        if not order:
            return {"错误": "启停单不存在"}
        if order["envelope_state"] != cz.ENVELOPE_INFLIGHT:
            return {"错误": f"信封已收口为 {order['envelope_state']}，不可重复收口"}
        pod = conn.execute("SELECT * FROM pods WHERE id=?", (order["pod_id"],)).fetchone()
        envelope_state, note = _adjudicate_start(conn, pod, order["target_image_id"])
        if envelope_state == cz.ENVELOPE_DONE:
            # 放行：舱体态与信封同事务翻转，杜绝「启动中/已完成」双灯并存
            conn.execute(
                "UPDATE pods SET run_state=?, image_id=? WHERE id=?",
                ("运行中", order["target_image_id"], pod["id"]),
            )
            conn.execute(
                "UPDATE quotas SET cpu_used=cpu_used+?, mem_used_mb=mem_used_mb+? WHERE id=1",
                (float(pod["cpu_quota"]), int(pod["mem_quota_mb"])),
            )
        ctx = {"strategy": cz.get_strategy(conn),
               "snapshot": cz.Snapshot(mode="", backoff_sec=0,
                                       policy_version=int(order["policy_version"]))}
        conn.execute(
            "UPDATE start_orders SET status=?, note=?, envelope_state=? WHERE id=?",
            (envelope_state, note, envelope_state, order_id),
        )
        cz.close_start_envelope(conn, order_id=order_id, pod_id=pod["id"],
                                envelope_state=envelope_state, ctx=ctx)
        conn.execute(
            "INSERT INTO events(kind, ref, detail, at) VALUES (?,?,?,?)",
            ("启停单", str(order_id), f"信封收口 → {envelope_state} {note}", now_iso()),
        )
        return {"id": order_id, "status": envelope_state, "note": note}


@router.post("/启停单/收口在途")
def reconcile_inflight():
    """回执丢失的兜底：把长期卡在「启动中」的信封整笔回滚。

    回滚后信封置「失败(回滚)」、舱体保持停止，争用格同步收口；
    不会留下舱体已运行却还亮启动中灯的双灯局面。
    """
    recovered = []
    with db() as conn:
        cz.sweep_bag_timeouts(conn)
        rows_ = conn.execute(
            "SELECT * FROM start_orders WHERE envelope_state=? ORDER BY id",
            (cz.ENVELOPE_INFLIGHT,),
        ).fetchall()
        for o in rows_:
            conn.execute(
                "UPDATE start_orders SET status=?, note=?, envelope_state=? WHERE id=?",
                (cz.ENVELOPE_FAILED, "回执丢失，在途信封回滚", cz.ENVELOPE_FAILED, o["id"]),
            )
            ctx = {"strategy": cz.get_strategy(conn),
                   "snapshot": cz.Snapshot(mode="", backoff_sec=0,
                                           policy_version=int(o["policy_version"]))}
            cz.close_start_envelope(conn, order_id=o["id"], pod_id=o["pod_id"],
                                    envelope_state=cz.ENVELOPE_FAILED, ctx=ctx)
            recovered.append(int(o["id"]))
        return {"回滚信封": recovered}


@router.post("/启停单")
def create_order(body: OrderIn):
    """一站式接口（前端沿用）：开闸与收口在同一事务内完成，原子等价于两阶段。"""
    with db() as conn:
        cz.sweep_bag_timeouts(conn)
        pod = conn.execute("SELECT * FROM pods WHERE id=?", (body.pod_id,)).fetchone()
        if not pod:
            return {"错误": "舱体不存在"}
        action = _norm_action(body.action)
        img_id = body.target_image_id or pod["image_id"]

        if action == "停止":
            if pod["run_state"] == "运行中":
                conn.execute("UPDATE pods SET run_state=? WHERE id=?", ("已停止", body.pod_id))
                conn.execute(
                    "UPDATE quotas SET cpu_used=MAX(0, cpu_used-?), mem_used_mb=MAX(0, mem_used_mb-?) WHERE id=1",
                    (float(pod["cpu_quota"]), int(pod["mem_quota_mb"])),
                )
            cur = conn.execute(
                """
                INSERT INTO start_orders(pod_id, action, target_image_id, status, note,
                                         created_at, envelope_state, policy_version)
                VALUES (?,?,?,?,?,?,?,?)
                """,
                (body.pod_id, "停止", img_id, cz.ENVELOPE_DONE, "停止成功", now_iso(),
                 cz.ENVELOPE_DONE, cz.policy_snapshot(conn).policy_version),
            )
            conn.execute(
                "INSERT INTO events(kind, ref, detail, at) VALUES (?,?,?,?)",
                ("启停单", str(cur.lastrowid), "停止 → 已完成 停止成功", now_iso()),
            )
            return {"id": cur.lastrowid, "status": "已完成", "note": "停止成功"}

        if action != "启动":
            cur = conn.execute(
                "INSERT INTO start_orders(pod_id, action, target_image_id, status, note, created_at, envelope_state) "
                "VALUES (?,?,?,?,?,?,?)",
                (body.pod_id, action, img_id, "失败", f"未知动作:{action}", now_iso(),
                 cz.ENVELOPE_FAILED),
            )
            return {"id": cur.lastrowid, "status": "失败", "note": f"未知动作:{action}"}

        # 启动：信封先落库（开闸），再按盖印版本裁决收口，全程同一事务
        ctx = cz.open_start_envelope(conn, body.pod_id)
        cur = conn.execute(
            """
            INSERT INTO start_orders(pod_id, action, target_image_id, status, note,
                                     created_at, envelope_state, policy_version, seq)
            VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (body.pod_id, "启动", img_id, cz.ENVELOPE_INFLIGHT, "信封已开闸，待收口",
             now_iso(), cz.ENVELOPE_INFLIGHT, ctx["snapshot"].policy_version, ctx["seq"]),
        )
        order_id = int(cur.lastrowid)
        envelope_state, note = _adjudicate_start(conn, pod, img_id)
        if envelope_state == cz.ENVELOPE_DONE:
            conn.execute(
                "UPDATE pods SET run_state=?, image_id=? WHERE id=?",
                ("运行中", img_id, body.pod_id),
            )
            conn.execute(
                "UPDATE quotas SET cpu_used=cpu_used+?, mem_used_mb=mem_used_mb+? WHERE id=1",
                (float(pod["cpu_quota"]), int(pod["mem_quota_mb"])),
            )
        conn.execute(
            "UPDATE start_orders SET status=?, note=?, envelope_state=? WHERE id=?",
            (envelope_state, note, envelope_state, order_id),
        )
        cz.close_start_envelope(conn, order_id=order_id, pod_id=body.pod_id,
                                envelope_state=envelope_state, ctx=ctx)
        conn.execute(
            "INSERT INTO events(kind, ref, detail, at) VALUES (?,?,?,?)",
            ("启停单", str(order_id), f"启动 → {envelope_state} {note}", now_iso()),
        )
        return {"id": order_id, "status": envelope_state, "note": note}
