"use strict";
const ImagePolicyUI = (() => {
  const defaults = {enabled:true, local_resources:["nx5-image"], default_route:"local_first", allow_cloud:true,
    when_busy:"cloud", when_unavailable:"queue", queue_limit:10, queue_timeout:1800,
    cloud_provider:"codex", paid_fallback:false, daily_paid_images:20};
  const field = key => document.getElementById(`image-policy-${key}`);
  let refresh = null;
  function render(settings, api) {
    const value = {...defaults, ...settings.image_generation};
    for (const [key, item] of Object.entries(value)) {
      const input = field(key);
      if (!input) continue;
      if (input.type === "checkbox") input.checked = item;
      else input.value = Array.isArray(item) ? item.join(", ") : item;
    }
    refresh = async () => {
      const output = document.getElementById("image-policy-resources");
      output.textContent = "正在检查已配置节点…";
      try {
        const result = await api("/api/media/image-resources");
        const labels = {ready:"空闲", busy:"忙", unavailable:"不可用", disabled:"未启用", unqualified:"未验收"};
        output.textContent = result.data.length ? result.data.map(row =>
          `${row.id}：${labels[row.status] || row.status}；当前策略${row.selected ? "已选" : "未选"}；在途 ${row.active_jobs.length}`
        ).join("\n") : "尚未登记图片执行节点；此处不会自动安装模型或启动服务。";
      } catch (error) { output.textContent = `资源状态读取失败：${error.message}`; }
    };
    document.getElementById("image-policy-refresh").onclick = () => refresh();
    update();
  }
  function collect() {
    const result = {};
    for (const [key, fallback] of Object.entries(defaults)) {
      const input = field(key);
      result[key] = typeof fallback === "boolean" ? input.checked : typeof fallback === "number" ? Number(input.value)
        : Array.isArray(fallback) ? input.value.split(",").map(x => x.trim()).filter(Boolean) : input.value;
    }
    return result;
  }
  function update() {
    const allow = field("allow_cloud").checked;
    const text = !allow ? "云端已禁止：账号授权和显式选择均不能绕过。"
      : field("default_route").value === "local_only" ? "默认仅本地；忙时不会自动转云端。显式云端请求仍须账号获准。"
      : field("default_route").value === "cloud_only" ? "默认使用云端；仍须账号获准且通道可用。"
      : field("when_busy").value === "cloud" ? "本地优先 → 忙时尝试获准的云端 → 云端不符合条件则排队。"
      : "本地优先 → 按忙时与不可用规则处理。";
    document.getElementById("image-policy-summary").textContent = `${text} 已提交但结果未知的任务只查询原任务。`;
  }
  function validate(value) {
    if (value.default_route === "cloud_only" && !value.allow_cloud) return ["仅云端模式必须允许云端API"];
    if (value.paid_fallback && !value.allow_cloud) return ["付费备用必须允许云端API"];
    if (!Number.isInteger(value.queue_timeout) || value.queue_timeout < 1 || value.queue_timeout > 86400) return ["图片排队期限须为1–86400秒"];
    if (!Number.isInteger(value.queue_limit) || value.queue_limit < 1 || value.queue_limit > 100) return ["图片队列上限须为1–100"];
    return [];
  }
  document.querySelectorAll("[data-policy-kind]").forEach(button => button.addEventListener("click", () => {
    const image = button.dataset.policyKind === "image";
    document.getElementById("image-policy-panel").hidden = !image;
    document.getElementById("text-policy-panel").hidden = image;
    document.querySelector(".policy-workflow-bar .policy-node-nav").hidden = image;
    document.querySelectorAll("[data-policy-kind]").forEach(item => item.setAttribute("aria-pressed", String(item === button)));
  }));
  document.getElementById("image-policy-panel").addEventListener("change", update);
  return {render, collect, validate};
})();
