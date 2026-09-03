const state = {
  key: sessionStorage.getItem("ai-router-admin-key") || "",
  settings: null,
  dashboard: null,
  clients: [],
  routeGraph: null,
  routeTraces: [],
  routeTraceCursor: null,
  selectedTraceId: null,
  selectedTrace: null,
  selectedTraceAttempt: 1,
  selectedTraceNodeId: null,
  editingEndpointId: null,
  editingClientId: null,
  selectedClientId: null,
  view: "dashboard",
  timer: null,
};

const viewTitles = {
  dashboard: "运行总览",
  nodes: "端点管理",
  requests: "请求记录",
  audit: "路由审计",
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
  local_sufficient: "本地完整满足",
  remote_profile_fallback: "云端画像回退",
  configured_remote_order: "固定云端顺序",
  history_migration_required: "历史迁移受阻",
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
  interrupted: "已中断",
};

const reviewLabels = {
  unreviewed: "未审核",
  correct: "正确",
  incorrect: "错误",
  needs_review: "待复核",
};

const traceNodeLabels = {};
let traceGraphRenderKey = "";
let traceGraphRenderSequence = 0;
let traceGraphScale = 1;
let traceGraphNeedsInitialFocus = false;
let traceSearchTimer = null;

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
            <span class="table-secondary">${item.rpm_limit} RPM · ${item.max_parallel_requests} 并发 · ${item.allow_compaction ? "可压缩" : "不压缩"}</span>
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
  byId("client-allow-compaction").checked = Boolean(
    client?.allow_compaction,
  );
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
    allow_compaction: byId("client-allow-compaction").checked,
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
        <td>${statusBadge(
          item.status,
          item.status_code,
          null,
          Boolean(item.task || item.selected_model),
        )}</td>
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
  const enabled = endpoints.filter(({endpoint}) => endpoint.enabled).length;
  byId("endpoint-count").textContent =
    `${endpoints.filter((item) => item.status.healthy).length}/${endpoints.length} 健康 · ${enabled} 启用`;
  byId("endpoint-table").innerHTML = endpoints.map(({endpoint, status, management}) => `
    <tr>
      <td>${endpointStatusBadge(endpoint, status)}</td>
      <td><span class="node-label node-${escapeHtml(endpoint.node)}">${escapeHtml(endpoint.node.toUpperCase())}</span></td>
      <td>
        <strong class="table-primary">${escapeHtml(endpoint.public_model)}</strong>
        <span class="table-secondary">${escapeHtml(endpoint.id)}</span>
      </td>
      <td>${escapeHtml(endpoint.tier)}</td>
      <td>
        <strong class="table-primary">${formatTokens(status.eligible_context_tokens || endpoint.safe_context_tokens)}</strong>
        <span class="table-secondary">配置 ${formatTokens(endpoint.configured_context_tokens)}</span>
      </td>
      <td>${status.healthy ? `${Math.round(status.load_headroom * 100)}%` : "—"}</td>
      <td>${capabilitySummary(endpoint.capabilities, status.detail?.effective_modalities || endpoint.modalities)}</td>
      <td>
        <strong class="table-primary">${escapeHtml(endpoint.capabilities?.validation_status || "unverified")}</strong>
        <span class="table-secondary">${escapeHtml(endpoint.capabilities?.validated_at || "—")}</span>
      </td>
      <td>${endpoint.auto_candidate ? "是" : "否"}</td>
      <td>${endpointConfigStatus(management)}</td>
      <td>
        <div class="row-actions">
          <button class="secondary compact" type="button" data-endpoint-edit="${escapeHtml(endpoint.id)}">编辑</button>
          <button class="secondary compact" type="button" data-endpoint-auto="${escapeHtml(endpoint.id)}">
            ${endpoint.auto_candidate ? "退出自动" : "加入自动"}
          </button>
          <button class="secondary compact ${endpoint.enabled ? "danger-action" : ""}" type="button" data-endpoint-toggle="${escapeHtml(endpoint.id)}">
            ${endpoint.enabled ? "停用" : "启用"}
          </button>
        </div>
      </td>
    </tr>
  `).join("");
  bindEndpointActions();
}

function endpointStatusBadge(endpoint, status) {
  if (!endpoint.enabled) {
    return '<span class="badge danger"><i></i>已停用</span>';
  }
  return healthBadge(status.healthy);
}

function endpointConfigStatus(management = {}) {
  const draft = management.draft;
  if (draft) {
    const status = draft.validation?.status || "pending";
    const label = status === "passed"
      ? "草稿已验证"
      : status === "failed"
        ? "草稿失败"
        : "草稿待验证";
    const tone = status === "passed"
      ? "success"
      : status === "failed"
        ? "danger"
        : "warning";
    return `<span class="badge ${tone}"><i></i>${label}</span><span class="table-secondary">rev ${management.revision}</span>`;
  }
  return `
    <strong class="table-primary">${management.has_override ? "动态配置" : "注册表基线"}</strong>
    <span class="table-secondary">rev ${management.revision ?? 0}</span>
  `;
}

function bindEndpointActions() {
  document.querySelectorAll("[data-endpoint-edit]").forEach((button) => {
    button.addEventListener("click", () => {
      openEndpointDialog(button.dataset.endpointEdit);
    });
  });
  document.querySelectorAll("[data-endpoint-toggle]").forEach((button) => {
    button.addEventListener("click", () => {
      void runEndpointAction(button.dataset.endpointToggle, "enabled");
    });
  });
  document.querySelectorAll("[data-endpoint-auto]").forEach((button) => {
    button.addEventListener("click", () => {
      void runEndpointAction(button.dataset.endpointAuto, "auto");
    });
  });
}

function endpointItem(endpointId) {
  return (state.dashboard?.endpoints || []).find(
    ({endpoint}) => endpoint.id === endpointId,
  );
}

