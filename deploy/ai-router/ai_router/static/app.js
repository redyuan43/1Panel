const state = {
  key: sessionStorage.getItem("ai-router-admin-key") || "",
  settings: null,
  dashboard: null,
  clients: [],
  editingClientId: null,
  selectedClientId: null,
  view: "dashboard",
  timer: null,
};

const viewTitles = {
  dashboard: "运行总览",
  nodes: "模型节点",
  requests: "请求记录",
  clients: "客户端账号",
  settings: "策略设置",
};

const weightLabels = {
  quality: "质量",
  load: "负载",
  latency: "延迟",
  context: "上下文余量",
  cost: "成本",
  locality: "本机优先",
};

const reasonLabels = {
  quality_score: "质量评分",
  explicit_model: "显式模型",
  conversation_affinity: "会话亲和",
  capacity_spillover: "容量分流",
  affinity_spillover: "亲和迁移",
  cloud_capacity_fallback: "云端容量兜底",
  local_priority: "本地优先",
  cloud_priority: "云端优先",
  balanced_score: "均衡评分",
  preferred_tier: "高难任务升级",
  logical_affinity: "逻辑亲和",
  tier_requirement: "层级要求",
};

const affinityLabels = {
  new: "新会话",
  hit: "缓存命中",
  "logical-hit": "逻辑命中",
  explicit: "显式",
  migrated: "已迁移",
  "physical-failover": "同模型迁移",
};

const statusLabels = {
  running: "运行中",
  succeeded: "成功",
  failed: "失败",
  stale: "已中断",
};

const byId = (id) => document.getElementById(id);

function authHeaders() {
  return {
    Authorization: `Bearer ${state.key}`,
    "Content-Type": "application/json",
  };
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {...authHeaders(), ...(options.headers || {})},
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(payload.error?.message || `HTTP ${response.status}`);
  }
  return payload;
}

function notice(message = "", error = false) {
  const target = byId("notice");
  target.textContent = message;
  target.classList.toggle("error", error);
}

function setConnected(connected) {
  const target = byId("connection-state");
  target.classList.toggle("connected", connected);
  target.innerHTML = `<i></i>${connected ? "已连接" : "未连接"}`;
}

async function connect() {
  state.key = byId("admin-key").value.trim();
  if (!state.key) {
    notice("请输入管理密钥。", true);
    return;
  }
  sessionStorage.setItem("ai-router-admin-key", state.key);
  notice("正在连接控制面...");
  try {
    await Promise.all([loadDashboard(), loadSettings(), loadClients()]);
    setConnected(true);
    notice("");
    startPolling();
  } catch (error) {
    setConnected(false);
    notice(error.message, true);
  }
}

async function loadDashboard(silent = false) {
  if (!state.key) return;
  if (!silent) byId("refresh").disabled = true;
  try {
    state.dashboard = await api("/api/dashboard?limit=100");
    renderDashboard();
    byId("last-updated").textContent =
      `更新于 ${formatTime(state.dashboard.generated_at)}`;
    setConnected(true);
    if (!silent) notice("");
  } catch (error) {
    setConnected(false);
    if (!silent) notice(error.message, true);
  } finally {
    byId("refresh").disabled = false;
  }
}

function renderDashboard() {
  const data = state.dashboard;
  if (!data) return;
  renderSummary(data);
  renderRouterInstances(data.router_instances || []);
  renderAlerts(data.alerts);
  renderNodeOverview(data.endpoints);
  renderDistribution(data.node_distribution);
  renderBudget(data.cloud_budget, data.routing_mode);
  renderActiveRequests(data.requests);
  renderRecentRequests(data.requests.slice(0, 8));
  renderEndpointTable(data.endpoints);
  renderWorkerTable(data.workers);
  renderRequestTable();
}

async function loadClients(silent = false) {
  if (!state.key) return;
  try {
    const payload = await api("/api/clients");
    state.clients = payload.clients || [];
    renderClients();
    if (!silent) notice("");
  } catch (error) {
    if (!silent) notice(error.message, true);
  }
}

