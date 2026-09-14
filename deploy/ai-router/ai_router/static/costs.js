/* GLM accounting uses measured usage; money shown here is not budget occupancy. */
const costsState = {sequence: 0, detailSequence: 0, offset: 0, next: null, requestId: null};
const costMoney = value => {
  if (value == null) return "待核对";
  const [whole, fraction = ""] = String(value).split(".");
  return `¥${whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",")}.${fraction.replace(/0+$/, "").padEnd(2, "0")}`;
};
const costCount = value => value == null ? "未知" : Number(value).toLocaleString("zh-CN");
const costStatus = value => ({measured: "实测用量估算", pending: "进行中", unknown: "待核对"}[value] || "待核对");

function costParameters() {
  const p = new URLSearchParams();
  for (const [id, key] of [["cost-model", "model"], ["cost-client", "client_id"], ["cost-conversation", "conversation_id"], ["cost-measurement", "measurement"]]) {
    if (byId(id).value.trim()) p.set(key, byId(id).value.trim());
  }
  const from = byId("cost-from").value;
  const to = byId("cost-to").value;
  if (from) p.set("since", String(Date.parse(`${from}T00:00:00+08:00`) / 1000));
  if (to) p.set("until", String(Date.parse(`${to}T00:00:00+08:00`) / 1000 + 86400));
  return p;
}

function costRequestLink(item) {
  return /^(zhipu\/)?glm-5\.3(-flash)?$/.test(item.selected_model || item.requested_model || "") ? costRequestButton(item.request_id) : "";
}

function costRequestButton(id) {
  return `<button type="button" class="secondary" data-cost-request="${escapeHtml(id)}">费用详情</button>`;
}

function costFindings(items) {
  return (items || []).map(f => `<span class="table-secondary">${escapeHtml(f.label)}${f.saving_upper_bound_cny != null ? ` · 价差上限 ${costMoney(f.saving_upper_bound_cny)}` : ""}</span>`).join("");
}

