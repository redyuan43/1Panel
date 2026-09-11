"use strict";
const $ = (id) => document.getElementById(id);
$("lan-admin").value = sessionStorage.getItem("ai-router-admin-key") || "";
async function api(path, body) {
  const response = await fetch(path, {
    method: body ? "POST" : "GET",
    headers: {"Authorization": "Bearer " + $("lan-admin").value.trim(), ...(body ? {"Content-Type": "application/json"} : {})},
    ...(body ? {body: JSON.stringify(body)} : {}),
  });
  if (!response.ok) throw new Error(response.status === 401 ? "请填写有效的管理密钥。" : "检查接口暂不可用（" + response.status + "）。");
  return response.json();
}
async function load() {
  try {
    const data = await api("/api/lan-https/status");
    if (!data.configured) throw new Error(data.message);
    $("lan-url").value = data.base_url;
    $("ca-fingerprint").textContent = data.ca.sha256;
    $("server-valid").textContent = data.server.valid_now ? "当前有效 · 剩余 " + data.server.days_remaining + " 天" : "证书尚未生效或已过期";
    $("server-expiry").textContent = "到期：" + new Date(data.server.expires_at).toLocaleString();
    $("ca-expiry").textContent = "CA 到期：" + new Date(data.ca.expires_at).toLocaleString();
    $("renewal").textContent = (data.renewal.active ? "自动续签已启用，每日检查，剩余不足 30 天续签。" : "自动续签状态未确认。") + "最近检查：" + (data.renewal.last_check_at ? new Date(data.renewal.last_check_at).toLocaleString() : "未记录");
    $("device-check").href = data.base_url.slice(0, -3) + "/health";
    $("lan-details").hidden = false;
    $("lan-message").textContent = "配置已加载。新账号不需要重新生成证书。";
  } catch (error) {
    $("lan-details").hidden = true;
    $("lan-message").textContent = error.message;
  }
}
$("lan-login").addEventListener("submit", (event) => {event.preventDefault(); load();});
$("copy-url").addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText($("lan-url").value);
    $("lan-message").textContent = "连接地址已复制。";
  } catch {
    $("lan-url").focus(); $("lan-url").select();
    $("lan-message").textContent = "请按 Ctrl+C 或 Command+C 复制已选中的地址。";
  }
});
$("lan-os").addEventListener("change", () => {
  for (const os of ["windows", "linux", "macos"]) $("guide-" + os).hidden = os !== $("lan-os").value;
});
const labels = {network: "网络连接失败", network_tls: "网络与 HTTPS", certificate: "服务器证书验证失败", health: "Router 健康检查失败", authentication: "Key 认证或访问权限", model_access: "此 Key 无权访问填写的模型", inference: "模型推理请求", model_reply: "模型未返回预期检查内容", configuration: "服务器证书资料不完整"};
$("lan-check").addEventListener("submit", async (event) => {
  event.preventDefault();
  const button = event.submitter;
  const key = $("check-key").value.trim();
  if ($("check-inference").checked && !key) { $("check-result").textContent = "验证模型回复需要填写 API Key。"; return; }
  button.disabled = true;
  $("check-result").textContent = "正在从 AI 服务器检查…";
  try {
    const data = await api("/api/lan-https/check", {api_key: key || null, model: $("check-model").value.trim(), inference: $("check-inference").checked});
    $("check-result").textContent = data.ok
      ? "服务器侧通过：" + data.checks.map((item) => labels[item.stage] || item.stage).join("、") + "。客户端信任请用左侧检查。"
      : (labels[data.stage] || "检查失败") + (data.status ? "（HTTP " + data.status + "）" : "") + (data.reason === "timeout" ? "：请求超时" : "");
  } catch (error) { $("check-result").textContent = error.message; }
  finally { $("check-key").value = ""; button.disabled = false; }
});
load();