function renderClients() {
  const status = byId("client-status-filter")?.value || "";
  const clients = state.clients.filter((item) =>
    !status || (status === "enabled" ? item.enabled : !item.enabled)
  );
  byId("client-count").textContent = `${clients.length} 个账号`;
  byId("client-table").innerHTML = clients.length
    ? clients.map((item) => {
      const activeKeys = item.keys.filter((key) => key.status === "active");
      const usage = item.usage_24h || {};
      const lastUsed = item.keys.reduce(
        (latest, key) => Math.max(latest, Number(key.last_used_at || 0)),
        0,
      );
      return `
        <tr>
          <td>${accountStatusBadge(item.enabled)}</td>
          <td>
            <strong class="table-primary">${escapeHtml(item.name)}</strong>
            <span class="table-secondary">${escapeHtml(item.id)} · ${escapeHtml(item.source)}</span>
          </td>
          <td>${clientModelSummary(item.models)}</td>
          <td>
            <strong class="table-primary">${formatTokens(item.tpm_limit)} TPM</strong>
            <span class="table-secondary">${item.rpm_limit} RPM · ${item.max_parallel_requests} 并发</span>
          </td>
          <td>
            <strong class="table-primary">${activeKeys.length} 把有效</strong>
            <span class="table-secondary">${item.keys.length} 把总计</span>
          </td>
          <td>
            <strong class="table-primary">${usage.requests || 0} 次 · ${formatTokens((usage.input_tokens || 0) + (usage.output_tokens || 0))}</strong>
            <span class="table-secondary">${usage.errors || 0} 次错误</span>
          </td>
          <td>${lastUsed ? formatTime(lastUsed) : "—"}</td>
          <td>
            <div class="row-actions">
              <button class="secondary compact" type="button" data-client-edit="${escapeHtml(item.id)}">编辑</button>
              <button class="secondary compact" type="button" data-client-keys="${escapeHtml(item.id)}">Key</button>
              <button class="secondary compact ${item.enabled ? "danger-action" : ""}" type="button" data-client-toggle="${escapeHtml(item.id)}">
                ${item.enabled ? "停用" : "启用"}
              </button>
            </div>
          </td>
        </tr>`;
    }).join("")
    : emptyRow(8, "没有符合筛选条件的客户端账号");
  bindClientActions();
}

function clientModelSummary(models = []) {
  if (models.includes("*")) return '<span class="capability-chip">全部模型</span>';
  return models
    .map((model) => `<span class="capability-chip">${escapeHtml(shortModel(model))}</span>`)
    .join(" ");
}

function accountStatusBadge(enabled) {
  return `<span class="badge ${enabled ? "success" : "danger"}"><i></i>${enabled ? "已启用" : "已停用"}</span>`;
}

function keyStatusBadge(status) {
  const active = status === "active";
  return `<span class="badge ${active ? "success" : "danger"}"><i></i>${active ? "有效" : "已撤销"}</span>`;
}

function bindClientActions() {
  document.querySelectorAll("[data-client-edit]").forEach((button) => {
    button.addEventListener("click", () => openClientDialog(button.dataset.clientEdit));
  });
  document.querySelectorAll("[data-client-keys]").forEach((button) => {
    button.addEventListener("click", () => openKeysDialog(button.dataset.clientKeys));
  });
  document.querySelectorAll("[data-client-toggle]").forEach((button) => {
    button.addEventListener("click", () => toggleClient(button.dataset.clientToggle));
  });
}

function availableClientModels() {
  const values = ["auto"];
  (state.dashboard?.endpoints || []).forEach(({endpoint}) => {
    if (!values.includes(endpoint.public_model)) values.push(endpoint.public_model);
  });
  return values;
}

function renderClientModels(selected = ["auto"]) {
  const target = byId("client-models");
  const allSelected = selected.includes("*");
  const values = [
    {id: "*", label: "全部模型"},
    ...availableClientModels().map((id) => ({id, label: shortModel(id)})),
  ];
  target.innerHTML = values.map((item) => `
    <label>
      <input type="checkbox" data-client-model="${escapeHtml(item.id)}" ${allSelected || selected.includes(item.id) ? "checked" : ""}>
      <span>${escapeHtml(item.label)}</span>
    </label>
  `).join("");
  target.querySelector('[data-client-model="*"]').addEventListener("change", (event) => {
    target.querySelectorAll("[data-client-model]").forEach((input) => {
      input.checked = event.target.checked;
      input.disabled = event.target.checked && input !== event.target;
    });
  });
  if (allSelected) {
    target.querySelectorAll("[data-client-model]").forEach((input) => {
      input.disabled = input.dataset.clientModel !== "*";
    });
  }
}

function openClientDialog(clientId = null) {
  const client = state.clients.find((item) => item.id === clientId);
  state.editingClientId = client?.id || null;
  byId("client-dialog-title").textContent = client ? "编辑客户端账号" : "新建客户端账号";
  byId("client-id").value = client?.id || "";
  byId("client-id").disabled = Boolean(client);
  byId("client-name").value = client?.name || "";
  byId("client-rpm").value = client?.rpm_limit || 120;
  byId("client-tpm").value = client?.tpm_limit || 1000000;
  byId("client-parallel").value = client?.max_parallel_requests || 4;
  byId("client-enabled").checked = client?.enabled ?? true;
  renderClientModels(client?.models || ["auto"]);
  byId("client-dialog").showModal();
}

function collectClient() {
  const selected = [...document.querySelectorAll("[data-client-model]:checked")]
    .map((input) => input.dataset.clientModel);
  return {
    id: byId("client-id").value.trim(),
    name: byId("client-name").value.trim(),
    enabled: byId("client-enabled").checked,
    models: selected.includes("*") ? ["*"] : selected,
    rpm_limit: Number(byId("client-rpm").value),
    tpm_limit: Number(byId("client-tpm").value),
    max_parallel_requests: Number(byId("client-parallel").value),
  };
}