async function loadCosts(silent = false) {
  const sequence = ++costsState.sequence;
  const p = costParameters();
  const summary = new URLSearchParams(p);
  summary.delete("measurement");
  const reconciliation = new URLSearchParams(summary);
  reconciliation.delete("client_id");
  reconciliation.delete("conversation_id");
  p.set("limit", "30"); p.set("offset", String(costsState.offset)); p.set("sort", byId("cost-sort").value);
  try {
    const [s, r, b] = await Promise.all([
      api(`/api/costs/summary?${summary}`), api(`/api/costs/requests?${p}`), api(`/api/costs/reconciliation?${reconciliation}`),
    ]);
    if (sequence !== costsState.sequence) return;
    const metrics = [
      ["今日用量估算", costMoney(s.today.total_cny)], ["本月用量估算", costMoney(s.month.total_cny)],
      ["筛选期间费用", costMoney(s.total_cny)], ["缓存折扣节省", costMoney(s.saving_cny)],
      ["待核对 / 进行中", `${s.unknown_attempts} / ${s.pending_attempts}`],
      ["完整用量覆盖率", s.coverage == null ? "—" : `${(s.coverage * 100).toFixed(1)}%`],
    ];
    byId("cost-summary").innerHTML = metrics.map(([label, value]) => `<div class="metric"><span>${label}</span><strong>${value}</strong></div>`).join("");
    byId("cost-composition").textContent = `普通输入 ${costMoney(s.input_cny)} · 缓存输入 ${costMoney(s.cached_cny)} · 输出 ${costMoney(s.output_cny)}。输入 ${costCount(s.input_tokens)} Token，缓存 ${costCount(s.cached_tokens)} Token。`;
    byId("cost-coverage").textContent = `共 ${s.requests} 个请求、${s.attempts} 次上游尝试；金额仅累计有完整用量的部分。${s.backfill_remaining ? `历史回算中，剩余 ${s.backfill_remaining} 条审计。` : ""}首版 GLM 按量记账，未启用新的预算拦截。`;
    byId("cost-table").innerHTML = r.items.length ? r.items.map(item => `<tr>
      <td>${formatTime(item.started_at)}</td><td><code class="request-id-full">${escapeHtml(item.request_id)}</code><span class="table-secondary">尝试 ${item.attempt} · ${escapeHtml(item.client_id)}</span>${costRequestButton(item.request_id)}</td>
      <td>${escapeHtml(item.model)}<span class="table-secondary">${costStatus(item.measurement)}</span></td>
      <td>${costCount(item.input_tokens)} / ${costCount(item.cached_tokens)} / ${costCount(item.output_tokens)}</td>
      <td>${costMoney(item.total_cny)}<span class="table-secondary">缓存折扣节省 ${costMoney(item.saving_cny)}</span></td>
      <td>${costFindings(item.findings) || "待检查消耗；金额高不等于浪费"}</td></tr>`).join("") : emptyRow(6, "没有符合条件的 GLM 上游调用");
    costsState.next = r.next_offset;
    byId("cost-prev").disabled = costsState.offset === 0;
    byId("cost-next").disabled = r.next_offset == null;
    byId("cost-page").textContent = `匹配 ${r.total} 次上游尝试 · 第 ${Math.floor(costsState.offset / 30) + 1} 页`;
    byId("cost-conversations").innerHTML = s.top_conversations.length ? s.top_conversations.map(c => `<tr><td><button type="button" class="secondary" ${c.conversation_id ? `data-cost-conversation="${escapeHtml(c.conversation_id)}"` : `data-cost-request="${escapeHtml(c.request_id)}"`} data-cost-client="${escapeHtml(c.client_id)}">${escapeHtml(c.conversation_id || "无会话 ID")}</button></td><td>${escapeHtml(c.client_id)}</td><td>${c.requests} / ${c.attempts}</td><td>${costMoney(c.total_cny)}</td><td>${c.unknown_attempts}</td></tr>`).join("") : emptyRow(5, "暂无会话用量");
    byId("cost-bill-table").innerHTML = b.items.length ? b.items.map(d => `<tr><td>${escapeHtml(d.day)}</td><td>${escapeHtml(d.model)}</td><td>${costMoney(d.router.total_cny)}<span class="table-secondary">${d.router.unknown_attempts} 次待核对</span></td><td>${costMoney(d.official_gross_cny)}</td><td>${costMoney(d.adjustment_cny)}</td><td>${costMoney(d.official_net_cny)}</td><td>${costMoney(d.difference_cny)}<span class="table-secondary">${d.comparison_complete ? "Router 用量完整，按已导入行核对" : "账单或用量未齐"}</span></td></tr>`).join("") : emptyRow(7, "暂无账单与用量");
    byId("cost-prices").innerHTML = s.prices.versions.map(v => `<tr><td>${escapeHtml(v.model)}</td><td>${escapeHtml(v.since.slice(0, 10))} 至 ${v.until ? escapeHtml(v.until.slice(0, 10)) + "（不含）" : "下一价格版本"}</td><td>${v.input / 1000} / ${v.cached / 1000} / ${v.output / 1000}</td></tr>`).join("");
    if (!silent) notice("费用已更新；估算费用与官方结算分别列示。");
  } catch (error) {
    if (sequence === costsState.sequence) notice(error.message, true);
  }
}

