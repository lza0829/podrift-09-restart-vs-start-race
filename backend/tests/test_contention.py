"""抢点裁决测试夹具。

覆盖题目点名的四类夹具：
  1. 铜钮与启动对砸（defer / preempt 两条路由各一）
  2. 先后袋超时回滚
  3. 双灯并存回滚（回执丢失兜底）
  4. 半截脏裁决整体回滚
另加：并发对砸、次序可重放、鬼影争用格自检、路由互斥校验。
"""
from __future__ import annotations

import threading
import time

import pytest

from app import db as db_mod


@pytest.fixture()
def client(tmp_path, monkeypatch):
    # 每个用例独立临时库；connect() 在调用时读取该模块全局，patch 即生效
    monkeypatch.setattr(db_mod, "DB_PATH", tmp_path / "test.db")
    from fastapi.testclient import TestClient

    from app.main import app
    from app.seed import seed_if_empty

    seed_if_empty()
    return TestClient(app)


def set_strategy(client, strategy: str):
    r = client.post("/api/重启策/路由", json={"strategy": strategy})
    assert r.status_code == 200 and r.json().get("ok")


def open_gate(client, pod_id: int = 1):
    r = client.post("/api/启停单/开闸", json={"pod_id": pod_id})
    assert r.status_code == 200
    return r.json()


def close_gate(client, order_id: int):
    return client.post(f"/api/启停单/{order_id}/收口").json()


def align(client):
    return client.get("/api/对读").json()


def policy(client):
    return client.get("/api/重启策").json()


# ---------- 夹具 1a：铜钮与启动对砸 —— 先后袋路由 ----------

def test_夹具1a_对砸_defer_信封未落库不按新铜钮(client):
    set_strategy(client, "defer")
    v0 = policy(client)["version"]
    mode0 = policy(client)["mode"]

    gate = open_gate(client)
    # 铜钮在信封在途时翻转：必须入袋挂起，绝不只弹争用提示
    flip = client.post("/api/重启策", json={"mode": "总是"}).json()
    assert flip["state"] == "挂起"
    assert policy(client)["mode"] == mode0          # 铜钮未生效
    assert policy(client)["version"] == v0

    bag = client.get("/api/先后袋").json()
    assert len(bag) == 1 and bag[0]["state"] == "挂起"

    # 信封收口期间裁决仍按盖印的旧版本，收口后才消化袋
    closed = close_gate(client, gate["id"])
    assert closed["status"] == "已完成"
    assert policy(client)["mode"] == "总是"
    assert policy(client)["version"] == v0 + 1
    assert client.get("/api/先后袋").json()[0]["state"] == "已消化"

    al = align(client)
    assert al["自洽"] is True
    assert al["开放争用格"] == 0 and al["鬼影争用格(无在途却在途收口)"] == 0


# ---------- 夹具 1b：铜钮与启动对砸 —— 抢先改裁决表路由 ----------

def test_夹具1b_对砸_preempt_铜钮可抢先但须补齐可核(client):
    set_strategy(client, "preempt")
    v0 = policy(client)["version"]

    gate = open_gate(client)
    flip = client.post("/api/重启策", json={"mode": "永不"}).json()
    assert flip["state"] == "已消化"
    # 铜钮确实抢先改写
    assert policy(client)["mode"] == "永不"
    assert policy(client)["version"] == v0 + 1

    # 在途未收口：争用格必须保持开放且带着在途事实，不许装成无在途收口
    mid = align(client)
    assert mid["在途信封总数"] == 1
    assert mid["开放争用格"] >= 1
    assert mid["自洽"] is True
    assert mid["鬼影争用格(无在途却在途收口)"] == 0

    closed = close_gate(client, gate["id"])
    assert closed["status"] == "已完成"
    after = align(client)
    # 收口后开放格清零，裁决表保留可重放记录，本笔按信封盖印版本裁决
    assert after["开放争用格"] == 0 and after["在途信封总数"] == 0
    assert after["自洽"] is True
    adjs = client.get("/api/裁决表").json()
    order_adjs = [a for a in adjs if a["order_id"] == gate["id"]]
    assert any(a["policy_version_at_tick"] == v0 for a in order_adjs)


# ---------- 夹具 2：先后袋超时回滚 ----------

def test_夹具2_先后袋超时_翻转与争用格一并回滚(client):
    set_strategy(client, "defer")
    client.post("/api/舱规", json={"key": "policy_bag_timeout_sec", "value": "1"})
    mode0 = policy(client)["mode"]
    v0 = policy(client)["version"]

    gate = open_gate(client)
    flip = client.post("/api/重启策", json={"mode": "总是"}).json()
    assert flip["state"] == "挂起"

    time.sleep(1.1)
    swept = client.post("/api/先后袋/清扫超时").json()["回滚翻转"]
    assert flip["flip_id"] in swept

    # 铜钮回到翻转前快照
    p = policy(client)
    assert p["mode"] == mode0 and p["version"] == v0
    assert client.get("/api/先后袋").json()[0]["state"] == "已超时回滚"
    assert client.get("/api/翻转痕").json()[0]["state"] == "已超时回滚"

    # 信封仍可正常收口，收口后袋已空不再消化
    close_gate(client, gate["id"])
    assert policy(client)["mode"] == mode0
    al = align(client)
    assert al["自洽"] is True and al["开放争用格"] == 0