async function saveClient(event) {
  event.preventDefault();
  const value = collectClient();
  if (!value.models.length) {
    notice("至少选择一个允许模型。", true);
    return;
  }
  const editing = state.editingClientId;
  try {
    await api(editing ? `/api/clients/${encodeURIComponent(editing)}` : "/api/clients", {
      method: editing ? "PATCH" : "POST",
      body: JSON.stringify(value),
    });
    byId("client-dialog").close();
    await loadClients(true);
    notice(editing ? "客户端账号已更新。" : "客户端账号已创建。");
  } catch (error) {
    notice(error.message, true);
  }
}

async function toggleClient(clientId) {
  const client = state.clients.find((item) => item.id === clientId);
  if (!client) return;
  const action = client.enabled ? "停用" : "启用";
  if (!confirm(`${action}客户端账号 ${client.name}？`)) return;
  try {
    await api(`/api/clients/${encodeURIComponent(clientId)}`, {
      method: "PATCH",
      body: JSON.stringify({...client, enabled: !client.enabled}),
    });
    await loadClients(true);
    notice(`客户端账号已${action}。`);
  } catch (error) {
    notice(error.message, true);
  }
}

function openKeysDialog(clientId) {
  state.selectedClientId = clientId;
  const client = state.clients.find((item) => item.id === clientId);
  if (!client) return;
  byId("keys-dialog-title").textContent = `${client.name} · API Key`;
  byId("key-label").value = "";
  renderKeys(client);
  byId("keys-dialog").showModal();
}

function renderKeys(client) {
  byId("key-table").innerHTML = client.keys.length
    ? client.keys.map((key) => `
      <tr>
        <td>${keyStatusBadge(key.status)}</td>
        <td>${escapeHtml(key.label)}</td>
        <td><code>${escapeHtml(key.hint)}</code></td>
        <td>${key.source === "legacy_env" ? "旧环境变量" : "管理台生成"}</td>
        <td>${key.last_used_at ? formatTime(key.last_used_at) : "—"}</td>
        <td>
          ${key.status === "active"
            ? `<button class="secondary compact danger-action" type="button" data-key-revoke="${escapeHtml(key.key_id)}">撤销</button>`
            : "已撤销"}
        </td>
      </tr>
    `).join("")
    : emptyRow(6, "该账号还没有 API Key");
  document.querySelectorAll("[data-key-revoke]").forEach((button) => {
    button.addEventListener("click", () => revokeKey(button.dataset.keyRevoke));
  });
}

async function createKey(event) {
  event.preventDefault();
  const clientId = state.selectedClientId;
  if (!clientId) return;
  try {
    const payload = await api(`/api/clients/${encodeURIComponent(clientId)}/keys`, {
      method: "POST",
      body: JSON.stringify({label: byId("key-label").value.trim()}),
    });
    byId("generated-key").value = payload.api_key;
    byId("secret-dialog").showModal();
    await loadClients(true);
    const client = state.clients.find((item) => item.id === clientId);
    if (client) renderKeys(client);
    notice("新 API Key 已生成。");
  } catch (error) {
    notice(error.message, true);
  }
}

async function revokeKey(keyId) {
  const clientId = state.selectedClientId;
  if (!clientId || !confirm("撤销后该 Key 将立即失效，是否继续？")) return;
  try {
    await api(`/api/clients/${encodeURIComponent(clientId)}/keys/${encodeURIComponent(keyId)}/revoke`, {
      method: "POST",
    });
    await loadClients(true);
    const client = state.clients.find((item) => item.id === clientId);
    if (client) renderKeys(client);
    notice("API Key 已撤销。");
  } catch (error) {
    notice(error.message, true);
  }
}

function renderRouterInstances(instances) {
  byId("router-instance-count").textContent =
    instances.length ? `${instances.length} 个实例` : "暂无实例";
  byId("router-instance-table").innerHTML = instances.length
    ? instances.map((item) => {
      const cleanup = item.startup_cleanup || {};
      const cleanupCount = [
        cleanup.deployment_members,
        cleanup.client_members,
        cleanup.queue_members,
        cleanup.conversation_locks,
      ].reduce((total, value) => total + Number(value || 0), 0);
      const running = item.status === "running" && !item.draining;
      const status = item.draining
        ? "排空中"
        : item.status === "stopped"
          ? "已停止"
          : "运行中";
      return `
        <tr>
          <td>${healthBadge(running)} ${escapeHtml(status)}</td>
          <td><code>${escapeHtml(item.instance_id || "—")}</code></td>
          <td><code title="${escapeHtml(item.boot_id || "")}">${escapeHtml(shortId(item.boot_id || "—", 16))}</code></td>
          <td>${Number(item.active_request_count || 0)}</td>
          <td>${cleanupCount}</td>
          <td>${formatTime(item.updated_at)}</td>
        </tr>
      `;
    }).join("")
    : emptyRow(6, "Router API 实例尚未上报状态");
}

