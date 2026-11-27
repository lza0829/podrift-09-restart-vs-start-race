import React, { useEffect, useState } from "react";
import { api } from "../api";

export default function Orders() {
  const [pods, setPods] = useState([]);
  const [orders, setOrders] = useState([]);
  const [gates, setGates] = useState({});
  const load = () =>
    Promise.all([api.pods(), api.orders()]).then(([p, o]) => { setPods(p); setOrders(o); });
  useEffect(() => { load(); }, []);

  const act = (podId, action) => api.createOrder({ pod_id: podId, action }).then(load);

  // 两阶段：先开闸（信封落库为启动中），可在收口前去重启策页翻铜钮，再回来收口
  const openGate = (podId) =>
    api.openGate({ pod_id: podId }).then((g) => {
      setGates((s) => ({ ...s, [podId]: g.id }));
      load();
    });
  const closeGate = (podId) =>
    api.closeGate(gates[podId]).then(() => {
      setGates((s) => { const n = { ...s }; delete n[podId]; return n; });
      load();
    });

  return (
    <>
      <div className="hero">
        <h1>启停单</h1>
        <p>一站式启动/停止原子提交；也可用「开闸 → 翻铜钮 → 收口」复现抢点。镜像未就绪或配额不足整笔失败。</p>
      </div>
      <div className="panel">
        <div className="row-actions">
          {pods.map((p) => (
            <span key={p.id} className="pod-actions">
              <strong>{p.name}</strong>{" "}
              <button className="btn" onClick={() => act(p.id, "启动")}>启动</button>
              <button className="btn" onClick={() => act(p.id, "停止")}>停止</button>
              {gates[p.id]
                ? <button className="btn btn-on" onClick={() => closeGate(p.id)}>收口信封 #{gates[p.id]}</button>
                : <button className="btn" onClick={() => openGate(p.id)}>开闸（信封启动中）</button>}
            </span>
          ))}
        </div>
        <p className="hint">开闸后到「重启策」页翻铜钮，再回本页收口，即可对照先后袋与抢先裁决两种结局。</p>
      </div>
      <div className="panel">
        <table>
          <thead>
            <tr><th>ID</th><th>舱</th><th>动作</th><th>状态</th><th>信封</th><th>铜钮版本</th><th>说明</th><th>时间</th></tr>
          </thead>
          <tbody>
            {orders.map((o) => (
              <tr key={o.id}>
                <td>{o.id}</td>
                <td>{o.pod_id}</td>
                <td>{o.action}</td>
                <td><span className={`tag ${o.status === "已完成" ? "ok" : o.status === "失败" ? "bad" : "warn"}`}>{o.status}</span></td>
                <td>{o.envelope_state || "—"}{o.seq ? ` · seq${o.seq}` : ""}</td>
                <td>v{o.policy_version ?? "—"}</td>
                <td>{o.note}</td>
                <td>{o.created_at}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}
