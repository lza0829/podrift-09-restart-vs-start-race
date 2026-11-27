import React, { useEffect, useState } from "react";
import { api } from "../api";

const STRATEGIES = [
  ["defer", "先后袋为准（信封落库前翻转挂起）"],
  ["preempt", "抢先改裁决表（新铜钮先写，收口补齐可核）"],
];

export default function Policy() {
  const [p, setP] = useState(null);
  const [bag, setBag] = useState([]);
  const [msg, setMsg] = useState("");
  const load = () => Promise.all([api.policy(), api.bag()]).then(([pol, b]) => {
    setP(pol); setBag(b);
  });
  useEffect(() => { load(); }, []);
  if (!p) return null;

  const flip = (mode) =>
    api.setPolicy({ mode }).then((r) => { setMsg(r.detail || ""); load(); });
  const choose = (strategy) =>
    api.setStrategy(strategy).then(() => { setMsg(`抢点路由已切换：${strategy}`); load(); });
  const sweep = () =>
    api.sweepBag().then((r) => { setMsg(`超时回滚翻转：${(r["回滚翻转"] || []).join(",") || "无"}`); load(); });

  return (
    <>
      <div className="hero">
        <h1>重启策 · 铜钮</h1>
        <p>铜钮翻转与启动闸信封对砸时谁让路，只许二选一。两路提交同事务原子，结局凭次序可重放，禁止只弹争用提示。</p>
      </div>

      <div className="panel">
        <p>当前铜钮：<strong>{p.mode}</strong> · 退避 {p.backoff_sec}s · 版本 v{p.version}</p>
        <p>抢点路由：<span className="tag warn">{p.contention_strategy}</span> · 先后袋超时 {p.bag_timeout_sec}s</p>
        <div className="row-actions">
          {["失败重启", "永不", "总是"].map((m) => (
            <button key={m} className="btn" onClick={() => flip(m)}>翻到 {m}</button>
          ))}
        </div>
        {msg && <p className="hint">{msg}</p>}
      </div>

      <div className="panel">
        <h3>谁让路（二选一）</h3>
        <div className="row-actions">
          {STRATEGIES.map(([key, label]) => (
            <button
              key={key}
              className={`btn ${p.contention_strategy === key ? "btn-on" : ""}`}
              onClick={() => choose(key)}
            >
              {label}
            </button>
          ))}
        </div>
      </div>

      <div className="panel">
        <h3>先后袋</h3>
        <div className="row-actions">
          <button className="btn" onClick={sweep}>清扫超时并回滚</button>
        </div>
        <table>
          <thead>
            <tr><th>翻转</th><th>从→到</th><th>状态</th><th>入袋</th><th>消化/超时</th></tr>
          </thead>
          <tbody>
            {bag.length === 0 && <tr><td colSpan="5">袋空</td></tr>}
            {bag.map((q) => (
              <tr key={q.id}>
                <td>{q.flip_id}</td>
                <td>{q.from_mode} → {q.to_mode}</td>
                <td><span className={`tag ${q.state === "挂起" ? "warn" : q.state === "已超时回滚" ? "bad" : "ok"}`}>{q.state}</span></td>
                <td>{q.enqueued_at}</td>
                <td>{q.digested_at || "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}