function renderSummary(data) {
  const summary = data.summary;
  const budget = data.cloud_budget;
  const metrics = [
    {
      label: "健康节点",
      value: `${summary.healthy_endpoints}/${summary.total_endpoints}`,
      detail: summary.healthy_endpoints === summary.total_endpoints ? "全部在线" : "需要处理",
      tone: summary.healthy_endpoints === summary.total_endpoints ? "good" : "bad",
    },
    {
      label: "可用 Worker",
      value: `${summary.ready_workers}/${summary.total_workers}`,
      detail: "AI 本地模型池",
      tone: summary.ready_workers === summary.total_workers ? "good" : "warn",
    },
    {
      label: "运行中请求",
      value: String(summary.active_requests),
      detail: summary.active_requests ? "正在推理或排队" : "当前空闲",
      tone: summary.active_requests ? "live" : "",
    },
    {
      label: "近期成功率",
      value: summary.success_rate == null ? "—" : formatPercent(summary.success_rate),
      detail: summary.average_latency_ms == null
        ? "暂无完成记录"
        : `平均 ${formatDuration(summary.average_latency_ms)}`,
      tone: summary.success_rate == null || summary.success_rate >= 0.98 ? "good" : "warn",
    },
    {
      label: "云端预算",
      value: `$${budget.spent_usd.toFixed(4)}`,
      detail: `剩余 $${budget.remaining_usd.toFixed(2)}`,
      tone: budget.usage_ratio >= 0.8 ? "warn" : "",
    },
  ];
  byId("summary").innerHTML = metrics.map((item) => `
    <article class="metric ${item.tone}">
      <span>${escapeHtml(item.label)}</span>
      <strong>${escapeHtml(item.value)}</strong>
      <small>${escapeHtml(item.detail)}</small>
    </article>
  `).join("");
}

function renderAlerts(alerts) {
  byId("alert-count").textContent = alerts.length ? `${alerts.length} 项` : "无告警";
  const target = byId("alerts");
  if (!alerts.length) {
    target.innerHTML = `
      <div class="alert healthy">
        <span class="alert-indicator"></span>
        <div><strong>运行正常</strong><p>当前没有需要处理的路由告警。</p></div>
      </div>`;
    return;
  }
  target.innerHTML = alerts.map((item) => `
    <div class="alert ${escapeHtml(item.level)}">
      <span class="alert-indicator"></span>
      <div>
        <strong>${escapeHtml(item.title)}</strong>
        <p>${escapeHtml(item.detail)}</p>
      </div>
    </div>
  `).join("");
}

function renderNodeOverview(endpoints) {
  byId("node-overview").innerHTML = endpoints.map(({endpoint, status}) => {
    const load = Math.round(status.load_headroom * 100);
    return `
      <div class="node-row">
        <div class="node-identity">
          <span class="health-dot ${status.healthy ? "ok" : "bad"}"></span>
          <div>
            <strong>${escapeHtml(endpoint.node.toUpperCase())}</strong>
            <small>${escapeHtml(shortModel(endpoint.public_model))}</small>
          </div>
        </div>
        <div class="node-load">
          <span>${status.healthy ? `${load}% 余量` : "不可用"}</span>
          <div class="load-track">
            <i style="width:${status.healthy ? load : 0}%"></i>
          </div>
        </div>
      </div>`;
  }).join("");
}

function renderDistribution(distribution) {
  const entries = Object.entries(distribution);
  const total = entries.reduce((sum, [, value]) => sum + value, 0);
  const target = byId("distribution");
  if (!total) {
    target.innerHTML = '<p class="empty">暂无已完成请求</p>';
    return;
  }
  target.innerHTML = entries
    .sort((a, b) => b[1] - a[1])
    .map(([node, count]) => {
      const ratio = count / total;
      return `
        <div class="distribution-row">
          <div><span>${escapeHtml(node.toUpperCase())}</span><strong>${count}</strong></div>
          <div class="distribution-track">
            <i class="node-${escapeHtml(node)}" style="width:${ratio * 100}%"></i>
          </div>
        </div>`;
    }).join("");
}

function renderBudget(budget, routingMode) {
  const percent = Math.round(budget.usage_ratio * 1000) / 10;
  byId("budget").innerHTML = `
    <div class="budget-head">
      <span>DeepSeek ${escapeHtml(budget.month)}</span>
      <strong>$${budget.spent_usd.toFixed(4)} / $${budget.monthly_budget_usd.toFixed(2)}</strong>
    </div>
    <div class="budget-track"><i style="width:${Math.min(100, percent)}%"></i></div>
    <small>${providerPriorityLabel(routingMode)} · ${budget.auto_escalate ? "自动升级已开启" : "自动升级已关闭"} · ${percent}%</small>
  `;
}