function openEndpointDialog(endpointId) {
  const item = endpointItem(endpointId);
  if (!item) return;
  state.editingEndpointId = endpointId;
  const {endpoint, management} = item;
  const baseline = management.baseline;
  const values = management.draft?.values || management.effective;
  const capabilities = values.capabilities || {};
  byId("endpoint-dialog-title").textContent = endpoint.public_model;
  byId("endpoint-readonly").innerHTML = [
    ["端点 ID", endpoint.id],
    ["节点", endpoint.node],
    ["后端类型", endpoint.backend_type],
    ["API 地址", endpoint.api_base],
  ].map(([label, value]) => `
    <div><span>${escapeHtml(label)}</span><strong title="${escapeHtml(value)}">${escapeHtml(value)}</strong></div>
  `).join("");
  setNumberField(
    "endpoint-safe-context",
    values.safe_context_tokens,
    baseline.safe_context_tokens,
  );
  setNumberField(
    "endpoint-configured-context",
    values.configured_context_tokens,
    baseline.configured_context_tokens,
  );
  setNumberField(
    "endpoint-max-concurrency",
    values.max_concurrency,
    baseline.max_concurrency,
  );
  byId("endpoint-responses").value = capabilities.responses || "none";
  byId("endpoint-tools").value = capabilities.tools || "none";
  byId("endpoint-chat").checked = Boolean(capabilities.chat);
  byId("endpoint-chat").disabled =
    !baseline.capabilities?.chat;
  byId("endpoint-streaming").checked = Boolean(capabilities.streaming);
  byId("endpoint-streaming").disabled =
    !baseline.capabilities?.streaming;
  byId("endpoint-tool-choice").checked = Boolean(capabilities.tool_choice);
  byId("endpoint-tool-choice").disabled =
    !baseline.capabilities?.tool_choice;
  restrictEndpointSelect(
    "endpoint-responses",
    ["none", baseline.capabilities?.responses || "none"],
  );
  const toolRank = {"none": 0, "single": 1, "parallel": 2};
  restrictEndpointSelect(
    "endpoint-tools",
    Object.keys(toolRank).filter(
      (value) => (
        toolRank[value]
        <= toolRank[baseline.capabilities?.tools || "none"]
      ),
    ),
  );
  renderEndpointOptions(
    "endpoint-tasks",
    baseline.tasks,
    values.tasks,
    endpointTaskLabel,
  );
  renderEndpointOptions(
    "endpoint-modalities",
    baseline.modalities,
    values.modalities,
    endpointModalityLabel,
  );
  renderEndpointOptions(
    "endpoint-tool-choice-modes",
    baseline.capabilities?.tool_choice_modes || [],
    capabilities.tool_choice_modes || [],
    (value) => value,
  );
  renderEndpointOptions(
    "endpoint-structured-output",
    baseline.capabilities?.structured_output || [],
    capabilities.structured_output || [],
    (value) => value,
  );
  const validation = management.draft?.validation;
  byId("endpoint-validation").innerHTML = validation
    ? `<strong>草稿验证：${escapeHtml(validation.status || "pending")}</strong><span>${escapeHtml((validation.errors || []).join(", ") || "没有错误")}</span>`
    : "<strong>当前没有草稿</strong><span>保存后先验证，再激活到生产路由。</span>";
  byId("endpoint-discard").disabled = !management.draft;
  byId("endpoint-validate").disabled = !management.draft;
  byId("endpoint-activate").disabled =
    validation?.status !== "passed";
  byId("endpoint-reset").disabled =
    !management.has_override && !management.draft;
  byId("endpoint-dialog").showModal();
}

function restrictEndpointSelect(id, allowed) {
  const values = new Set(allowed);
  [...byId(id).options].forEach((option) => {
    option.disabled = !values.has(option.value);
  });
}

function setNumberField(id, value, maximum) {
  const field = byId(id);
  field.value = Number(value);
  field.max = Number(maximum);
}

function renderEndpointOptions(
  targetId,
  available = [],
  selected = [],
  label = (value) => value,
) {
  const current = new Set(selected || []);
  byId(targetId).innerHTML = available.length
    ? available.map((value) => `
      <label>
        <input type="checkbox" value="${escapeHtml(value)}" ${current.has(value) ? "checked" : ""}>
        <span>${escapeHtml(label(value))}</span>
      </label>
    `).join("")
    : '<span class="table-secondary">注册表未声明可选项</span>';
}

function endpointTaskLabel(value) {
  return {
    general: "通用",
    code: "代码",
    batch: "批处理",
    "long-context": "长上下文",
  }[value] || value;
}

function endpointModalityLabel(value) {
  return {
    text: "文本",
    image: "图像",
    audio: "音频",
  }[value] || value;
}

function checkedValues(targetId) {
  return [...byId(targetId).querySelectorAll('input[type="checkbox"]:checked')]
    .map((input) => input.value);
}

function collectEndpointDraft() {
  return {
    safe_context_tokens: Number(byId("endpoint-safe-context").value),
    configured_context_tokens: Number(byId("endpoint-configured-context").value),
    max_concurrency: Number(byId("endpoint-max-concurrency").value),
    tasks: checkedValues("endpoint-tasks"),
    modalities: checkedValues("endpoint-modalities"),
    capabilities: {
      chat: byId("endpoint-chat").checked,
      responses: byId("endpoint-responses").value,
      tools: byId("endpoint-tools").value,
      tool_choice: byId("endpoint-tool-choice").checked,
      tool_choice_modes: checkedValues("endpoint-tool-choice-modes"),
      structured_output: checkedValues("endpoint-structured-output"),
      streaming: byId("endpoint-streaming").checked,
    },
  };
}

async function saveEndpointDraft(event) {
  event.preventDefault();
  const item = endpointItem(state.editingEndpointId);
  if (!item) return;
  try {
    await api(`/api/endpoints/${encodeURIComponent(state.editingEndpointId)}`, {
      method: "PATCH",
      body: JSON.stringify({
        expected_revision: item.management.revision,
        changes: collectEndpointDraft(),
      }),
    });
    await refreshEndpointDialog("端点草稿已保存。");
  } catch (error) {
    notice(error.message, true);
  }
}

async function runEndpointDraftCommand(command) {
  const item = endpointItem(state.editingEndpointId);
  if (!item) return;
  const endpointId = state.editingEndpointId;
  try {
    if (command === "activate" && !window.confirm("激活后会立即改变生产路由能力，确认继续？")) return;
    const options = command === "discard"
      ? {
          method: "DELETE",
          body: JSON.stringify({
            expected_revision: item.management.revision,
          }),
        }
      : {
          method: "POST",
          body: JSON.stringify({
            expected_revision: item.management.revision,
          }),
        };
    const path = command === "discard"
      ? `/api/endpoints/${encodeURIComponent(endpointId)}/draft`
      : `/api/endpoints/${encodeURIComponent(endpointId)}/${command}`;
    await api(path, options);
    await refreshEndpointDialog({
      validate: "端点草稿验证完成。",
      activate: "端点配置已激活。",
      discard: "端点草稿已放弃。",
    }[command]);
  } catch (error) {
    notice(error.message, true);
  }
}

async function resetEndpoint() {
  const item = endpointItem(state.editingEndpointId);
  if (!item || !window.confirm("恢复注册表基线会清除动态配置和草稿，确认继续？")) return;
  try {
    await api(`/api/endpoints/${encodeURIComponent(state.editingEndpointId)}/reset`, {
      method: "POST",
      body: JSON.stringify({
        expected_revision: item.management.revision,
      }),
    });
    await refreshEndpointDialog("端点已恢复注册表基线。");
  } catch (error) {
    notice(error.message, true);
  }
}