async function showCostDetail(id, container = "cost-detail", silent = false) {
  const sequence = ++costsState.detailSequence;
  costsState.requestId = id;
  const target = byId(container);
  if (!target) return;
  target.hidden = false;
  if (!silent) target.textContent = "正在读取费用证据…";
  try {
    const data = await api(`/api/costs/requests/${encodeURIComponent(id)}`);
    if (sequence !== costsState.detailSequence) return;
    const a = data.analysis;
    const rows = data.items.map(r => `<tr><td>${r.attempt}</td><td>${escapeHtml(r.model)}</td><td>${costStatus(r.measurement)}</td><td>${costCount(r.uncached_tokens)} / ${costCount(r.cached_tokens)} / ${costCount(r.output_tokens)}</td><td>${costMoney(r.total_cny)}</td><td>${escapeHtml(r.price_version || "未知")}</td></tr>`).join("");
    target.innerHTML = `<h3>请求费用详情</h3><code class="request-id-full">${escapeHtml(id)}</code><p>来源：上游逐请求用量；金额按价格版本计算，实际扣款以官方账单为准。</p>
      <div class="table-wrap"><table><thead><tr><th>尝试</th><th>实际模型</th><th>用量状态</th><th>未缓存 / 缓存 / 输出 Token</th><th>估算费用</th><th>价格版本</th></tr></thead><tbody>${rows}</tbody></table></div>
      ${a.state === "available" ? `<p>请求体积 ${costCount(a.total_chars)} 字符；工具定义 ${costCount(a.tools_chars)} 字符。${Object.entries(a.role_chars).map(([role, n]) => `${escapeHtml(role)}：${costCount(n)}`).join(" · ")}。</p><p>system / developer ${costCount(a.system_chars)} 字符；历史消息（不含 system）${costCount(a.historical_chars)} 字符；新增消息 ${costCount(a.new_message_chars)} 字符。字符数用于定位体积，不是计费 Token。</p><p>较大的工具定义：${a.top_tools.map(t => `${escapeHtml(t.name)} ${costCount(t.chars)} 字符`).join("；")}。</p>` : "<p>正文归档不可用，仍可查看上游用量和费用。</p>"}
      ${costFindings(a.findings) || "<p>当前没有足以确认浪费的前缀证据。</p>"}<p>缓存价差上限不是承诺节省；不会自动删除内容、缩短输出或替换模型。</p>`;
    if (!silent) target.scrollIntoView({block: "nearest", behavior: "smooth"});
  } catch (error) {
    if (sequence === costsState.detailSequence) target.textContent = error.status === 404 ? "该请求没有 GLM 上游计费记录。" : error.message;
  }
}

async function importCostBill() {
  const input = byId("cost-bill-file");
  const file = input.files[0];
  if (!file) { notice("请选择智谱 XLSX 费用明细。", true); return; }
  const button = byId("cost-bill-import"); button.disabled = true;
  try {
    const result = await api("/api/costs/import", {method: "POST", headers: {"Content-Type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}, body: file});
    await loadCosts(true);
    notice(`账单导入完成：新增 ${result.inserted} 行，已存在 ${result.duplicates} 行。`);
    input.value = "";
  } catch (error) { notice(error.message, true); }
  finally { button.disabled = false; }
}

for (const id of ["cost-from", "cost-to", "cost-model", "cost-client", "cost-conversation", "cost-measurement", "cost-sort"]) {
  byId(id).addEventListener("change", () => { costsState.offset = 0; void loadCosts(); });
}
byId("cost-prev").addEventListener("click", () => { costsState.offset = Math.max(0, costsState.offset - 30); void loadCosts(); });
byId("cost-next").addEventListener("click", () => { if (costsState.next != null) { costsState.offset = costsState.next; void loadCosts(); } });
byId("cost-bill-import").addEventListener("click", () => void importCostBill());
byId("refresh").addEventListener("click", () => { if (state.view === "costs") void loadCosts(); });
document.addEventListener("click", event => {
  const request = event.target.closest("[data-cost-request]");
  if (request) { switchView("costs"); void showCostDetail(request.dataset.costRequest); }
  const conversation = event.target.closest("[data-cost-conversation]");
  if (conversation) {
    byId("cost-conversation").value = conversation.dataset.costConversation;
    byId("cost-client").value = conversation.dataset.costClient;
    costsState.offset = 0; void loadCosts();
  }
});

if (state.view === "costs" && state.key) void loadCosts(true);