function renderActiveRequests(requests) {
  const active = requests.filter((item) => item.status === "running");
  byId("active-count").textContent = active.length ? `${active.length} 个请求` : "当前空闲";
  const target = byId("active-requests");
  if (!active.length) {
    target.innerHTML = '<p class="empty">没有正在运行或排队的请求。</p>';
    return;
  }
  target.innerHTML = active.map((item) => `
    <article class="active-request">
      <span class="activity-pulse"></span>
      <div class="active-main">
        <strong>${escapeHtml(item.selected_model || item.requested_model || "等待路由")}</strong>
        <span>${escapeHtml(item.node || "—")} · ${escapeHtml(reasonLabel(item.reason))}</span>
      </div>
      <div class="active-meta">
        <span>${formatRelative(item.timestamp)}</span>
        <code>${escapeHtml(shortId(item.request_id))}</code>
      </div>
    </article>
  `).join("");
}

function renderRecentRequests(requests) {
  byId("recent-requests").innerHTML = requests.length
    ? requests.map((item) => `
      <tr>
        <td>${statusBadge(item.status, item.status_code)}</td>
        <td>${formatTime(item.timestamp)}</td>
        <td>
          <strong class="table-primary">${escapeHtml(shortModel(item.selected_model || item.requested_model))}</strong>
          <span class="table-secondary">${escapeHtml(item.task || "general")}</span>
        </td>
        <td><span class="node-label node-${escapeHtml(item.node || "unknown")}">${escapeHtml((item.node || "—").toUpperCase())}</span></td>
        <td>${escapeHtml(reasonLabel(item.reason))}</td>
        <td>${item.latency_ms == null ? "—" : formatDuration(item.latency_ms)}</td>
      </tr>
    `).join("")
    : emptyRow(6, "暂无请求记录");
}

function renderEndpointTable(endpoints) {
  byId("endpoint-count").textContent =
    `${endpoints.filter((item) => item.status.healthy).length}/${endpoints.length} 健康`;
  byId("endpoint-table").innerHTML = endpoints.map(({endpoint, status}) => `
    <tr>
      <td>${healthBadge(status.healthy)}</td>
      <td><span class="node-label node-${escapeHtml(endpoint.node)}">${escapeHtml(endpoint.node.toUpperCase())}</span></td>
      <td>
        <strong class="table-primary">${escapeHtml(endpoint.public_model)}</strong>
        <span class="table-secondary">${escapeHtml(endpoint.id)}</span>
      </td>
      <td>${escapeHtml(endpoint.tier)}</td>
      <td>${formatTokens(status.eligible_context_tokens || endpoint.safe_context_tokens)}</td>
      <td>${status.healthy ? `${Math.round(status.load_headroom * 100)}%` : "—"}</td>
      <td>${capabilitySummary(endpoint.capabilities, status.detail?.effective_modalities || endpoint.modalities)}</td>
      <td>
        <strong class="table-primary">${escapeHtml(endpoint.capabilities?.validation_status || "unverified")}</strong>
        <span class="table-secondary">${escapeHtml(endpoint.capabilities?.validated_at || "—")}</span>
      </td>
      <td>${endpoint.auto_candidate ? "是" : "否"}</td>
    </tr>
  `).join("");
}

function renderWorkerTable(workers) {
  const ready = workers.filter((item) =>
    item.ready && item.schedulable !== false && item.state === "available"
  ).length;
  byId("worker-count").textContent = `${ready}/${workers.length} 可调度`;
  byId("worker-table").innerHTML = workers.length
    ? workers.map((item) => `
      <tr>
        <td>${workerStatusBadge(item)}</td>
        <td><code>${escapeHtml(item.account_alias || item.port || "—")}</code></td>
        <td>
          <strong class="table-primary">${escapeHtml(item.names?.join(" + ") || (item.account_alias ? "Codex Pro" : workerTier(item.priority)))}</strong>
          <span class="table-secondary">${escapeHtml(item.tier || "—")}</span>
        </td>
        <td><code>${escapeHtml(item.profile_id || "—")}</code></td>
        <td>${formatTokens(item.safe_context_tokens)}</td>
        <td>${escapeHtml(item.cache_type_k ? `${item.cache_type_k}/${item.cache_type_v}` : "—")}</td>
        <td>${workerVisionSummary(item)}</td>
        <td>
          <strong class="table-primary">${item.config_drift?.length ? "不一致" : "一致"}</strong>
          <span class="table-secondary">${escapeHtml(item.config_drift?.join(", ") || shortId(item.runtime_fingerprint || "—", 16))}</span>
        </td>
        <td><code class="worker-id" title="${escapeHtml(item.worker_id)}">${escapeHtml(shortId(item.worker_id, 34))}</code></td>
      </tr>
    `).join("")
    : emptyRow(9, "当前端点没有公开物理 Worker");
}