async function refreshEndpointDialog(message) {
  const endpointId = state.editingEndpointId;
  byId("endpoint-dialog").close();
  await loadDashboard(true);
  openEndpointDialog(endpointId);
  notice(message);
}

async function runEndpointAction(endpointId, kind) {
  const item = endpointItem(endpointId);
  if (!item) return;
  const enabled = item.endpoint.enabled;
  const auto = item.endpoint.auto_candidate;
  const action = kind === "enabled"
    ? (enabled ? "disable" : "enable")
    : (auto ? "auto-disable" : "auto-enable");
  if (
    action === "disable"
    && !window.confirm("停用后新请求将不再进入此端点，正在运行的请求会继续完成。确认停用？")
  ) return;
  try {
    await api(`/api/endpoints/${encodeURIComponent(endpointId)}/actions/${action}`, {
      method: "POST",
      body: JSON.stringify({
        expected_revision: item.management.revision,
      }),
    });
    await loadDashboard(true);
    notice(action === "disable"
      ? "端点已停用。"
      : action === "enable"
        ? "端点已启用。"
        : action === "auto-enable"
          ? "端点已加入自动路由。"
          : "端点已退出自动路由。");
  } catch (error) {
    notice(error.message, true);
  }
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
        <td>${statusBadge(
          item.status,
          item.status_code,
          null,
          Boolean(item.task || item.selected_model),
        )}</td>
        <td>${formatTime(item.timestamp)}</td>
        <td>
          <strong class="table-primary">${escapeHtml(shortModel(item.requested_model))}</strong>
          <span class="table-secondary request-id-full">${escapeHtml(item.request_id || "—")}</span>
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

async function loadRouteAudit(silent = false) {
  if (!state.key) return;
  try {
    await Promise.all([
      state.routeGraph ? Promise.resolve() : loadRouteGraph(),
      loadRouteTraces(true),
    ]);
    if (!silent) notice("");
  } catch (error) {
    if (!silent) notice(error.message, true);
  }
}

async function loadRouteGraph() {
  state.routeGraph = await api("/api/route-graph");
  Object.keys(traceNodeLabels).forEach((key) => delete traceNodeLabels[key]);
  (state.routeGraph.nodes || []).forEach((node) => {
    traceNodeLabels[node.id] = node.label;
  });
  traceGraphRenderKey = "";
}

function traceFilterQuery(cursor = null) {
  const query = new URLSearchParams({
    limit: "50",
    request_mode: byId("trace-mode-filter").value || "auto",
  });
  const values = {
    review_status: byId("trace-review-filter").value,
    client_id: byId("trace-client-filter").value,
    route_profile: byId("trace-task-filter").value,
    selected_model: byId("trace-model-filter").value,
    status: byId("trace-status-filter").value,
    search: byId("trace-search").value.trim(),
  };
  Object.entries(values).forEach(([key, value]) => {
    if (value) query.set(key, value);
  });
  if (cursor) query.set("cursor", cursor);
  return query.toString();
}

async function loadRouteTraces(silent = false, append = false) {
  if (!state.key) return;
  const listState = byId("trace-list-state");
  if (!silent) listState.textContent = "正在加载";
  const cursor = append ? state.routeTraceCursor : null;
  try {
    const payload = await api(`/api/route-traces?${traceFilterQuery(cursor)}`);
    state.routeTraces = append
      ? [...state.routeTraces, ...(payload.items || [])]
      : (payload.items || []);
    state.routeTraceCursor = payload.next_cursor || null;
    syncTraceFilterOptions();
    renderTraceList();
    byId("trace-load-more").hidden = !state.routeTraceCursor;
    listState.textContent = "";

    const selectedStillVisible = state.routeTraces.some(
      (item) => item.request_id === state.selectedTraceId,
    );
    if (!append && state.routeTraces.length && !selectedStillVisible) {
      await selectRouteTrace(state.routeTraces[0].request_id);
    } else if (!append && !state.routeTraces.length) {
      clearTraceDetail();
    } else if (
      state.selectedTrace?.status === "running" &&
      state.selectedTraceId
    ) {
      await selectRouteTrace(state.selectedTraceId, true);
    }
  } catch (error) {
    listState.textContent = "加载失败";
    if (!silent) notice(error.message, true);
    throw error;
  }
}

function syncTraceFilterOptions() {
  const clientValues = new Set(
    state.routeTraces.map((item) => item.client_id).filter(Boolean),
  );
  state.clients.forEach((item) => clientValues.add(item.id));
  const taskValues = new Set(
    state.routeTraces.map((item) => item.route_profile).filter(Boolean),
  );
  const modelValues = new Set(
    state.routeTraces.map((item) => item.selected_model).filter(Boolean),
  );
  updateTraceSelect(
    "trace-client-filter",
    [...clientValues].sort(),
    "全部客户端",
  );
  updateTraceSelect(
    "trace-task-filter",
    [...taskValues].sort(),
    "全部画像",
  );
  updateTraceSelect(
    "trace-model-filter",
    [...modelValues].sort(),
    "全部模型",
    shortModel,
  );
}

function updateTraceSelect(id, values, emptyLabel, format = (value) => value) {
  const select = byId(id);
  const current = select.value;
  if (current && !values.includes(current)) values.push(current);
  select.innerHTML =
    `<option value="">${escapeHtml(emptyLabel)}</option>` +
    values.map((value) => (
      `<option value="${escapeHtml(value)}">${escapeHtml(format(value))}</option>`
    )).join("");
  select.value = current;
}

function renderTraceList() {
  const items = state.routeTraces;
  byId("trace-count").textContent = `${items.length} 条`;
  const target = byId("trace-list");
  if (!items.length) {
    target.innerHTML = '<p class="empty">当前筛选条件下没有路由轨迹。</p>';
    return;
  }
  if (!byId("trace-group-conversation").checked) {
    target.innerHTML = items.map(traceListItem).join("");
  } else {
    const groups = new Map();
    items.forEach((item) => {
      const key = item.conversation_id || "无会话 ID";
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(item);
    });
    target.innerHTML = [...groups.entries()].map(([conversationId, values]) => `
      <div class="trace-conversation-heading" title="${escapeHtml(conversationId)}">
        会话 ${escapeHtml(shortId(conversationId, 26))} · ${values.length} 次请求
      </div>
      ${values.map(traceListItem).join("")}
    `).join("");
  }
  target.querySelectorAll("[data-trace-id]").forEach((button) => {
    button.addEventListener("click", () => {
      void selectRouteTrace(button.dataset.traceId);
    });
  });
}

function traceListItem(item) {
  const selected = item.request_id === state.selectedTraceId;
  const excerpt = item.excerpt?.text || "无文本摘要";
  return `
    <button class="trace-list-item${selected ? " selected" : ""}" type="button"
      data-trace-id="${escapeHtml(item.request_id)}">
      <span class="trace-list-primary">
        <strong>${escapeHtml(shortModel(item.selected_model || item.requested_model || "等待路由"))}</strong>
        ${statusBadge(
          item.status,
          item.status_code,
          item.error?.code,
          Boolean(item.task || item.selected_model),
        )}
      </span>
      <span class="trace-list-secondary">
        <span>${escapeHtml(item.route_profile || item.task || "等待画像")} · ${escapeHtml(item.client_id)}</span>
        <span>${formatTime(item.started_at)}</span>
      </span>
      <span class="trace-list-tertiary">
        <span title="${escapeHtml(excerpt)}">${escapeHtml(excerpt)}</span>
        ${reviewBadge(item.review_status)}
      </span>
    </button>`;
}

function reviewBadge(value) {
  const tone = value === "correct"
    ? "success"
    : value === "incorrect"
      ? "danger"
      : value === "needs_review"
        ? "warning"
        : "neutral";
  return `<span class="badge ${tone}"><i></i>${escapeHtml(reviewLabels[value] || value || "未审核")}</span>`;
}

async function selectRouteTrace(requestId, silent = false) {
  if (!requestId) return;
  const changed = state.selectedTraceId !== requestId;
  state.selectedTraceId = requestId;
  if (changed) {
    traceGraphScale = defaultTraceGraphScale();
    traceGraphNeedsInitialFocus = true;
  }
  renderTraceList();
  try {
    const payload = await api(`/api/route-traces/${encodeURIComponent(requestId)}`);
    state.selectedTrace = payload.trace;
    const attempts = state.selectedTrace.attempts || [];
    const attemptNumbers = attempts.map((item) => Number(item.number));
    if (!attemptNumbers.includes(state.selectedTraceAttempt)) {
      state.selectedTraceAttempt = Math.max(...attemptNumbers, 1);
    }
    state.selectedTraceNodeId = null;
    renderTraceDetail();
    if (traceEnteredRouting(state.selectedTrace)) {
      await ensureTraceGraphRendered();
    }
  } catch (error) {
    if (!silent) notice(error.message, true);
  }
}

function clearTraceDetail() {
  state.selectedTraceId = null;
  state.selectedTrace = null;
  state.selectedTraceNodeId = null;
  byId("trace-detail").hidden = true;
  byId("trace-detail-empty").hidden = false;
}

function renderTraceDetail() {
  const trace = state.selectedTrace;
  if (!trace) {
    clearTraceDetail();
    return;
  }
  byId("trace-detail-empty").hidden = true;
  byId("trace-detail").hidden = false;
  byId("trace-detail-title").textContent =
    shortModel(trace.selected_model || trace.requested_model || "路由轨迹");
  byId("trace-detail-status").innerHTML =
    statusBadge(
      trace.status,
      trace.status_code,
      trace.error?.code,
      traceEnteredRouting(trace),
    );
  byId("trace-detail-summary").textContent =
    trace.excerpt?.text || "此请求没有可显示的文本摘要。";
  byId("trace-detail-meta").innerHTML = [
    `请求 <code>${escapeHtml(shortId(trace.request_id, 20))}</code>`,
    `客户端 <strong>${escapeHtml(trace.client_id)}</strong>`,
    `画像 <strong>${escapeHtml(trace.task || "—")}</strong>`,
    `Token <strong>${formatTokens(trace.request?.prompt_tokens || 0)} + ${formatTokens(trace.request?.output_reserve_tokens || 0)}</strong>`,
    `策略 <code>${escapeHtml(trace.settings_fingerprint || "—")}</code>`,
    `注册表 <code>${escapeHtml(trace.registry_fingerprint || "—")}</code>`,
  ].map((item) => `<span>${item}</span>`).join("");
  renderTraceRoutingState(trace);
  renderTraceAttempts();
  renderTraceCurrentReview();
}

function renderTraceRoutingState(trace) {
  const target = byId("trace-routing-state");
  const beforeRouting = !traceEnteredRouting(trace);
  byId("trace-core-heading").hidden = beforeRouting;
  byId("trace-attempts").hidden = beforeRouting;
  byId("trace-graph-viewport").hidden = beforeRouting;
  if (beforeRouting) {
    byId("trace-node-inspector").hidden = true;
  }
  if (!beforeRouting) {
    target.hidden = true;
    target.textContent = "";
    return;
  }
  const client = state.clients.find(
    (item) => item.id === trace.client_id,
  );
  const allowedModels = trace.client_models?.length
    ? trace.client_models
    : (client?.models || []);
  const accessDenied = trace.error?.code === "invalid_api_key";
  target.className = "trace-routing-state warning";
  target.innerHTML = accessDenied
    ? `<strong>未进入智能路由</strong><span>客户端 ${escapeHtml(trace.client_id)} 未授权 ${escapeHtml(trace.requested_model)}；当前允许：${escapeHtml(allowedModels.join(", ") || "未配置")}</span>`
    : `<strong>未进入智能路由</strong><span>${escapeHtml(trace.error?.message || "请求在选模前被拒绝")}</span>`;
  target.hidden = false;
}

function traceEnteredRouting(trace) {
  return Boolean(trace?.task || trace?.selected_model);
}

function renderTraceAttempts() {
  const attempts = state.selectedTrace?.attempts || [];
  byId("trace-attempts").innerHTML = attempts.map((attempt) => `
    <button class="trace-attempt${Number(attempt.number) === state.selectedTraceAttempt ? " active" : ""}"
      type="button" data-trace-attempt="${Number(attempt.number)}">
      尝试 ${Number(attempt.number)}
      ${attempt.selection?.endpoint_id ? ` · ${escapeHtml(attempt.selection.endpoint_id)}` : ""}
    </button>
  `).join("");
  byId("trace-attempts").querySelectorAll("[data-trace-attempt]").forEach((button) => {
    button.addEventListener("click", () => {
      state.selectedTraceAttempt = Number(button.dataset.traceAttempt);
      state.selectedTraceNodeId = null;
      traceGraphScale = defaultTraceGraphScale();
      traceGraphNeedsInitialFocus = true;
      renderTraceAttempts();
      applyTraceGraphState();
      scheduleInitialTraceGraphFocus();
    });
  });
}

function selectedTraceAttempt() {
  return (state.selectedTrace?.attempts || []).find(
    (item) => Number(item.number) === state.selectedTraceAttempt,
  ) || null;
}

function initializeTraceMermaid() {
  if (!window.mermaid) return false;
  if (!window.mermaid.__aiRouterInitialized) {
    window.mermaid.initialize({
      startOnLoad: false,
      securityLevel: "antiscript",
      theme: "base",
      themeVariables: {
        background: "#090c10",
        primaryColor: "#10161d",
        primaryTextColor: "#f3f6f8",
        primaryBorderColor: "#52606d",
        lineColor: "#707b86",
        secondaryColor: "#151c24",
        tertiaryColor: "#0f1318",
        edgeLabelBackground: "#090c10",
        fontFamily: "Inter, system-ui, sans-serif",
        fontSize: "13px",
      },
      flowchart: {
        htmlLabels: true,
        curve: "basis",
        useMaxWidth: false,
        nodeSpacing: 34,
        rankSpacing: 52,
      },
    });
    window.mermaid.__aiRouterInitialized = true;
  }
  return true;
}

async function ensureTraceGraphRendered() {
  const graph = state.routeGraph;
  if (!graph?.mermaid) return;
  const target = byId("trace-graph");
  const renderKey = `${graph.graph_version}:${graph.mermaid}`;
  if (renderKey === traceGraphRenderKey && target.querySelector("svg")) {
    applyTraceGraphScale();
    applyTraceGraphState();
    scheduleInitialTraceGraphFocus();
    return;
  }
  if (!initializeTraceMermaid()) {
    renderTraceGraphFallback("Mermaid 渲染器未加载");
    return;
  }
  const sequence = ++traceGraphRenderSequence;
  target.classList.add("loading");
  target.innerHTML = '<div class="trace-graph-empty">正在生成路由流程图...</div>';
  try {
    const result = await window.mermaid.render(
      `route-audit-${Date.now()}-${sequence}`,
      graph.mermaid,
    );
    if (sequence !== traceGraphRenderSequence) return;
    target.classList.remove("loading");
    target.innerHTML = result?.svg || "";
    traceGraphRenderKey = renderKey;
    bindTraceGraph();
    applyTraceGraphScale();
    applyTraceGraphState();
    scheduleInitialTraceGraphFocus();
  } catch (error) {
    target.classList.remove("loading");
    renderTraceGraphFallback(`流程图渲染失败：${error.message}`);
  }
}

function traceGraphNodeElement(nodeId) {
  if (!nodeId) return null;
  return Array.from(
    byId("trace-graph").querySelectorAll("g.node, [data-mermaid-node-id]"),
  ).find((node) => {
    if (node.dataset.mermaidNodeId === nodeId) return true;
    const id = node.getAttribute("id") || "";
    return id === nodeId
      || id.startsWith(`flowchart-${nodeId}-`)
      || id.includes(`-${nodeId}-`);
  }) || null;
}

function bindTraceGraph() {
  (state.routeGraph?.nodes || []).forEach((flowNode) => {
    const graphNode = traceGraphNodeElement(flowNode.id);
    if (!graphNode) return;
    graphNode.dataset.traceNodeId = flowNode.id;
    graphNode.setAttribute("role", "button");
    graphNode.setAttribute("tabindex", "0");
    graphNode.setAttribute("aria-label", flowNode.label);
    const select = () => selectTraceGraphNode(flowNode.id);
    graphNode.addEventListener("click", select);
    graphNode.addEventListener("keydown", (event) => {
      if (event.key !== "Enter" && event.key !== " ") return;
      event.preventDefault();
      select();
    });
  });
  const edgeElements = Array.from(
    byId("trace-graph").querySelectorAll(
      "g.edgePath, g.edgePaths > path.flowchart-link",
    ),
  );
  edgeElements.forEach((element, index) => {
    const edge = state.routeGraph?.edges?.[index];
    if (!edge) return;
    element.dataset.traceEdgeId = edge.id;
  });
  const edgeLabels = Array.from(
    byId("trace-graph").querySelectorAll("g.edgeLabel"),
  );
  const labeledEdges = (state.routeGraph?.edges || []).filter(
    (edge) => edge.label,
  );
  edgeLabels.forEach((element, index) => {
    const edge = labeledEdges[index];
    if (!edge) return;
    element.dataset.traceEdgeId = edge.id;
  });
}

function traceGraphSteps() {
  const attempt = selectedTraceAttempt();
  const graphNodeIds = new Set(
    (state.routeGraph?.nodes || []).map((item) => item.id),
  );
  const steps = (attempt?.steps || []).filter(
    (step) => (
      graphNodeIds.has(step.node_id)
      && step.path !== false
    ),
  );
  const hasFinalSelection = steps.some(
    (step) => step.node_id === "route_selected",
  );
  const capacityAcquired = (attempt?.steps || []).some(
    (step) => (
      step.node_id === "capacity_check"
      && ["selected", "passed"].includes(step.status)
      && ["capacity_acquired", "lease_acquired"].includes(step.reason)
    ),
  );
  if (
    graphNodeIds.has("route_selected")
    && !hasFinalSelection
    && capacityAcquired
    && attempt?.selection
  ) {
    steps.push({
      sequence: Number.MAX_SAFE_INTEGER,
      timestamp: state.selectedTrace?.completed_at,
      node_id: "route_selected",
      status: "selected",
      branch: "available",
      reason: attempt.selection.reason || "route_selected",
      path: true,
      evidence: {
        selected_model: attempt.selection.selected_model,
        endpoint_id: attempt.selection.endpoint_id,
        deployment_id: attempt.selection.deployment_id,
        affinity: attempt.selection.affinity,
      },
    });
  }
  return steps;
}

function defaultTraceGraphScale() {
  return 1;
}

function defaultTraceFocusNodeId(steps = traceGraphSteps()) {
  if (!steps.length) return null;
  if (state.selectedTrace?.status === "running") {
    return steps[steps.length - 1]?.node_id || null;
  }
  const requestedModel = state.selectedTrace?.requested_model;
  const preferred = requestedModel === "auto"
    ? [
        "local_sufficiency",
        "remote_expert_dispatch",
        "candidate_scope",
        "route_selected",
      ]
    : [
        "explicit_model",
        "explicit_selection",
        "candidate_scope",
        "route_selected",
      ];
  return preferred.find(
    (nodeId) => steps.some((step) => step.node_id === nodeId),
  ) || steps[steps.length - 1]?.node_id || null;
}

function traceDisplayStatus(step, steps = traceGraphSteps()) {
  if (
    step.status === "selected"
    && step.node_id !== "route_selected"
  ) {
    return "passed";
  }
  if (
    step.status === "failed"
    && ["explicit_model", "conversation_affinity"].includes(step.node_id)
  ) {
    return "evaluated";
  }
  if (
    step.node_id === "candidate_scope"
    && Array.isArray(step.evidence?.candidates)
    && step.evidence.candidates.length
    && !step.evidence.candidates.some((item) => !item.rejection_reason)
  ) {
    return "blocked";
  }
  if (
    step.status === "failed"
    && step.node_id === "capacity_check"
    && steps.some((item) => (
      item.node_id === "retry_decision"
      && ["passed", "evaluated"].includes(item.status)
    ))
  ) {
    return "rejected";
  }
  if (
    step.status === "failed"
    && step.node_id === "retry_decision"
  ) {
    return "blocked";
  }
  return step.status;
}

function applyTraceGraphState() {
  const steps = traceGraphSteps();
  const latest = new Map();
  steps.forEach((step) => latest.set(step.node_id, step));
  const currentStep = steps[steps.length - 1] || null;
  if (!state.selectedTraceNodeId && currentStep) {
    state.selectedTraceNodeId =
      defaultTraceFocusNodeId(steps) || currentStep.node_id;
  }

  (state.routeGraph?.nodes || []).forEach((flowNode) => {
    const graphNode = traceGraphNodeElement(flowNode.id);
    if (!graphNode) return;
    const step = latest.get(flowNode.id);
    graphNode.classList.remove(
      "trace-unvisited",
      "trace-status-passed",
      "trace-status-evaluated",
      "trace-status-rejected",
      "trace-status-blocked",
      "trace-status-failed",
      "trace-status-error",
      "trace-status-selected",
      "trace-status-running",
      "trace-node-selected",
    );
    if (!step) {
      graphNode.classList.add("trace-unvisited");
    } else {
      graphNode.classList.add(
        `trace-status-${traceDisplayStatus(step, steps)}`,
      );
    }
    graphNode.classList.toggle(
      "trace-node-selected",
      flowNode.id === state.selectedTraceNodeId,
    );
  });

  const visiblePath = [];
  steps.forEach((step) => {
    const previous = visiblePath[visiblePath.length - 1];
    if (previous?.node_id === step.node_id) {
      visiblePath[visiblePath.length - 1] = step;
    } else {
      visiblePath.push(step);
    }
  });
  const transitions = visiblePath.slice(1).map((step, index) => ({
    from: visiblePath[index].node_id,
    to: step.node_id,
    branch: step.branch,
  }));
  (state.routeGraph?.edges || []).forEach((edge) => {
    const active = transitions.some((transition) => (
      transition.from === edge.from
      && transition.to === edge.to
      && (!edge.branch || transition.branch === edge.branch)
    ));
    byId("trace-graph")
      .querySelectorAll(`[data-trace-edge-id="${cssEscape(edge.id)}"]`)
      .forEach((element) => {
        element.classList.toggle("trace-edge-active", active);
        element.classList.toggle("trace-unvisited", !active);
      });
  });
  renderTraceNodeInspector();
}

function selectTraceGraphNode(nodeId) {
  state.selectedTraceNodeId = nodeId;
  applyTraceGraphState();
  centerTraceGraphNode(nodeId);
}

function renderTraceNodeInspector() {
  const inspector = byId("trace-node-inspector");
  const steps = traceGraphSteps();
  const step = [...steps].reverse().find(
    (item) => item.node_id === state.selectedTraceNodeId,
  );
  if (!step) {
    inspector.hidden = true;
    return;
  }
  inspector.hidden = false;
  byId("trace-node-title").textContent =
    traceNodeLabels[step.node_id] || step.node_id;
  byId("trace-node-status").innerHTML = traceStepBadge(
    traceDisplayStatus(step, steps),
  );
  byId("trace-node-reason").textContent =
    `${step.reason || "无附加原因"}${step.branch ? ` · 分支 ${step.branch}` : ""}`;
  byId("trace-node-evidence").innerHTML =
    renderTraceEvidence(step.evidence || {});
}

function traceStepBadge(status) {
  const labels = {
    passed: "通过",
    evaluated: "已判断",
    rejected: "候选跳过",
    blocked: "路由阻断",
    failed: "未满足",
    error: "异常",
    selected: "已选择",
    running: "执行中",
    skipped: "已跳过",
  };
  const tone = status === "passed"
    ? "success"
    : status === "selected" || status === "running"
      ? "running"
      : status === "evaluated"
        ? "neutral"
        : ["rejected", "skipped"].includes(status)
        ? "warning"
        : "danger";
  return `<span class="badge ${tone}"><i></i>${escapeHtml(labels[status] || status)}</span>`;
}

function renderTraceEvidence(evidence) {
  const candidates = Array.isArray(evidence.candidates)
    ? evidence.candidates
    : null;
  const entries = Object.entries(evidence).filter(
    ([key]) => key !== "candidates",
  );
  const grid = entries.length
    ? `<div class="trace-evidence-grid">${entries.map(([key, value]) => `
        <div class="trace-evidence-item">
          <span>${escapeHtml(key.replaceAll("_", " "))}</span>
          <strong>${escapeHtml(traceEvidenceValue(value))}</strong>
        </div>
      `).join("")}</div>`
    : '<p class="empty">该节点没有附加证据。</p>';
  return candidates ? `${grid}${renderTraceCandidates(candidates)}` : grid;
}

function traceEvidenceValue(value) {
  if (value == null || value === "") return "—";
  if (typeof value === "boolean") return value ? "是" : "否";
  if (Array.isArray(value)) {
    if (!value.length) return "—";
    return value.map((item) => (
      typeof item === "object" ? JSON.stringify(item) : String(item)
    )).join(", ");
  }
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function renderTraceCandidates(candidates) {
  if (!candidates.length) return "";
  return `
    <table class="trace-candidate-table">
      <thead>
        <tr>
          <th>端点</th>
          <th>模型 / 节点</th>
          <th>类型</th>
          <th>健康 / 容量</th>
          <th>上下文</th>
          <th>画像质量</th>
          <th>结果</th>
        </tr>
      </thead>
      <tbody>
        ${candidates.map((item) => `
          <tr>
            <td><code>${escapeHtml(item.endpoint_id)}</code></td>
            <td>
              <strong class="table-primary">${escapeHtml(shortModel(item.model))}</strong>
              <span class="table-secondary">${escapeHtml(item.node)}</span>
            </td>
            <td>${item.cloud ? "云端" : "本地"}</td>
            <td>
              <strong class="table-primary">${item.healthy && item.fresh ? "健康" : "不可用"} · ${Math.round(Number(item.load_headroom || 0) * 100)}%</strong>
              <span class="table-secondary">${Number(item.physical_deployments?.available || 0)}/${Number(item.physical_deployments?.total || 0)} 物理部署空闲</span>
            </td>
            <td>${formatTokens(item.required_context_tokens)} / ${formatTokens(item.safe_context_tokens)}</td>
            <td>
              <strong class="table-primary">${Number(item.quality_score || 0)}</strong>
              <span class="table-secondary">${escapeHtml(item.quality_status || "unverified")}</span>
            </td>
            <td>${item.rejection_reason
              ? `<span class="badge danger"><i></i>${escapeHtml(rejectionReasonLabel(item.rejection_reason))}</span>`
              : '<span class="badge success"><i></i>合格</span>'}</td>
          </tr>
        `).join("")}
      </tbody>
    </table>`;
}

function renderTraceGraphFallback(message) {
  const steps = traceGraphSteps();
  byId("trace-graph").innerHTML = `
    <div class="trace-graph-empty">
      <div>
        <strong>${escapeHtml(message)}</strong>
        <p>${steps.map((step) => (
          `${escapeHtml(traceNodeLabels[step.node_id] || step.node_id)}：${escapeHtml(step.reason || step.status)}`
        )).join("<br>") || "没有可显示的轨迹步骤"}</p>
      </div>
    </div>`;
}

function renderTraceCurrentReview() {
  const review = state.selectedTrace?.current_review;
  byId("trace-current-review").textContent = review
    ? `${reviewLabels[review.verdict] || review.verdict} · ${formatTime(review.created_at)}`
    : "尚未审核";
  byId("trace-review-verdict").value = review?.verdict || "";
  byId("trace-expected-task").value = review?.expected_task || "";
  byId("trace-expected-model").value = review?.expected_model || "";
  byId("trace-review-note").value = review?.note || "";
}

async function submitTraceReview(event) {
  event.preventDefault();
  if (!state.selectedTraceId) return;
  const button = byId("trace-review-submit");
  button.disabled = true;
  try {
    await api(
      `/api/route-traces/${encodeURIComponent(state.selectedTraceId)}/reviews`,
      {
        method: "POST",
        body: JSON.stringify({
          verdict: byId("trace-review-verdict").value,
          expected_task: byId("trace-expected-task").value || null,
          expected_model: byId("trace-expected-model").value.trim() || null,
          note: byId("trace-review-note").value.trim() || null,
        }),
      },
    );
    notice("路由审核已保存。");
    await selectRouteTrace(state.selectedTraceId, true);
    await loadRouteTraces(true);
  } catch (error) {
    notice(error.message, true);
  } finally {
    button.disabled = false;
  }
}

function applyTraceGraphScale() {
  const svg = byId("trace-graph").querySelector("svg");
  if (!svg) return;
  const viewBoxWidth = Number(svg.viewBox?.baseVal?.width) || 1440;
  const vertical = state.routeGraph?.mermaid?.includes("flowchart TD");
  const minimumWidth = vertical ? 320 : 1800;
  svg.style.width =
    `${Math.max(minimumWidth, viewBoxWidth * traceGraphScale)}px`;
  svg.style.maxWidth = "none";
  svg.style.height = "auto";
}

function centerTraceGraphNode(nodeId, smooth = true) {
  const target = traceGraphNodeElement(nodeId);
  const viewport = byId("trace-graph-viewport");
  if (!target || !viewport) return;
  const targetRect = target.getBoundingClientRect();
  const viewportRect = viewport.getBoundingClientRect();
  viewport.scrollTo({
    left: Math.max(
      0,
      viewport.scrollLeft
        + targetRect.left
        - viewportRect.left
        - (viewport.clientWidth - targetRect.width) / 2,
    ),
    top: Math.max(
      0,
      viewport.scrollTop
        + targetRect.top
        - viewportRect.top
        - (viewport.clientHeight - targetRect.height) / 2,
    ),
    behavior: smooth ? "smooth" : "auto",
  });
}

function scheduleInitialTraceGraphFocus() {
  if (!traceGraphNeedsInitialFocus) return;
  traceGraphNeedsInitialFocus = false;
  window.requestAnimationFrame(() => {
    window.requestAnimationFrame(() => {
      centerTraceGraphNode(
        state.selectedTraceNodeId || defaultTraceFocusNodeId(),
        false,
      );
    });
  });
}

function fitTraceGraph(smooth = true) {
  const svg = byId("trace-graph").querySelector("svg");
  const viewport = byId("trace-graph-viewport");
  if (!svg || !viewport) return;
  const viewBoxWidth = Number(svg.viewBox?.baseVal?.width) || 1440;
  traceGraphScale = Math.max(
    0.45,
    Math.min(1.4, (viewport.clientWidth - 52) / viewBoxWidth),
  );
  applyTraceGraphScale();
  viewport.scrollTo({
    left: Math.max(0, (viewport.scrollWidth - viewport.clientWidth) / 2),
    top: 0,
    behavior: smooth ? "smooth" : "auto",
  });
}

function focusTraceCurrentNode() {
  const steps = traceGraphSteps();
  const nodeId = steps[steps.length - 1]?.node_id;
  if (nodeId) selectTraceGraphNode(nodeId);
}

function cssEscape(value) {
  if (window.CSS?.escape) return window.CSS.escape(value);
  return String(value).replaceAll('"', '\\"');
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
  byId("identity-enabled").checked = Boolean(value("identity.enabled", false));
  byId("identity-model-id").value = value(
    "identity.public_model_id",
    "siyuan/auto",
  );
  byId("identity-name-zh").value = value(
    "identity.display_name_zh",
    "思源",
  );
  byId("identity-name-en").value = value(
    "identity.display_name_en",
    "SIYUAN",
  );
  byId("identity-provider").value = value(
    "identity.provider_name",
    "SIYUAN",
  );
  byId("identity-description").value = value(
    "identity.description",
    "由思源智能路由服务提供的统一 AI 助手。",
  );
  byId("identity-response").value = value(
    "identity.identity_response",
    "我是思源（SIYUAN），由思源智能路由服务提供的统一 AI 助手。",
  );
  byId("cloud-enabled").checked = Boolean(value("cloud.enabled", false));
  byId("cloud-auto").checked = Boolean(value("cloud.auto_escalate", false));
  byId("cloud-budget").value = value("cloud.monthly_budget", 0);
  byId("cloud-providers").value = value("cloud.allowed_providers", []).join(", ");
  byId("cloud-models").value = value("cloud.allowed_models", []).join(", ");
  byId("evaluator-enabled").checked = Boolean(value("evaluator.enabled", false));
  byId("evaluator-model").value = value("evaluator.model_id");
  byId("evaluator-confidence").value = value("evaluator.confidence_threshold", 0.85);
  byId("compaction-enabled").checked = Boolean(value("compaction.enabled", true));
  byId("compaction-mode").value = value(
    "compaction.mode",
    "explicit_only",
  );
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
  byId("routing-strategy").value = value(
    "routing.strategy",
    "legacy_v1",
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
    identity: {
      ...state.settings.identity,
      enabled: byId("identity-enabled").checked,
      public_model_id: byId("identity-model-id").value.trim(),
      display_name_zh: byId("identity-name-zh").value.trim(),
      display_name_en: byId("identity-name-en").value.trim(),
      provider_name: byId("identity-provider").value.trim(),
      description: byId("identity-description").value.trim(),
      identity_response: byId("identity-response").value.trim(),
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
      mode: byId("compaction-mode").value,
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
      strategy: byId("routing-strategy").value,
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
  document.querySelector("main")?.classList.toggle(
    "audit-main",
    view === "audit",
  );
  document.querySelectorAll(".tab").forEach((item) => {
    item.classList.toggle("active", item.dataset.view === view);
  });
  document.querySelectorAll(".view").forEach((item) => {
    item.classList.toggle("active", item.id === `${view}-view`);
  });
  byId("view-title").textContent = viewTitles[view];
  if (view === "requests") renderRequestTable();
  if (view === "audit") void loadRouteAudit();
  if (view === "clients") loadClients(true);
}

function startPolling() {
  clearInterval(state.timer);
  if (!byId("auto-refresh").checked) return;
  state.timer = setInterval(() => {
    if (!document.hidden && state.key) {
      loadDashboard(true);
      if (state.view === "clients") loadClients(true);
      if (state.view === "audit") loadRouteTraces(true);
    }
  }, 5000);
}

function healthBadge(healthy) {
  return `<span class="badge ${healthy ? "success" : "danger"}"><i></i>${healthy ? "健康" : "不可用"}</span>`;
}

function statusBadge(
  status,
  code,
  errorCode = null,
  enteredRouting = false,
) {
  let tone = status === "succeeded"
    ? "success"
    : status === "running"
      ? "running"
      : status === "stale" || status === "interrupted"
        ? "warning"
        : "danger";
  let label = statusLabels[status] || status;
  if (status === "failed") {
    if (
      [401, 403].includes(Number(code))
      || errorCode === "invalid_api_key"
    ) {
      tone = "warning";
      label = "权限未授权";
    } else if (Number(code) === 429) {
      tone = "warning";
      label = "容量或限流";
    } else if ([400, 409, 413, 422].includes(Number(code))) {
      if (enteredRouting) {
        label = "无可用路由";
      } else {
        tone = "warning";
        label = "请求未进入路由";
      }
    } else {
      label = `${code || ""} 路由失败`.trim();
    }
  }
  return `<span class="badge ${tone}"><i></i>${escapeHtml(label)}</span>`;
}

function rejectionReasonLabel(value) {
  return {
    excluded: "本轮已排除",
    disabled: "端点未启用",
    auto_disabled: "未加入 Auto 候选",
    cooldown: "故障冷却中",
    unhealthy_or_stale: "健康状态不可用",
    physical_deployment: "无合格物理部署",
    modality: "模态不匹配",
    capability: "协议能力不匹配",
    task: "任务画像不匹配",
    context: "上下文不足",
    cloud_disabled: "云端已关闭",
    cloud_auto_disabled: "云端自动升级关闭",
    cloud_model_not_allowed: "云模型未授权",
    cloud_provider_not_allowed: "Provider 未授权",
    cloud_budget: "云端预算不足",
    cloud_pricing: "缺少云端价格",
    tier_downgrade: "不允许会话降级",
    tier: "模型层级不足",
  }[value] || value;
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
[
  "trace-mode-filter",
  "trace-review-filter",
  "trace-client-filter",
  "trace-task-filter",
  "trace-model-filter",
  "trace-status-filter",
].forEach((id) => {
  byId(id).addEventListener("change", () => void loadRouteTraces());
});
byId("trace-search").addEventListener("input", () => {
  clearTimeout(traceSearchTimer);
  traceSearchTimer = setTimeout(() => void loadRouteTraces(), 350);
});
byId("trace-group-conversation").addEventListener("change", renderTraceList);
byId("trace-filter-reset").addEventListener("click", () => {
  byId("trace-mode-filter").value = "auto";
  byId("trace-review-filter").value = "unreviewed";
  byId("trace-client-filter").value = "";
  byId("trace-task-filter").value = "";
  byId("trace-model-filter").value = "";
  byId("trace-status-filter").value = "";
  byId("trace-search").value = "";
  byId("trace-group-conversation").checked = false;
  void loadRouteTraces();
});
byId("trace-load-more").addEventListener("click", () => {
  void loadRouteTraces(false, true);
});
byId("trace-review-form").addEventListener("submit", submitTraceReview);
byId("trace-pan-left").addEventListener("click", () => {
  const viewport = byId("trace-graph-viewport");
  viewport.scrollBy({
    left: -Math.max(280, viewport.clientWidth * 0.7),
    behavior: "smooth",
  });
});
byId("trace-pan-right").addEventListener("click", () => {
  const viewport = byId("trace-graph-viewport");
  viewport.scrollBy({
    left: Math.max(280, viewport.clientWidth * 0.7),
    behavior: "smooth",
  });
});
byId("trace-zoom-out").addEventListener("click", () => {
  traceGraphScale = Math.max(0.45, traceGraphScale - 0.1);
  applyTraceGraphScale();
});
byId("trace-zoom-in").addEventListener("click", () => {
  traceGraphScale = Math.min(1.8, traceGraphScale + 0.1);
  applyTraceGraphScale();
});
byId("trace-fit").addEventListener("click", () => fitTraceGraph());
byId("trace-current").addEventListener("click", focusTraceCurrentNode);
byId("trace-fullscreen").addEventListener("click", async () => {
  const viewport = byId("trace-graph-viewport");
  try {
    if (document.fullscreenElement === viewport) {
      await document.exitFullscreen();
    } else {
      await viewport.requestFullscreen();
      fitTraceGraph(false);
    }
  } catch (error) {
    notice(`无法进入全屏：${error.message}`, true);
  }
});
byId("client-status-filter").addEventListener("change", renderClients);
byId("create-client").addEventListener("click", () => openClientDialog());
byId("client-form").addEventListener("submit", saveClient);
byId("key-form").addEventListener("submit", createKey);
byId("endpoint-form").addEventListener("submit", saveEndpointDraft);
byId("endpoint-validate").addEventListener("click", () => {
  void runEndpointDraftCommand("validate");
});
byId("endpoint-activate").addEventListener("click", () => {
  void runEndpointDraftCommand("activate");
});
byId("endpoint-discard").addEventListener("click", () => {
  void runEndpointDraftCommand("discard");
});
byId("endpoint-reset").addEventListener("click", () => {
  void resetEndpoint();
});
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
