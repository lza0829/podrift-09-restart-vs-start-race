from __future__ import annotations
from fastapi import APIRouter
from pydantic import BaseModel
from app.db import db
from app.services import contention as cz
from app.services.common import now_iso, rows

router = APIRouter(tags=["舱规"])


class SettingIn(BaseModel):
    key: str
    value: str


@router.get("/舱规")
def list_settings():
    with db() as conn:
        data = rows(conn.execute("SELECT * FROM settings ORDER BY key"))
        return data


@router.post("/舱规")
def set_setting(body: SettingIn):
    with db() as conn:
        if body.key == cz.STRATEGY_KEY and body.value not in cz.VALID_STRATEGIES:
            return {"错误": f"抢点路由只许 {sorted(cz.VALID_STRATEGIES)}"}
        if body.key == cz.BAG_TIMEOUT_KEY:
            try:
                if int(body.value) < 1:
                    raise ValueError
            except ValueError:
                return {"错误": "先后袋超时须为正整数秒"}
        conn.execute(
            "INSERT INTO settings(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (body.key, body.value),
        )
        conn.execute(
            "INSERT INTO events(kind, ref, detail, at) VALUES (?,?,?,?)",
            ("舱规", body.key, f"{body.key}={body.value}", now_iso()),
        )
        return {"ok": True}


@router.post("/先后袋/清扫超时")
def sweep_timeouts():
    """显式触发先后袋超时回滚（读路径也会惰性清扫）。"""
    with db() as conn:
        rolled = cz.sweep_bag_timeouts(conn)
        return {"回滚翻转": rolled}


@router.get("/事件")
def list_events():
    with db() as conn:
        return rows(conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT 80"))