function renderRequestTable() {
  const requests = state.dashboard?.requests || [];
  const node = byId("node-filter").value;
  const status = byId("status-filter").value;
  const nodes = [...new Set(requests.map((item) => item.node).filter(Boolean))].sort();
  const currentNode = node;
  byId("node-filter").innerHTML =
    '<option value="">全部节点</option>' +
    nodes.map((item) => `<option value="${escapeHtml(item)}">${escapeHtml(item.toUpperCase())}</option>`).join("");
  byId("node-filter").value = currentNode;
  const filtered = requests.filter((item) =>
    (!node || item.node === node) && (!status || item.status === status)
  );
  byId("request-count").textContent = `${filtered.length} 条`;
  byId("request-table").innerHTML = filtered.length
    ? filtered.map((item) => `
      <tr>
        <td>${statusBadge(item.status, item.status_code)}</td>
        <td>${formatTime(item.timestamp)}</td>
        <td>
          <strong class="table-primary">${escapeHtml(shortModel(item.requested_model))}</strong>
          <span class="table-secondary">${escapeHtml(shortId(item.request_id))}</span>
        </td>
        <td>
          <strong class="table-primary">${escapeHtml(shortModel(item.selected_model))}</strong>
          <span class="table-secondary">${escapeHtml(reasonLabel(item.reason))}</span>
        </td>
        <td>
          <code title="${escapeHtml(item.deployment_id || "")}">${escapeHtml(shortId(item.deployment_id || "—", 22))}</code>
          <span class="table-secondary">${escapeHtml(item.deployment_profile_id || "—")}${item.image_resizes ? ` · 缩图 ${item.image_resizes}` : ""}</span>
        </td>
        <td>${escapeHtml(item.task || "—")}</td>
        <td>
          <strong class="table-primary">${escapeHtml(protocolLabel(item.protocol, item.native_or_adapter))}</strong>
          <span class="table-secondary">${Number(item.tool_history_repairs || 0)} 次修复</span>
        </td>
        <td>${escapeHtml(affinityLabel(item.affinity))}</td>
        <td>${item.attempts || 1} / ${item.capacity_attempts || 1}</td>
        <td>${item.queue_wait_ms == null ? "—" : formatDuration(item.queue_wait_ms)}</td>
        <td>${formatTokens(item.prompt_tokens || 0)}</td>
        <td>${formatCacheHit(item.cached_prompt_tokens, item.cache_hit_ratio)}</td>
        <td>${item.latency_ms == null ? formatRelative(item.timestamp) : formatDuration(item.latency_ms)}</td>
      </tr>
    `).join("")
    : emptyRow(13, "没有符合筛选条件的请求");
}

function formatCacheHit(tokens, ratio) {
  if (tokens == null) return "—";
  const percent = ratio == null ? "—" : `${Math.round(Number(ratio) * 100)}%`;
  return `${formatTokens(tokens)} / ${percent}`;
}

function value(path, fallback = "") {
  let current = state.settings;
  for (const part of path.split(".")) current = current?.[part];
  return current ?? fallback;
}

async function loadSettings() {
  if (!state.key) return;
  const payload = await api("/api/settings");
  state.settings = payload.settings;
  renderSettings();
}

function renderSettings() {
  if (!state.settings) return;
  byId("cloud-enabled").checked = Boolean(value("cloud.enabled", false));
  byId("cloud-auto").checked = Boolean(value("cloud.auto_escalate", false));
  byId("cloud-budget").value = value("cloud.monthly_budget", 0);
  byId("cloud-providers").value = value("cloud.allowed_providers", []).join(", ");
  byId("cloud-models").value = value("cloud.allowed_models", []).join(", ");
  byId("evaluator-enabled").checked = Boolean(value("evaluator.enabled", false));
  byId("evaluator-model").value = value("evaluator.model_id");
  byId("evaluator-confidence").value = value("evaluator.confidence_threshold", 0.85);
  byId("compaction-enabled").checked = Boolean(value("compaction.enabled", true));
  byId("compaction-model").value = value("compaction.model_id");
  byId("affinity-ttl-minutes").value = Math.round(
    value("affinity.ttl_seconds", 86400) / 60,
  );
  byId("affinity-burst").value = value("affinity.max_priority_burst", 8);
  byId("queue-timeout").value = value("queue.timeout_seconds", 120);
  byId("failover-attempts").value = value("failover.max_attempts", 2);
  byId("affinity-capacity-wait").value = value(
    "routing.affinity_capacity_wait_seconds",
    3,
  );
  byId("new-request-capacity-wait").value = value(
    "routing.new_request_capacity_wait_seconds",
    0,
  );
  byId("provider-priority").value = value(
    "routing.provider_priority",
    "local_first",
  );
  byId("all-local-busy-policy").value = value(
    "routing.all_local_busy_policy",
    "cloud_or_429",
  );

  const weights = byId("weights");
  weights.replaceChildren();
  Object.entries(weightLabels).forEach(([key, label]) => {
    const wrapper = document.createElement("label");
    wrapper.textContent = label;
    const input = document.createElement("input");
    input.type = "number";
    input.min = "0";
    input.max = "1";
    input.step = "0.01";
    input.dataset.weight = key;
    input.value = value(`routing.weights.${key}`, 0);
    wrapper.append(input);
    weights.append(wrapper);
  });
}

