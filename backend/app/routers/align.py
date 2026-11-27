from __future__ import annotations
from fastapi import APIRouter
from app.db import db
from app.services import contention as cz
from app.services.common import rows

router = APIRouter(tags=["对读屏"])


@router.get("/对读")
def align_view():
    with db() as conn:
        cz.sweep_bag_timeouts(conn)
        pods = rows(
            conn.execute(
                """
                SELECT p.id, p.name, p.run_state, pr.color AS probe_color, pr.fail_streak,
                       i.pull_state, o.status AS last_order_status, o.note AS last_order_note,
                       o.envelope_state AS last_envelope_state, o.policy_version AS order_policy_version
                FROM pods p
                LEFT JOIN probes pr ON pr.pod_id = p.id
                LEFT JOIN images i ON i.id = p.image_id
                LEFT JOIN start_orders o ON o.id = (
                  SELECT MAX(id) FROM start_orders WHERE pod_id = p.id
                )
                ORDER BY p.id
                """
            )
        )
        quota = dict(conn.execute("SELECT * FROM quotas WHERE id=1").fetchone())
        policy = dict(conn.execute("SELECT * FROM restart_policy WHERE id=1").fetchone())
        policy["contention_strategy"] = cz.get_strategy(conn)

        # 对读屏争用格：每个舱体的在途信封与开放争用一一对应
        disputes = rows(
            conn.execute(
                """
                SELECT a.id, a.seq, a.pod_id, a.order_id, a.strategy,
                       a.policy_version_at_tick, a.mode_at_tick, a.inflight_envelope,
                       a.outcome, a.state, o.envelope_state AS order_envelope_state
                FROM adjudications a
                LEFT JOIN start_orders o ON o.id = a.order_id
                ORDER BY a.seq
                """
            )
        )
        # 完整性自检：不得装成「无在途却已按新铜钮收口」
        phantom = cz.no_inflight_open_dispute(conn)
        inflight_total = cz.inflight_envelope_count(conn)
        open_disputes = sum(1 for d in disputes if d["state"] == cz.DISPUTE_OPEN)

        for p in pods:
            pid = p["id"]
            p["在途信封数"] = cz.inflight_envelope_count(conn, pid)
            p["争用格"] = [d for d in disputes if d["pod_id"] == pid]
            # 双灯并存自检：舱体运行中却仍有启动中信封，或反之
            p["双灯并存"] = (
                (p["run_state"] == "运行中" and p["在途信封数"] > 0)
                or (p["last_envelope_state"] == cz.ENVELOPE_INFLIGHT
                    and p["last_order_status"] == cz.ENVELOPE_DONE)
            )

        return {
            "舱体投影": pods,
            "配额": quota,
            "重启策": policy,
            "争用格总数": len(disputes),
            "开放争用格": open_disputes,
            "在途信封总数": inflight_total,
            "鬼影争用格(无在途却在途收口)": phantom,
            "自洽": phantom == 0 and not any(p["双灯并存"] for p in pods),
        }