# ---------- 夹具 3：双灯并存回滚（回执丢失，长期卡启动中） ----------

def test_夹具3_回执丢失_在途信封回滚_无双灯(client):
    gate = open_gate(client)
    pod_before = [p for p in align(client)["舱体投影"] if p["id"] == 1][0]
    assert pod_before["run_state"] == "已停止"      # 舱体没被提前点亮
    assert pod_before["在途信封数"] == 1

    recovered = client.post("/api/启停单/收口在途").json()["回滚信封"]
    assert gate["id"] in recovered

    al = align(client)
    pod = [p for p in al["舱体投影"] if p["id"] == 1][0]
    assert pod["双灯并存"] is False
    assert pod["在途信封数"] == 0 and pod["run_state"] == "已停止"
    assert al["自洽"] is True and al["开放争用格"] == 0

    order = [o for o in client.get("/api/启停单").json() if o["id"] == gate["id"]][0]
    assert order["envelope_state"] == "失败"


def test_夹具3b_正常收口不存在双灯(client):
    gate = open_gate(client)
    close_gate(client, gate["id"])
    al = align(client)
    pod = [p for p in al["舱体投影"] if p["id"] == 1][0]
    assert pod["run_state"] == "运行中"
    assert pod["在途信封数"] == 0 and pod["双灯并存"] is False


# ---------- 夹具 4：半截脏裁决整体回滚到铜钮前快照 ----------

def test_夹具4_半截脏裁决_事务整体回滚(client):
    from app.db import db
    from app.services import contention as cz

    with db() as conn:
        before = dict(conn.execute(
            "SELECT mode, version FROM restart_policy WHERE id=1").fetchone())

    with pytest.raises(RuntimeError):
        with db() as conn:
            cz.flip_policy(conn, "总是", None)
            # 舱体补齐前崩溃
            raise RuntimeError("半截脏裁决")

    with db() as conn:
        after = dict(conn.execute(
            "SELECT mode, version FROM restart_policy WHERE id=1").fetchone())
        n_flips = conn.execute("SELECT COUNT(*) c FROM policy_flips").fetchone()["c"]
        n_bag = conn.execute("SELECT COUNT(*) c FROM policy_flip_queue").fetchone()["c"]
        n_adj = conn.execute("SELECT COUNT(*) c FROM adjudications").fetchone()["c"]

    assert after == before
    assert (n_flips, n_bag, n_adj) == (0, 0, 0)


# ---------- 多信封：袋须等全部在途信封落库后才消化 ----------

def test_夹具1c_defer_多个在途信封_最后一个落库才消化(client):
    set_strategy(client, "defer")
    mode0 = policy(client)["mode"]

    g1 = open_gate(client, pod_id=1)
    g2 = open_gate(client, pod_id=1)
    flip = client.post("/api/重启策", json={"mode": "总是"}).json()
    assert flip["state"] == "挂起"

    # 第一个信封收口：仍有在途信封，袋必须继续等，铜钮不生效
    close_gate(client, g1["id"])
    assert policy(client)["mode"] == mode0
    assert client.get("/api/先后袋").json()[0]["state"] == "挂起"
    assert align(client)["在途信封总数"] == 1

    # 最后一个信封落库后才消化
    close_gate(client, g2["id"])
    assert policy(client)["mode"] == "总是"
    assert client.get("/api/先后袋").json()[0]["state"] == "已消化"
    al = align(client)
    assert al["自洽"] is True and al["开放争用格"] == 0 and al["在途信封总数"] == 0


# ---------- 并发对砸：库层串行化，结局可重放 ----------

def test_并发对砸_次序单调且自洽(client):
    set_strategy(client, "defer")
    errors: list[Exception] = []

    def worker(i: int):
        try:
            if i % 2 == 0:
                client.post("/api/重启策", json={"mode": "总是" if i % 4 == 0 else "永不"})
            else:
                g = open_gate(client, pod_id=1)
                if "id" in g:
                    close_gate(client, g["id"])
        except Exception as e:  # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(24)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    al = align(client)
    assert al["自洽"] is True
    assert al["鬼影争用格(无在途却在途收口)"] == 0
    assert al["开放争用格"] == 0 and al["在途信封总数"] == 0

    seqs = [row["seq"] for row in client.get("/api/次序").json()]
    assert seqs == sorted(seqs) and len(seqs) == len(set(seqs))
    # 裁决表每一行都能凭 seq 在次序里回放
    log_seqs = set(seqs)
    for adj in client.get("/api/裁决表").json():
        assert adj["seq"] in log_seqs


# ---------- 路由互斥与校验 ----------

def test_抢点路由只许二选一(client):
    r = client.post("/api/重启策/路由", json={"strategy": "bogus"})
    assert "错误" in r.json()
    r = client.post("/api/舱规", json={"key": "contention_strategy", "value": "bogus"})
    assert "错误" in r.json()
    r = client.post("/api/舱规", json={"key": "policy_bag_timeout_sec", "value": "0"})
    assert "错误" in r.json()


def test_一站式启停单仍原子(client):
    ok = client.post("/api/启停单", json={"pod_id": 1, "action": "启动"}).json()
    assert ok["status"] == "已完成"
    bad = client.post("/api/启停单", json={"pod_id": 2, "action": "启动"}).json()
    assert bad["status"] == "失败" and bad["note"] == "镜像未就绪"
    assert align(client)["自洽"] is True