function collectSettings() {
  const weights = {};
  document.querySelectorAll("[data-weight]").forEach((input) => {
    weights[input.dataset.weight] = Number(input.value);
  });
  return {
    affinity: {
      ...state.settings.affinity,
      ttl_seconds: Number(byId("affinity-ttl-minutes").value) * 60,
      max_priority_burst: Number(byId("affinity-burst").value),
    },
    cloud: {
      ...state.settings.cloud,
      enabled: byId("cloud-enabled").checked,
      auto_escalate: byId("cloud-auto").checked,
      monthly_budget: Number(byId("cloud-budget").value),
      allowed_providers: csv(byId("cloud-providers").value),
      allowed_models: csv(byId("cloud-models").value),
    },
    compaction: {
      ...state.settings.compaction,
      enabled: byId("compaction-enabled").checked,
      model_id: byId("compaction-model").value.trim(),
    },
    evaluator: {
      ...state.settings.evaluator,
      enabled: byId("evaluator-enabled").checked,
      model_id: byId("evaluator-model").value.trim(),
      confidence_threshold: Number(byId("evaluator-confidence").value),
    },
    failover: {
      ...state.settings.failover,
      max_attempts: Number(byId("failover-attempts").value),
    },
    health: state.settings.health,
    queue: {
      ...state.settings.queue,
      timeout_seconds: Number(byId("queue-timeout").value),
    },
    routing: {
      ...state.settings.routing,
      affinity_capacity_wait_seconds: Number(
        byId("affinity-capacity-wait").value,
      ),
      new_request_capacity_wait_seconds: Number(
        byId("new-request-capacity-wait").value,
      ),
      provider_priority: byId("provider-priority").value,
      all_local_busy_policy: byId("all-local-busy-policy").value,
      weights,
    },
  };
}

async function saveSettings(event) {
  event.preventDefault();
  const button = event.submitter;
  button.disabled = true;
  try {
    const payload = await api("/api/settings", {
      method: "PUT",
      body: JSON.stringify(collectSettings()),
    });
    state.settings = payload.settings;
    renderSettings();
    notice("设置已保存，新请求立即生效。");
    await loadDashboard(true);
  } catch (error) {
    notice(error.message, true);
  } finally {
    button.disabled = false;
  }
}

function switchView(view) {
  state.view = view;
  document.querySelectorAll(".tab").forEach((item) => {
    item.classList.toggle("active", item.dataset.view === view);
  });
  document.querySelectorAll(".view").forEach((item) => {
    item.classList.toggle("active", item.id === `${view}-view`);
  });
  byId("view-title").textContent = viewTitles[view];
  if (view === "requests") renderRequestTable();
  if (view === "clients") loadClients(true);
}

function startPolling() {
  clearInterval(state.timer);
  if (!byId("auto-refresh").checked) return;
  state.timer = setInterval(() => {
    if (!document.hidden && state.key) {
      loadDashboard(true);
      if (state.view === "clients") loadClients(true);
    }
  }, 5000);
}

function healthBadge(healthy) {
  return `<span class="badge ${healthy ? "success" : "danger"}"><i></i>${healthy ? "健康" : "不可用"}</span>`;
}

function statusBadge(status, code) {
  const tone = status === "succeeded"
    ? "success"
    : status === "running"
      ? "running"
      : status === "stale"
        ? "warning"
        : "danger";
  const label = code && status === "failed"
    ? `${code} 失败`
    : statusLabels[status] || status;
  return `<span class="badge ${tone}"><i></i>${escapeHtml(label)}</span>`;
}

function reasonLabel(value) {
  return reasonLabels[value] || value || "—";
}

function affinityLabel(value) {
  return affinityLabels[value] || value || "—";
}

function workerTier(priority) {
  if (priority == null) return "本地 Worker";
  if (priority === 0) return "V100 32GB";
  if (priority === 1) return "V100 + P40";
  return "P40";
}

function workerStatusBadge(item) {
  if (!item.ready) return '<span class="badge danger"><i></i>不可用</span>';
  if (item.config_drift?.length) {
    return '<span class="badge warning"><i></i>配置漂移</span>';
  }
  if (item.state !== "available") {
    return '<span class="badge running"><i></i>忙碌</span>';
  }
  return '<span class="badge success"><i></i>可调度</span>';
}

