import React, { useEffect, useState, useCallback } from "react";
import { api } from "../api";

export default function Align() {
  const [data, setData] = useState(null);
  const load = useCallback(() => api.align().then(setData), []);
  useEffect(() => { load(); }, [load]);
  if (!data) return null;

  const reconcile = () => api.reconcile().then(load);

  return (
    <>
      <div className="hero">
        <h1>对读屏</h1>
        <p>铜钮翻转痕、启动闸信封、舱体运行态、争用格落到同一次序。争用结局可重放，不得装成无在途却已按新铜钮收口。</p>
      </div>

      <div className="stat-strip">
        <div className="stat">
          <div className="k">在途信封 / 开放争用格</div>
          <div className="v">{data["在途信封总数"]} / {data["开放争用格"]}</div>
        </div>
        <div className="stat">
          <div className="k">鬼影争用格（须为 0）</div>
          <div className="v">{data["鬼影争用格(无在途却在途收口)"]}</div>
        </div>
        <div className="stat">
          <div className="k">整屏自洽 / 路由</div>
          <div className="v">
            <span className={`tag ${data["自洽"] ? "ok" : "bad"}`}>{data["自洽"] ? "自洽" : "破功"}</span>
            {" "}{data["重启策"].contention_strategy}
          </div>
        </div>
      </div>

      <div className="panel">
        <div className="row-actions">
          <button className="btn" onClick={reconcile}>回执丢失兜底：回滚所有卡「启动中」信封</button>
        </div>
      </div>

      <div className="panel">
        <table>
          <thead>
            <tr>
              <th>舱</th><th>运行</th><th>探针</th><th>镜像</th>
              <th>最近信封</th><th>在途数</th><th>双灯</th><th>争用格</th>
            </tr>
          </thead>
          <tbody>
            {data["舱体投影"].map((r) => (
              <tr key={r.id}>
                <td>{r.name}</td>
                <td><span className={`tag ${r.run_state === "运行中" ? "ok" : "warn"}`}>{r.run_state}</span></td>
                <td>{r.probe_color} · {r.fail_streak}</td>
                <td>{r.pull_state}</td>
                <td>
                  {r.last_envelope_state
                    ? <span className={`tag ${r.last_envelope_state === "已完成" ? "ok" : r.last_envelope_state === "启动中" ? "warn" : "bad"}`}>
                        {r.last_envelope_state}
                      </span>
                    : "—"}
                  <small> v{r.order_policy_version ?? "—"}</small>
                </td>
                <td>{r["在途信封数"]}</td>
                <td>
                  {r["双灯并存"]
                    ? <span className="tag bad">双灯并存</span>
                    : <span className="tag ok">无</span>}
                </td>
                <td>
                  {r["争用格"].length === 0 ? "—" : r["争用格"].map((d) => (
                    <div key={d.id} className="dispute">
                      <span className={`tag ${d.state === "在途" ? "warn" : "ok"}`}>{d.state}</span>
                      <small> seq{d.seq} · {d.strategy} · 盖印v{d.policy_version_at_tick}</small>
                    </div>
                  ))}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}
