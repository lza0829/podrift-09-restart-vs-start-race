const j = async (url, opt) => {
  const r = await fetch(url, {
    headers: { "Content-Type": "application/json" },
    ...opt,
  });
  return r.json();
};

export const api = {
  health: () => j("/api/健康"),
  images: () => j("/api/镜像"),
  advancePull: (id) => j(`/api/镜像/${id}/推进拉取`, { method: "POST" }),
  pods: () => j("/api/舱体"),
  orders: () => j("/api/启停单"),
  createOrder: (body) => j("/api/启停单", { method: "POST", body: JSON.stringify(body) }),
  probes: () => j("/api/探针"),
  setProbe: (podId, body) => j(`/api/探针/${podId}`, { method: "POST", body: JSON.stringify(body) }),
  quota: () => j("/api/配额"),
  policy: () => j("/api/重启策"),
  setPolicy: (body) => j("/api/重启策", { method: "POST", body: JSON.stringify(body) }),
  setStrategy: (strategy) => j("/api/重启策/路由", { method: "POST", body: JSON.stringify({ strategy }) }),
  bag: () => j("/api/先后袋"),
  sweepBag: () => j("/api/先后袋/清扫超时", { method: "POST" }),
  adjudications: () => j("/api/裁决表"),
  opLog: () => j("/api/次序"),
  flips: () => j("/api/翻转痕"),
  openGate: (body) => j("/api/启停单/开闸", { method: "POST", body: JSON.stringify(body) }),
  closeGate: (id) => j(`/api/启停单/${id}/收口`, { method: "POST" }),
  reconcile: () => j("/api/启停单/收口在途", { method: "POST" }),
  align: () => j("/api/对读"),
  settings: () => j("/api/舱规"),
  events: () => j("/api/事件"),
};