function workerVisionSummary(item) {
  const modalities = item.modalities || [];
  const values = [];
  if (modalities.includes("text")) values.push("文本");
  if (modalities.includes("image")) values.push("图像");
  const imageLimit = item.max_images == null
    ? item.vision_status || "unverified"
    : `${item.vision_status || "unverified"} · 最多 ${item.max_images} 图`;
  return `
    <strong class="table-primary">${values.map((value) => escapeHtml(value)).join(" · ") || "—"}</strong>
    <span class="table-secondary">${escapeHtml(imageLimit)}</span>
  `;
}

function capabilitySummary(capabilities = {}, modalities = []) {
  const values = [];
  if (modalities.includes("text")) values.push("文本");
  if (modalities.includes("image")) values.push("图像");
  if (modalities.includes("audio")) values.push("音频");
  if (capabilities.chat) values.push("Chat");
  if (capabilities.responses && capabilities.responses !== "none") {
    values.push(`Responses ${capabilities.responses}`);
  }
  if (capabilities.tools && capabilities.tools !== "none") {
    values.push(capabilities.tools === "parallel" ? "并行工具" : "单工具");
  }
  const structured = capabilities.structured_output || [];
  if (structured.includes("json_schema")) values.push("JSON Schema");
  else if (structured.includes("json_object")) values.push("JSON Object");
  if (capabilities.streaming) values.push("流式");
  return values.map((item) => `<span class="capability-chip">${escapeHtml(item)}</span>`).join(" ");
}

function protocolLabel(protocol, mode) {
  if (!protocol) return "—";
  return mode && mode !== "native"
    ? `${protocol} · ${mode}`
    : `${protocol} · native`;
}

function providerPriorityLabel(value) {
  return {
    local_first: "本地优先",
    balanced: "均衡评分",
    cloud_first: "云端优先",
  }[value] || value || "本地优先";
}

function formatTime(timestamp) {
  if (!timestamp) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  }).format(new Date(timestamp * 1000));
}

function formatRelative(timestamp) {
  const seconds = Math.max(0, Math.round(Date.now() / 1000 - timestamp));
  if (seconds < 60) return `${seconds} 秒`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟`;
  return `${Math.floor(seconds / 3600)} 小时`;
}

function formatDuration(milliseconds) {
  if (milliseconds < 1000) return `${Math.round(milliseconds)} ms`;
  return `${(milliseconds / 1000).toFixed(milliseconds < 10000 ? 2 : 1)} s`;
}

function formatTokens(value) {
  const number = Number(value || 0);
  if (number >= 1000000) return `${(number / 1000000).toFixed(1)}M`;
  if (number >= 1000) return `${Math.round(number / 1000)}K`;
  return String(number);
}

function formatPercent(value) {
  return `${Math.round(value * 1000) / 10}%`;
}

function shortModel(value = "") {
  const parts = String(value).split("/");
  return parts[parts.length - 1] || "—";
}

function shortId(value = "", length = 12) {
  const text = String(value);
  return text.length > length ? `${text.slice(0, length)}…` : text;
}

function emptyRow(columns, text) {
  return `<tr><td colspan="${columns}" class="empty-cell">${escapeHtml(text)}</td></tr>`;
}

function csv(value) {
  return value.split(",").map((item) => item.trim()).filter(Boolean);
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => switchView(tab.dataset.view));
});

document.querySelectorAll("[data-open-view]").forEach((button) => {
  button.addEventListener("click", () => switchView(button.dataset.openView));
});

byId("admin-key").value = state.key;
byId("connect").addEventListener("click", connect);
byId("refresh").addEventListener("click", () => loadDashboard());
byId("reload").addEventListener("click", async () => {
  try {
    await loadSettings();
    notice("设置已重新加载。");
  } catch (error) {
    notice(error.message, true);
  }
});
byId("settings-form").addEventListener("submit", saveSettings);
byId("auto-refresh").addEventListener("change", startPolling);
byId("node-filter").addEventListener("change", renderRequestTable);
byId("status-filter").addEventListener("change", renderRequestTable);
byId("client-status-filter").addEventListener("change", renderClients);
byId("create-client").addEventListener("click", () => openClientDialog());
byId("client-form").addEventListener("submit", saveClient);
byId("key-form").addEventListener("submit", createKey);
byId("copy-generated-key").addEventListener("click", async () => {
  const field = byId("generated-key");
  try {
    await navigator.clipboard.writeText(field.value);
  } catch (_error) {
    field.select();
    document.execCommand("copy");
  }
  notice("API Key 已复制。");
});
byId("secret-dialog").addEventListener("close", () => {
  byId("generated-key").value = "";
});
document.querySelectorAll("[data-close-dialog]").forEach((button) => {
  button.addEventListener("click", () => {
    const dialog = byId(button.dataset.closeDialog);
    if (dialog.id === "secret-dialog") byId("generated-key").value = "";
    dialog.close();
  });
});
byId("admin-key").addEventListener("keydown", (event) => {
  if (event.key === "Enter") connect();
});

if (state.key) connect();
