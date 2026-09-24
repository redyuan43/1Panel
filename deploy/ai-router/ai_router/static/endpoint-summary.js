/* Presentation only: health is not an admission or cache-hit guarantee. */
window.EndpointSummary = (() => {
  function device(endpoint) {
    if (endpoint.id === "ai-qwen38-27b") return "V100 TP4";
    if (endpoint.node === "ivan-v10016" || endpoint.id === "ivan-v10016-bonsai2-128k") return "V100 16G";
    if (endpoint.node === "amd") return "AMD 395";
    // Never relabel the legacy IVAN endpoint as the distinct V100 16G service.
    return String(endpoint.node || "未知设备").toUpperCase();
  }
  function concurrency(endpoint, status) {
    const running = status.detail?.running;
    const capacity = endpoint.max_concurrency;
    const known = typeof running === "number" && Number.isFinite(running) && running >= 0;
    return `${known ? running : "未知"} / ${Number.isInteger(capacity) && capacity > 0 ? capacity : "未知"}`;
  }
  function automatic(endpoint, status) {
    if (!endpoint.auto_candidate) return "未加入";
    if (!endpoint.enabled) return "已加入 · 已停用";
    if (status.detail?.draining) return "已加入 · 排空中";
    if (!status.healthy) return "已加入 · 健康检查未通过";
    return "已加入 · 仍需请求检查";
  }
  function visible(rows, includeDisabled) {
    return rows.filter(({endpoint}) => includeDisabled || endpoint.enabled);
  }
  return {device, concurrency, automatic, visible};
})();
