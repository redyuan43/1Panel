const state = {
  key: sessionStorage.getItem("ai-router-admin-key") || "",
  settings: null,
  policy: null,
  directivePool: null,
  dashboard: null,
  clients: [],
  routeGraph: null,
  routeGraphMode: "simple",
  routeGraphCache: {},
  routeTraces: [],
  routeTraceCursor: null,
  requestTraces: [],
  requestTraceTotal: 0,
  requestTracePage: 1,
  requestTraceCursors: [null],
  requestTraceNextCursor: null,
  requestConversationSummaries: new Map(),
  requestExpandedConversations: new Set(),
  requestExpandedRequests: new Set(),
  requestConversationPages: new Map(),
  selectedTraceId: null,
  selectedTrace: null,
  routeDiagnosis: null,
  conversationControl: null,
  cacheDeployments: null,
  selectedCacheDeploymentId: null,
  selectedCacheLayerId: null,
  selectedCacheDeploymentTab: "declared",
  cacheDeploymentAnomaliesOnly: false,
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
  "cache-deployments": "缓存部署",
  settings: "策略设置",
};

const FALLBACK_PROFILES = [
  ["general", "通用对话"],
  ["agent_text", "Agent 文本"],
  ["code", "代码"],
  ["complex_code", "复杂代码"],
  ["multimodal", "多模态"],
  ["multimodal_complex_code", "多模态复杂代码"],
];

const PROMPT_DIRECTIVES = [
  ["rilun", "Sol"],
  ["beichen", "Astra"],
  ["qinglan", "DeepSeek V4 Pro"],
  ["yuheng", "GLM 5.3"],
  ["reset", "恢复常规模式"],
];

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
  conversation_affinity: "沿用会话路由",
  capacity_spillover: "容量分流",
  affinity_spillover: "亲和迁移",
  cloud_capacity_fallback: "云端容量兜底",
  local_sufficient: "本地完整满足",
  local_pool_spread: "本地会话分散",
  local_pool_faster_first_output: "预计更快首个输出",
  remote_profile_fallback: "云端画像回退",
  configured_remote_order: "固定云端顺序",
  history_migration_required: "历史迁移受阻",
  local_priority: "本地优先",
  cloud_priority: "云端优先",
  balanced_score: "均衡评分",
  preferred_tier: "高难任务升级",
  logical_affinity: "逻辑亲和",
  tier_requirement: "层级要求",
  affinity_same_tier_fallback: "同层级会话回退",
};

const affinityLabels = {
  new: "新链路",
  hit: "沿用会话设备",
  "logical-hit": "沿用会话模型",
  explicit: "显式",
  migrated: "已迁移",
  "physical-failover": "同模型迁移",
  "cache-reset": "缓存代际重置",
};

const lineageRelationLabels = {
  new: "新链路",
  continuation: "父分支续接",
  compaction_reset: "压缩重置",
  legacy: "旧记录",
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
let traceSelectionSequence = 0;
let traceGraphRenderSequence = 0;
let traceGraphScale = 1;
let traceGraphNeedsInitialFocus = false;
let traceSearchTimer = null;
let requestSearchTimer = null;
let requestTraceLoadSequence = 0;
const conversationTurnLoads = new Map();
const REQUEST_PAGE_SIZE = 30;

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
    const error = new Error(payload.error?.message || `HTTP ${response.status}`);
    error.status = response.status;
    throw error;
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
    renderRemoteFallbackOrder();
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
  renderLmcacheRuntimeStatus();
  syncRequestNodeOptions();
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
            <span class="table-secondary">${escapeHtml(item.id)} · ${escapeHtml(item.source)} · ${item.disclosure_mode === "public" ? "客户脱敏" : "内部详细"}</span>
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

function publicIdentityModelId() {
  return state.settings?.identity?.public_model_id || "siyuan/auto";
}

function renderClientModels(selected = ["auto"], disclosureMode = "internal") {
  const target = byId("client-models");
  if (disclosureMode === "public") {
    const publicModel = publicIdentityModelId();
    target.innerHTML = `
      <label>
        <input type="checkbox" data-client-model="${escapeHtml(publicModel)}" checked disabled>
        <span>${escapeHtml(publicModel)}</span>
      </label>
    `;
    return;
  }
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
  byId("client-disclosure-mode").value = (
    client?.disclosure_mode || (client ? "internal" : "public")
  );
  byId("client-enabled").checked = client?.enabled ?? true;
  byId("client-allow-compaction").checked = Boolean(
    client?.allow_compaction,
  );
  renderClientModels(
    client?.models || [publicIdentityModelId()],
    byId("client-disclosure-mode").value,
  );
  byId("client-dialog").showModal();
}

function collectClient() {
  const selected = [...document.querySelectorAll("[data-client-model]:checked")]
    .map((input) => input.dataset.clientModel);
  return {
    id: byId("client-id").value.trim(),
    name: byId("client-name").value.trim(),
    enabled: byId("client-enabled").checked,
    models: byId("client-disclosure-mode").value === "public"
      ? [publicIdentityModelId()]
      : (selected.includes("*") ? ["*"] : selected),
    rpm_limit: Number(byId("client-rpm").value),
    tpm_limit: Number(byId("client-tpm").value),
    max_parallel_requests: Number(byId("client-parallel").value),
    allow_compaction: byId("client-allow-compaction").checked,
    disclosure_mode: byId("client-disclosure-mode").value,
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
  byId("endpoint-table").innerHTML = endpoints.map(({endpoint, status, management, cache_declaration}) => `
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
      <td>${endpointCacheStatus(endpoint, status, cache_declaration)}</td>
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

function endpointCacheStatus(endpoint, status, cache_declaration) {
  const declared = cache_declaration || {};
  if (declared.strategy === "native_memory") {
    const layer = (declared.layers || []).find(item => item.medium === "process");
    return `<strong class="table-primary">内存前缀缓存 · 配置已核对</strong><span class="table-secondary">${escapeHtml(layer?.capacity || "数量未采集")} · 进程内 · 命中率未采集</span>`;
  }
  const detail = status.detail || {};
  const queries = Number(detail.prefix_cache_queries || 0);
  const hits = Number(detail.prefix_cache_hits || 0);
  const apcRatio = queries > 0 ? hits / queries : null;
  if (endpoint.backend_type !== "vllm") {
    return '<span class="table-secondary">—</span>';
  }
  const gpu = `<strong class="table-primary">GPU ${escapeHtml(formatCacheHit(hits, apcRatio))}</strong>`;
  if (declared.strategy === "native_disk") {
    const layer = (declared.layers || []).find(item => item.medium === "disk");
    return `${gpu}<span class="table-secondary">原生磁盘缓存 · 已配置 ${escapeHtml(layer?.capacity || "容量未知")} · 占用未采集</span>`;
  }
  const lmcache = detail.lmcache || {};
  if (lmcache.supported !== true) {
    return `${gpu}<span class="table-secondary">外部缓存未采集 · LMCache 不适用</span>`;
  }
  const desired = endpoint.id === "ai-qwen38-27b"
    ? state.settings?.lmcache
    : null;
  const active = Boolean(lmcache.connector_active);
  const restartRequired = desired
    ? Boolean(desired.enabled) !== active
    : false;
  const lmcacheRatio = Number(lmcache.lookup_requested_tokens || 0) > 0
    ? Number(lmcache.lookup_hit_tokens || 0)
      / Number(lmcache.lookup_requested_tokens)
    : null;
  const memory = Number(lmcache.memory_total_bytes || 0) > 0
    ? `${formatBytes(lmcache.memory_used_bytes)} / ${formatBytes(lmcache.memory_total_bytes)}`
    : lmcache.memory_total_bytes === 0 ? "未分配" : "容量未采集";
  const runtimeLabel = restartRequired
    ? "待重启"
    : active
      ? lmcache.healthy ? "运行中" : "故障"
      : "未启用";
  return `
    <strong class="table-primary">GPU ${escapeHtml(formatCacheHit(hits, apcRatio))}</strong>
    <span class="table-secondary">
      DRAM ${escapeHtml(formatCacheHit(lmcache.lookup_hit_tokens, lmcacheRatio))}
      · ${escapeHtml(runtimeLabel)} · ${escapeHtml(memory)}
    </span>`;
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

const cacheDeploymentStateLabels = {
  healthy: "健康",
  warning: "配置差异",
  unavailable: "不可用",
  unknown: "缓存健康未确认",
};

const cacheDeploymentDriftLabels = {
  target_missing: "注册目标不存在",
  endpoint_unhealthy: "端点健康检查失败",
  worker_missing: "Fleet 未报告该 Worker",
  worker_not_ready: "Worker 不可调度",
  planned_not_deployed: "持久化缓存尚未部署",
  lmcache_restart_required: "LMCache 目标与 Connector 不一致",
  lmcache_unhealthy: "LMCache Connector 已连接但服务异常",
  aggregate_telemetry_unavailable: "仅有请求级证据",
};

const cacheDeploymentPersistenceLabels = {
  "vllm-process": "vLLM 进程内",
  "lmcache-process": "LMCache 进程保持期间",
  "same-vllm-process": "同一 vLLM 进程",
  "llama-process": "llama.cpp 进程内",
  "local-disk": "跨后端和网关重启",
  "planned-local-disk": "计划跨进程恢复",
  none: "不持久化",
};

const cacheDeploymentStrategyLabels = {
  lmcache_dram: "vLLM + LMCache 内存缓存",
  native_disk: "vLLM 原生磁盘缓存",
  gateway_snapshot: "llama.cpp 网关磁盘快照",
  native_memory: "llama.cpp 内存前缀缓存",
};

const cacheDeploymentLifecycleLabels = {
  deployed: "已部署",
  planned: "待部署",
};

const cacheDeploymentValidationLabels = {
  validated: "已验证",
  partial: "部分验证",
  planned: "待验证",
};

const cacheDeploymentTelemetryLabels = {
  installed: "已安装",
  config_only: "仅核对配置，缓存计数未采集",
  partial: "仅请求级",
  planned: "待部署",
  native: "原生指标",
  request: "请求级遥测",
  aggregate: "聚合遥测",
};

async function loadCacheDeployments(silent = false) {
  if (!state.key) return;
  if (!silent) byId("refresh").disabled = true;
  try {
    state.cacheDeployments = await api("/api/cache/deployments");
    const visible = cacheDeploymentItems();
    if (
      !state.selectedCacheDeploymentId
      || !visible.some(
        (item) => item.id === state.selectedCacheDeploymentId,
      )
    ) {
      state.selectedCacheDeploymentId =
        visible[0]?.id || state.cacheDeployments.deployments?.[0]?.id;
      state.selectedCacheLayerId = null;
    }
    renderCacheDeployments();
    byId("last-updated").textContent =
      `更新于 ${formatTime(state.cacheDeployments.generated_at)}`;
    setConnected(true);
    if (!silent) notice("");
  } catch (error) {
    setConnected(false);
    if (!silent) notice(error.message, true);
  } finally {
    byId("refresh").disabled = false;
  }
}

function cacheDeploymentItems() {
  const items = state.cacheDeployments?.deployments || [];
  if (!state.cacheDeploymentAnomaliesOnly) return items;
  return items.filter((item) =>
    item.drift?.some(({severity}) =>
      severity === "warning" || severity === "critical"
    )
  );
}

function renderCacheDeployments() {
  const payload = state.cacheDeployments;
  if (!payload) return;
  renderCacheDeploymentSummary(payload.summary || {});
  renderCacheDeploymentTable(cacheDeploymentItems());
  renderCacheDeploymentDetail();
}

function renderCacheDeploymentSummary(summary) {
  const metrics = [
    ["纳管设备", summary.total || 0, "AI、Edge、AMD、NX3、NX4、AGX", ""],
    ["已部署", summary.deployed || 0, `${summary.planned || 0} 项仍在规划`, "good"],
    ["配置差异", summary.drifted || 0, "声明配置与实时状态比较", summary.drifted ? "warn" : "good"],
    ["缓存架构", summary.strategies || 0, "内存前缀、DRAM、原生磁盘、网关快照", "live"],
  ];
  byId("cache-deployment-summary").innerHTML = metrics.map(
    ([label, value, hint, tone]) => `
      <div class="metric ${tone}">
        <span>${escapeHtml(label)}</span>
        <strong>${escapeHtml(value)}</strong>
        <small>${escapeHtml(hint)}</small>
      </div>
    `,
  ).join("");
}

function cacheDeploymentBadge(item) {
  const tone = item.state?.tone || "neutral";
  const label = item.state?.label === "Planned"
    ? "待部署"
    : cacheDeploymentStateLabels[item.state?.code]
      || item.state?.label
      || "未知";
  return `<span class="badge ${escapeHtml(tone)}"><i></i>${escapeHtml(label)}</span>`;
}

function cacheDeploymentCapacity(item) {
  const layers = item.declared?.layers || [];
  const durable = layers.find(({medium}) =>
    medium === "dram" || medium === "disk"
  );
  return durable
    ? {
        value: durable.capacity,
        detail: durable.backend,
      }
    : {value: layers.find(layer => layer.medium === "process")?.capacity || "GPU KV Cache", detail: "配置容量 · 进程内"};
}

function cacheDeploymentObservedSummary(item) {
  const observed = item.observed || {};
  const worker = observed.worker;
  const lmcache = observed.cache?.lmcache || {};
  if (item.id === "ai-v100-tp2") {
    const desired = Boolean(item.desired?.lmcache?.enabled);
    const service = observed.cache_health?.healthy == null
      ? "LMCache 健康未确认"
      : !desired
        ? "LMCache 目标关闭"
        : lmcache.healthy
          ? "LMCache 健康"
          : "LMCache 状态不可用";
    const connector = lmcache.connector_active
      ? `Connector ${lmcache.registered_count ?? "—"}/${lmcache.expected_registrations ?? "—"}`
      : `Connector 未连接 ${lmcache.registered_count ?? "—"}/${lmcache.expected_registrations ?? "—"}`;
    return [service, connector];
  }
  if (item.id === "edge-qwen38-flash") {
    const hits = observed.cache?.external?.hit_tokens;
    return [
      observed.status?.healthy ? "vLLM 健康" : "vLLM 不可用",
      hits == null ? "磁盘命中指标不可用" : `外部命中 ${formatTokens(hits)} Token`,
    ];
  }
  if (item.declared?.strategy === "native_memory") {
    return [observed.status?.healthy ? "llama.cpp 健康" : "llama.cpp 不可用", "内存缓存配置已核对 · 命中率未采集"];
  }
  if (worker) {
    return [
      worker.ready ? "Worker 可调度" : "Worker 不可调度",
      `Checkpoint ${worker.context_checkpoints ?? "—"} · ${worker.state || "未知"}`,
    ];
  }
  return [
    observed.status?.healthy ? "端点健康" : "端点不可用",
    "目标 Worker 未报告",
  ];
}

function cacheDeploymentPersistence(item) {
  const layers = item.declared?.layers || [];
  const durable = layers.find(({medium}) =>
    medium === "dram" || medium === "disk"
  );
  return durable
    ? cacheDeploymentPersistenceLabels[durable.persistence]
      || durable.persistence
    : "进程内";
}

function cacheDeploymentDrift(item) {
  const actionable = (item.drift || []).filter(({severity}) =>
    severity === "warning" || severity === "critical"
  );
  const informational = (item.drift || []).filter(
    ({severity}) => severity === "info",
  );
  if (actionable.length) {
    return {
      primary: cacheDeploymentDriftLabels[actionable[0].code]
        || actionable[0].message,
      secondary: actionable.length > 1
        ? `另有 ${actionable.length - 1} 项`
        : "",
    };
  }
  if (informational.length) {
    return {
      primary: cacheDeploymentDriftLabels[informational[0].code]
        || informational[0].message,
      secondary: "不影响当前调度",
    };
  }
  return {primary: "无", secondary: "配置与运行一致"};
}

function cacheDeploymentActionLabel(action) {
  return {
    "lmcache-settings": "配置 LMCache",
    disable: "停用",
    enable: "启用",
    "auto-disable": "退出自动",
    "auto-enable": "加入自动",
  }[action.id] || action.label;
}

function cacheDeploymentActions(item) {
  const actions = item.management?.actions || [];
  if (!actions.length) {
    return '<span class="table-secondary">只读纳管</span>';
  }
  return `
    <div class="row-actions">
      ${actions.map((action) => `
        <button
          type="button"
          class="secondary compact ${action.id === "disable" ? "danger-action" : ""}"
          data-cache-deployment-action="${escapeHtml(item.id)}"
          data-cache-action-id="${escapeHtml(action.id)}"
          data-cache-revision="${escapeHtml(item.management?.revision ?? "")}"
        >${escapeHtml(cacheDeploymentActionLabel(action))}</button>
      `).join("")}
    </div>
  `;
}

function renderCacheDeploymentTable(items) {
  byId("cache-deployment-count").textContent =
    `${items.length} / ${state.cacheDeployments?.summary?.total || 0} 台设备`;
  byId("cache-deployment-table").innerHTML = items.length
    ? items.map((item) => {
        const endpoint = item.observed?.endpoint || {};
        const capacity = cacheDeploymentCapacity(item);
        const observed = cacheDeploymentObservedSummary(item);
        const drift = cacheDeploymentDrift(item);
        const selected = item.id === state.selectedCacheDeploymentId;
        return `
          <tr
            class="cache-deployment-row ${selected ? "selected" : ""}"
            data-cache-deployment-id="${escapeHtml(item.id)}"
          >
            <td>${cacheDeploymentBadge(item)}</td>
            <td>
              <strong class="table-primary">${escapeHtml(item.title)}</strong>
              <span class="table-secondary">${escapeHtml(item.target?.worker_id || item.target?.endpoint_id || "—")}</span>
            </td>
            <td>
              <strong class="table-primary cache-deployment-flow-label">${escapeHtml(item.declared?.flow_label || "—")}</strong>
              <span class="table-secondary">${escapeHtml(endpoint.model || item.node)}</span>
            </td>
            <td>
              <strong class="table-primary">${escapeHtml(capacity.value)}</strong>
              <span class="table-secondary">${escapeHtml(capacity.detail)}</span>
            </td>
            <td>
              <strong class="table-primary">${escapeHtml(observed[0])}</strong>
              <span class="table-secondary">${escapeHtml(observed[1])}</span>
            </td>
            <td>${escapeHtml(cacheDeploymentPersistence(item))}</td>
            <td>
              <strong class="table-primary">${escapeHtml(drift.primary)}</strong>
              <span class="table-secondary">${escapeHtml(drift.secondary)}</span>
            </td>
            <td>${cacheDeploymentActions(item)}</td>
          </tr>
        `;
      }).join("")
    : emptyRow(8, "当前筛选下没有异常设备。");
  bindCacheDeploymentRows();
}

function bindCacheDeploymentRows() {
  document.querySelectorAll("[data-cache-deployment-id]").forEach((row) => {
    row.addEventListener("click", (event) => {
      if (event.target.closest("button")) return;
      state.selectedCacheDeploymentId = row.dataset.cacheDeploymentId;
      state.selectedCacheLayerId = null;
      renderCacheDeployments();
    });
  });
  document.querySelectorAll("[data-cache-deployment-action]").forEach((button) => {
    button.addEventListener("click", () => {
      void runCacheDeploymentAction(
        button.dataset.cacheDeploymentAction,
        button.dataset.cacheActionId,
        button.dataset.cacheRevision,
      );
    });
  });
}

async function runCacheDeploymentAction(deploymentId, actionId, renderedRevision) {
  const item = (state.cacheDeployments?.deployments || []).find(
    ({id}) => id === deploymentId,
  );
  if (!item) return;
  state.selectedCacheDeploymentId = deploymentId;
  if (actionId === "lmcache-settings") {
    switchView("settings");
    byId("lmcache-enabled")?.scrollIntoView({
      behavior: "smooth",
      block: "center",
    });
    notice("LMCache 修改通过策略草稿、验证和激活流程保存；运行服务仍需受控重启。");
    return;
  }
  const endpointId = item.management?.endpoint_id;
  const action = item.management?.actions?.find(({id, kind}) =>
    id === actionId && kind === "endpoint");
  const revision = Number(renderedRevision);
  if (!endpointId || !action || !["enable", "disable", "auto-enable", "auto-disable"].includes(actionId)) return;
  if (renderedRevision == null || renderedRevision === "" || !Number.isSafeInteger(revision) || revision < 0) {
    notice("操作版本缺失，请刷新后重试。", true);
    return;
  }
  if (actionId === "disable" && !window.confirm("停用后新请求将不再进入此端点，正在运行的请求会继续完成。确认停用？")) return;
  try {
    // Submit the displayed action and its revision; never toggle another snapshot.
    await api(`/api/endpoints/${encodeURIComponent(endpointId)}/actions/${actionId}`, {
      method: "POST",
      body: JSON.stringify({expected_revision: revision}),
    });
    await Promise.all([loadDashboard(true), loadCacheDeployments(true)]);
    notice(`${cacheDeploymentActionLabel(action)}操作已完成。`);
  } catch (error) {
    if (error.status === 409) {
      await Promise.allSettled([loadDashboard(true), loadCacheDeployments(true)]);
      notice("配置版本已变化，已刷新。请核对后重新操作；未自动重试。", true);
    } else {
      notice(error.message, true);
    }
  }
}

function selectedCacheDeployment() {
  return (state.cacheDeployments?.deployments || []).find(
    ({id}) => id === state.selectedCacheDeploymentId,
  );
}

function renderCacheDeploymentDetail() {
  const item = selectedCacheDeployment();
  const detail = byId("cache-deployment-detail");
  if (!item) {
    detail.hidden = true;
    return;
  }
  detail.hidden = false;
  const endpoint = item.observed?.endpoint || {};
  byId("cache-deployment-detail-title").textContent =
    `${item.title} · ${item.target?.worker_id || item.target?.endpoint_id}`;
  byId("cache-deployment-detail-subtitle").textContent = [
    endpoint.backend_type || item.declared?.strategy,
    endpoint.safe_context_tokens
      ? `安全上下文 ${formatTokens(endpoint.safe_context_tokens)}`
      : null,
    endpoint.max_concurrency
      ? `并发 ${endpoint.max_concurrency}`
      : null,
  ].filter(Boolean).join(" · ");
  byId("cache-deployment-detail-state").innerHTML =
    cacheDeploymentBadge(item);
  renderCacheDeploymentPipeline(item);
  renderCacheDeploymentInspector(item);
}

function renderCacheDeploymentPipeline(item) {
  const layers = item.declared?.layers || [];
  if (!state.selectedCacheLayerId) {
    state.selectedCacheLayerId = layers[0]?.id || null;
  }
  byId("cache-deployment-pipeline").innerHTML = layers.map(
    (layer, index) => `
      ${index ? '<span class="cache-deployment-arrow" aria-hidden="true">→</span>' : ""}
      <button
        type="button"
        class="cache-deployment-layer ${layer.id === state.selectedCacheLayerId ? "selected" : ""}"
        data-cache-layer-id="${escapeHtml(layer.id)}"
      >
        <small>${escapeHtml(layer.medium)}</small>
        <strong>${escapeHtml(layer.label)}</strong>
        <span>${escapeHtml(layer.capacity)}</span>
      </button>
    `,
  ).join("");
  byId("cache-deployment-pipeline")
    .querySelectorAll("[data-cache-layer-id]")
    .forEach((button) => {
      button.addEventListener("click", () => {
        state.selectedCacheLayerId = button.dataset.cacheLayerId;
        renderCacheDeploymentPipeline(item);
      });
    });
  const layer = layers.find(({id}) =>
    id === state.selectedCacheLayerId
  ) || layers[0];
  byId("cache-deployment-layer-detail").innerHTML = layer
    ? `
      <strong>${escapeHtml(layer.label)} · ${escapeHtml(cacheDeploymentPersistenceLabels[layer.persistence] || layer.persistence)}</strong>
      <p>${escapeHtml(layer.description)}</p>
    `
    : "";
}

function renderCacheDeploymentInspector(item) {
  document.querySelectorAll("[data-cache-deployment-tab]").forEach((button) => {
    button.classList.toggle(
      "active",
      button.dataset.cacheDeploymentTab
        === state.selectedCacheDeploymentTab,
    );
  });
  const target = byId("cache-deployment-tab-content");
  const tab = state.selectedCacheDeploymentTab;
  if (tab === "declared") {
    target.innerHTML = cacheDeploymentFacts([
      ["策略", cacheDeploymentStrategyLabels[item.declared?.strategy] || item.declared?.strategy],
      ["生命周期", cacheDeploymentLifecycleLabels[item.declared?.lifecycle] || item.declared?.lifecycle],
      ["缓存流程", item.declared?.flow_label],
      ["遥测", `${cacheDeploymentTelemetryLabels[item.declared?.telemetry?.mode] || item.declared?.telemetry?.mode || "—"} · ${cacheDeploymentTelemetryLabels[item.declared?.telemetry?.status] || item.declared?.telemetry?.status || "—"}`],
    ]);
  } else if (tab === "observed") {
    const endpoint = item.observed?.endpoint || {};
    const worker = item.observed?.worker;
    const lmcache = item.observed?.cache?.lmcache || {};
    target.innerHTML = cacheDeploymentFacts([
      ["目标状态", item.observed?.target_found ? "已发现" : "未发现"],
      ["运行健康", item.observed?.runtime_healthy ? "健康" : "异常"],
      ["端点", endpoint.id || "—"],
      ["模型", endpoint.model || "—"],
      ["Worker", worker ? `${worker.worker_id} · ${worker.state}` : "不适用"],
      ["缓存健康", item.observed?.cache_health?.healthy === true ? "服务与 Connector 已验证" : item.observed?.cache_health?.healthy === false ? "缓存层未就绪" : "未确认（缺少实时证据或证据已过期）"],
      ["LMCache", lmcache.supported === true
        ? item.observed?.cache_health?.healthy == null ? "实时健康未确认" : `${lmcache.healthy ? "服务健康" : "服务异常"} · ${lmcache.connector_active ? "Connector 已连接" : "Connector 未连接"}`
        : "不适用"],
    ]);
  } else if (tab === "validated") {
    target.innerHTML = cacheDeploymentFacts([
      ["状态", cacheDeploymentValidationLabels[item.validated?.status] || item.validated?.status],
      ["验证日期", item.validated?.validated_at || "未验证"],
      ["证据摘要", item.validated?.summary],
    ]);
  } else if (tab === "services") {
    const services = (item.declared?.services || []).map(
      (service) => [
        service.manager,
        `${service.unit}${service.state ? ` · ${service.state}` : ""}`,
      ],
    );
    const paths = Object.entries(item.declared?.paths || {}).map(
      ([key, value]) => [`路径 · ${key}`, value],
    );
    target.innerHTML = cacheDeploymentFacts([...services, ...paths]);
  } else {
    const limitations = (item.declared?.limitations || []).map(
      (value, index) => [`限制 ${index + 1}`, value],
    );
    const drift = (item.drift || []).map(
      (value) => [
        `${value.severity} · ${value.code}`,
        cacheDeploymentDriftLabels[value.code] || value.message,
      ],
    );
    target.innerHTML = cacheDeploymentFacts([
      ...limitations,
      ...drift,
      ["网页能力", "不提供远程重启、缓存清除或远程构建"],
    ]);
  }
}

function cacheDeploymentFacts(items) {
  return `
    <dl class="cache-deployment-facts">
      ${items.map(([label, value]) => `
        <div>
          <dt>${escapeHtml(label || "—")}</dt>
          <dd>${escapeHtml(value ?? "—")}</dd>
        </div>
      `).join("")}
    </dl>
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

function syncRequestNodeOptions() {
  const select = byId("node-filter");
  if (!select) return;
  const current = select.value;
  const nodes = [...new Set(
    (state.dashboard?.endpoints || [])
      .map((item) => item.node)
      .filter(Boolean),
  )].sort();
  select.innerHTML =
    '<option value="">全部节点</option>' +
    nodes.map((item) => (
      `<option value="${escapeHtml(item)}">${escapeHtml(item.toUpperCase())}</option>`
    )).join("");
  select.value = current;
}

function requestFilterQuery({
  cursor = null,
  conversationId = null,
  limit = REQUEST_PAGE_SIZE,
} = {}) {
  const query = new URLSearchParams({
    limit: String(limit),
    request_mode: "all",
  });
  if (conversationId) {
    query.set("conversation_id", conversationId);
  } else {
    const values = {
      node: byId("node-filter").value,
      status: byId("status-filter").value,
      search: byId("request-search").value.trim(),
    };
    Object.entries(values).forEach(([key, value]) => {
      if (value) query.set(key, value);
    });
  }
  if (cursor) query.set("cursor", cursor);
  return query.toString();
}

async function loadRequestTraces(silent = false) {
  if (!state.key) return;
  const sequence = ++requestTraceLoadSequence;
  const listState = byId("request-list-state");
  if (!silent) {
    listState.textContent = "正在加载";
    byId("refresh").disabled = true;
  }
  const cursor = state.requestTraceCursors[
    state.requestTracePage - 1
  ] || null;
  try {
    const payload = await api(
      `/api/route-traces?${requestFilterQuery({cursor})}`,
    );
    if (sequence !== requestTraceLoadSequence) return;
    state.requestTraces = payload.items || [];
    state.requestTraceTotal = Number(payload.total_count || 0);
    state.requestTraceNextCursor = payload.next_cursor || null;
    state.requestConversationSummaries = new Map(
      (payload.conversation_summaries || []).map((item) => [
        item.conversation_id,
        item,
      ]),
    );
    renderRequestTable();
    listState.textContent = "";
    byId("last-updated").textContent =
      `更新于 ${formatTime(Date.now() / 1000)}`;
    setConnected(true);
    if (!silent) notice("");
  } catch (error) {
    if (sequence !== requestTraceLoadSequence) return;
    listState.textContent = "加载失败";
    if (!silent) notice(error.message, true);
  } finally {
    if (!silent) byId("refresh").disabled = false;
  }
}

function resetRequestPagination() {
  state.requestTracePage = 1;
  state.requestTraceCursors = [null];
  state.requestTraceNextCursor = null;
  state.requestExpandedConversations.clear();
  state.requestExpandedRequests.clear();
  state.requestConversationPages.clear();
}

function requestGroups() {
  const groups = [];
  const conversations = new Map();
  state.requestTraces.forEach((item) => {
    if (!item.conversation_id) {
      groups.push({
        key: `request:${item.request_id}`,
        conversationId: null,
        items: [item],
      });
      return;
    }
    let group = conversations.get(item.conversation_id);
    if (!group) {
      group = {
        key: `conversation:${item.conversation_id}`,
        conversationId: item.conversation_id,
        items: [],
      };
      conversations.set(item.conversation_id, group);
      groups.push(group);
    }
    group.items.push(item);
  });
  return groups;
}

function renderRequestTable() {
  const groups = requestGroups();
  const totalPages = Math.max(
    1,
    Math.ceil(state.requestTraceTotal / REQUEST_PAGE_SIZE),
  );
  byId("request-count").textContent =
    `匹配 ${state.requestTraceTotal} 条`;
  byId("request-page-label").textContent =
    `第 ${state.requestTracePage} / ${totalPages} 页`;
  byId("request-prev").disabled = state.requestTracePage <= 1;
  byId("request-next").disabled = !state.requestTraceNextCursor;
  byId("request-table").innerHTML = groups.length
    ? groups.map(requestGroupRows).join("")
    : emptyRow(8, "没有符合筛选条件的请求");
  bindRequestTableActions();
}

function requestGroupRows(group) {
  const latest = group.items[0];
  if (!group.conversationId) {
    return requestStandaloneRow(latest);
  }
  const summary = state.requestConversationSummaries.get(
    group.conversationId,
  ) || {};
  const expanded = state.requestExpandedConversations.has(
    group.conversationId,
  );
  const excerpt = summary.latest_excerpt?.text
    || latest.excerpt?.text
    || "无文本摘要";
  const latestStatus = summary.latest_status || latest.status;
  const latestStatusCode = summary.latest_status_code
    ?? latest.status_code;
  const selectedModel = summary.latest_selected_model
    || latest.selected_model
    || summary.latest_requested_model
    || latest.requested_model;
  const node = summary.latest_node || latest.node || "—";
  const deployment = summary.latest_deployment_id
    || latest.deployment_id
    || "—";
  const total = Number(summary.request_count || group.items.length);
  return `
    <tr class="request-conversation-row${expanded ? " expanded" : ""}">
      <td>
        <button class="request-expand-button" type="button"
          data-request-conversation="${escapeHtml(group.conversationId)}"
          aria-expanded="${expanded ? "true" : "false"}"
          title="${expanded ? "折叠会话" : "展开会话"}">
          ${expanded ? "⌄" : "›"}
        </button>
      </td>
      <td>${statusBadge(
        latestStatus,
        latestStatusCode,
        latest.error?.code,
        Boolean(latest.task || selectedModel),
      )}</td>
      <td>
        <code class="request-id-full">${escapeHtml(group.conversationId)}</code>
        <span class="table-secondary">本页 ${group.items.length} 次请求</span>
      </td>
      <td>
        <strong class="table-primary">${group.items.length} / ${total}</strong>
        <span class="table-secondary">本页 / 留存</span>
      </td>
      <td>
        <strong class="table-primary">${escapeHtml(shortModel(selectedModel))}</strong>
        <span class="table-secondary">${escapeHtml(latest.client_id || "—")}</span>
      </td>
      <td>
        <strong class="table-primary">${escapeHtml(String(node).toUpperCase())}</strong>
        <span class="table-secondary" title="${escapeHtml(deployment)}">${escapeHtml(shortId(deployment, 28))}</span>
      </td>
      <td>
        <span class="request-summary" title="${escapeHtml(excerpt)}">${escapeHtml(graphemeExcerpt(excerpt))}</span>
      </td>
      <td>${formatTime(summary.latest_started_at || latest.started_at)}</td>
    </tr>
    ${expanded ? requestConversationDetailRow(group.conversationId) : ""}
  `;
}

function requestStandaloneRow(item) {
  const excerpt = item.excerpt?.text || "无文本摘要";
  const expanded = state.requestExpandedRequests.has(item.request_id);
  return `
    <tr class="request-standalone-row${expanded ? " expanded" : ""}">
      <td>
        <button class="request-expand-button" type="button"
          data-request-standalone="${escapeHtml(item.request_id)}"
          aria-expanded="${expanded ? "true" : "false"}"
          title="${expanded ? "折叠请求" : "展开请求"}">
          ${expanded ? "⌄" : "›"}
        </button>
      </td>
      <td>${statusBadge(
        item.status,
        item.status_code,
        item.error?.code,
        Boolean(item.task || item.selected_model),
      )}</td>
      <td>
        <code class="request-id-full">${escapeHtml(item.request_id)}</code>
        <span class="table-secondary">无会话 ID</span>
      </td>
      <td><strong class="table-primary">1 / 1</strong></td>
      <td>
        <strong class="table-primary">${escapeHtml(shortModel(item.selected_model || item.requested_model))}</strong>
        <span class="table-secondary">${escapeHtml(item.client_id || "—")}</span>
      </td>
      <td>
        <strong class="table-primary">${escapeHtml(String(item.node || "—").toUpperCase())}</strong>
        <span class="table-secondary">${escapeHtml(shortId(item.deployment_id || "—", 28))}</span>
      </td>
      <td><span class="request-summary" title="${escapeHtml(excerpt)}">${escapeHtml(graphemeExcerpt(excerpt))}</span></td>
      <td>${formatTime(item.started_at)}</td>
    </tr>
    ${expanded ? requestStandaloneDetailRow(item) : ""}
  `;
}

function requestRoundTable(items) {
  return `
    <div class="request-round-table-wrap">
      <table class="request-round-table">
        <thead>
          <tr>
            <th>状态</th>
            <th>开始时间</th>
            <th>请求 / 分支 ID</th>
            <th>请求模型</th>
            <th>实际路由</th>
            <th>部署</th>
            <th>任务</th>
            <th>协议</th>
            <th>路由延续</th>
            <th>网络/容量尝试</th>
            <th>容量等待</th>
            <th>Token</th>
            <th>缓存命中</th>
            <th>耗时</th>
          </tr>
        </thead>
        <tbody>${items.map(requestRoundRow).join("")}</tbody>
      </table>
    </div>
  `;
}

function requestConversationDetailRow(conversationId) {
  const page = state.requestConversationPages.get(conversationId);
  let content = '<div class="request-conversation-loading">正在加载会话轮次...</div>';
  if (page?.error) {
    content = `<div class="request-conversation-loading error">${escapeHtml(page.error)}</div>`;
  } else if (page?.items) {
    content = `
      ${requestRoundTable(page.items)}
      <div class="request-conversation-footer">
        <span>已加载 ${page.items.length} / ${page.totalCount} 轮</span>
        <button class="secondary compact" type="button"
          data-request-earlier="${escapeHtml(conversationId)}"
          ${page.nextCursor ? "" : "disabled"}>
          ${page.nextCursor ? "加载更早记录" : "已显示全部记录"}
        </button>
      </div>
    `;
  }
  return `
    <tr class="request-conversation-detail-row">
      <td colspan="8">
        <div class="request-conversation-detail"
          data-request-conversation-detail="${escapeHtml(conversationId)}">
          ${content}
        </div>
      </td>
    </tr>
  `;
}

function requestStandaloneDetailRow(item) {
  return `
    <tr class="request-conversation-detail-row">
      <td colspan="8">
        <div class="request-conversation-detail">
          ${requestRoundTable([item])}
        </div>
      </td>
    </tr>
  `;
}

function requestRoundRow(item) {
  return `
    <tr data-request-round-id="${escapeHtml(item.request_id)}">
      <td>${statusBadge(
        item.status,
        item.status_code,
        item.error?.code,
        Boolean(item.task || item.selected_model),
      )}</td>
      <td>${formatTime(item.started_at)}</td>
      <td>
        <code class="request-id-full">${escapeHtml(item.request_id || "—")}</code>
        <button class="privacy-trace-link" type="button" data-privacy-trace="${escapeHtml(item.request_id)}" title="查看隐私审核与纠偏">${privacyBadge(item.privacy_assessment)}</button>
        <span class="table-secondary request-id-full">分支 ${escapeHtml(item.branch_id || "—")}</span>
        <span class="table-secondary request-id-full">父分支 ${escapeHtml(item.parent_branch_id || "—")}</span>
        <span class="table-secondary">${requestLineageLabel(item)}</span>
      </td>
      <td><strong class="table-primary">${escapeHtml(shortModel(item.requested_model))}</strong></td>
      <td>
        <strong class="table-primary">${escapeHtml(shortModel(item.selected_model))}</strong>
        <span class="table-secondary">${escapeHtml(reasonLabel(item.reason))}</span>
      </td>
      <td>
        <code title="${escapeHtml(item.deployment_id || "")}">${escapeHtml(shortId(item.deployment_id || "—", 28))}</code>
        <span class="table-secondary">${escapeHtml(item.deployment_profile_id || "—")}${item.image_resizes ? ` · 缩图 ${item.image_resizes}` : ""}</span>
      </td>
      <td>${escapeHtml(item.task || "—")}</td>
      <td>${escapeHtml(protocolLabel(item.protocol, item.native_or_adapter))}</td>
      <td>${escapeHtml(affinityLabel(item.affinity))}</td>
      <td>${item.attempts || 1} / ${item.capacity_attempts || 1}</td>
      <td>${item.queue_wait_ms == null ? "—" : formatDuration(item.queue_wait_ms)}</td>
      <td>
        <strong class="table-primary">${formatTokens(item.prompt_tokens || 0)}</strong>
        <span class="table-secondary">输入 ${formatTokens(item.input_tokens || 0)} · 输出 ${formatTokens(item.output_tokens || 0)}</span>
      </td>
      <td>${formatCacheHit(item.cached_prompt_tokens, item.cache_hit_ratio)}</td>
      <td>${item.latency_ms == null ? formatRelative(item.started_at) : formatDuration(item.latency_ms)}</td>
    </tr>
  `;
}

function requestLineageLabel(item) {
  if (item.context_compacted) {
    return `${item.context_compaction_source === "client" ? "客户端" : "Router"}压缩`;
  }
  return lineageRelationLabels[item.lineage_relation]
    || (item.conversation_mode === "stateful" ? "显式链路" : "推断链路");
}

function bindRequestTableActions() {
  byId("request-table").querySelectorAll("[data-privacy-trace]").forEach((button) => {
    button.addEventListener("click", async () => {
      byId("trace-mode-filter").value = "all";
      byId("trace-review-filter").value = "";
      byId("trace-privacy-filter").value = "";
      byId("trace-client-filter").value = "";
      byId("trace-task-filter").value = "";
      byId("trace-model-filter").value = "";
      byId("trace-status-filter").value = "";
      byId("trace-search").value = button.dataset.privacyTrace;
      await switchView("audit");
      await selectRouteTrace(button.dataset.privacyTrace);
    });
  });
  byId("request-table").querySelectorAll(
    "[data-request-conversation]",
  ).forEach((button) => {
    button.addEventListener("click", () => {
      void toggleRequestConversation(
        button.dataset.requestConversation,
      );
    });
  });
  byId("request-table").querySelectorAll(
    "[data-request-earlier]",
  ).forEach((button) => {
    button.addEventListener("click", () => {
      void loadEarlierConversation(button.dataset.requestEarlier);
    });
  });
  byId("request-table").querySelectorAll(
    "[data-request-standalone]",
  ).forEach((button) => {
    button.addEventListener("click", () => {
      const requestId = button.dataset.requestStandalone;
      if (state.requestExpandedRequests.has(requestId)) {
        state.requestExpandedRequests.delete(requestId);
      } else {
        state.requestExpandedRequests.add(requestId);
      }
      renderRequestTable();
    });
  });
}

async function toggleRequestConversation(conversationId) {
  if (state.requestExpandedConversations.has(conversationId)) {
    state.requestExpandedConversations.delete(conversationId);
    renderRequestTable();
    return;
  }
  state.requestExpandedConversations.add(conversationId);
  renderRequestTable();
  if (!state.requestConversationPages.has(conversationId)) {
    await loadConversationPage(conversationId);
  }
}

async function fetchConversationTurns(conversationId, cursor = null, preserveEarlier = false) {
  const pending = conversationTurnLoads.get(conversationId);
  if (pending?.cursor === cursor && pending.preserveEarlier === preserveEarlier) {
    return pending.promise;
  }
  // Serialize refresh and pagination for this conversation; other conversations stay independent.
  const promise = (pending?.promise || Promise.resolve()).then(
    () => fetchConversationTurnsPage(conversationId, cursor, preserveEarlier),
  );
  const load = {cursor, preserveEarlier, promise};
  conversationTurnLoads.set(conversationId, load);
  try {
    await promise;
  } finally {
    if (conversationTurnLoads.get(conversationId) === load) {
      conversationTurnLoads.delete(conversationId);
    }
  }
}

async function fetchConversationTurnsPage(conversationId, cursor, preserveEarlier) {
  const current = state.requestConversationPages.get(conversationId);
  state.requestConversationPages.set(conversationId, {
    items: [],
    ...(current || {}),
    loading: true,
    error: null,
  });
  try {
    const payload = await api(
      `/api/route-traces?${requestFilterQuery({
        cursor,
        conversationId,
        limit: 100,
      })}`,
    );
    const fresh = payload.items || [];
    const previousIds = new Set((current?.items || []).map((item) => item.request_id));
    const keepEarlier = !cursor && preserveEarlier
      && (current?.items?.length || 0) > fresh.length
      && Number(payload.total_count) >= Number(current?.totalCount || 0)
      && fresh.some((item) => previousIds.has(item.request_id));
    const combined = cursor || keepEarlier
      ? [...(current?.items || []), ...(payload.items || [])]
      : (payload.items || []);
    const unique = new Map(
      combined.map((item) => [item.request_id, item]),
    );
    const items = [...unique.values()].sort(
      (left, right) => (
        Number(left.started_at) - Number(right.started_at)
        || String(left.request_id).localeCompare(String(right.request_id))
      ),
    );
    state.requestConversationPages.set(conversationId, {
      items,
      nextCursor: keepEarlier ? current.nextCursor : (payload.next_cursor || null),
      totalCount: Number(payload.total_count || items.length),
      loading: false,
      error: null,
    });
  } catch (error) {
    state.requestConversationPages.set(conversationId, {
      items: [],
      ...(current || {}),
      loading: false,
      error: error.message,
    });
  }
}

async function loadConversationPage(conversationId, cursor = null) {
  await fetchConversationTurns(conversationId, cursor);
  renderRequestTable();
}

async function loadTraceTimeline(conversationId, cursor = null) {
  await fetchConversationTurns(conversationId, cursor, true);
  if (state.selectedTrace?.conversation_id === conversationId) {
    renderTraceTimeline();
  }
}

async function loadEarlierConversation(conversationId) {
  const page = state.requestConversationPages.get(conversationId);
  if (!page?.nextCursor) return;
  const oldestRequestId = page.items?.[0]?.request_id;
  const anchor = oldestRequestId
    ? Array.from(document.querySelectorAll("[data-request-round-id]"))
      .find((item) => item.dataset.requestRoundId === oldestRequestId)
    : null;
  const previousTop = anchor?.getBoundingClientRect().top;
  await loadConversationPage(conversationId, page.nextCursor);
  if (previousTop == null || !oldestRequestId) return;
  requestAnimationFrame(() => {
    const nextAnchor = Array.from(
      document.querySelectorAll("[data-request-round-id]"),
    ).find((item) => item.dataset.requestRoundId === oldestRequestId);
    if (!nextAnchor) return;
    window.scrollBy({
      top: nextAnchor.getBoundingClientRect().top - previousTop,
      behavior: "auto",
    });
  });
}

function graphemeExcerpt(value, limit = 80) {
  const text = String(value || "").replace(/\s+/gu, " ").trim();
  if (!text) return "无文本摘要";
  const values = typeof Intl.Segmenter === "function"
    ? [...new Intl.Segmenter("zh-CN", {
      granularity: "grapheme",
    }).segment(text)].map((item) => item.segment)
    : Array.from(text);
  return values.length > limit
    ? `${values.slice(0, limit).join("")}…`
    : text;
}

async function loadRouteAudit(silent = false) {
  if (!state.key) return;
  try {
    await Promise.all([
      state.routeGraph ? Promise.resolve() : loadRouteGraph(),
      loadRouteTraces(true),
    ]);
    if (traceEnteredRouting(state.selectedTrace)) await ensureTraceGraphRendered();
    if (!silent) notice("");
  } catch (error) {
    if (!silent) notice(error.message, true);
  }
}

async function loadRouteGraph(mode = state.routeGraphMode || "simple") {
  const graph = await api(`/api/route-graph?mode=${encodeURIComponent(mode)}`);
  activateTraceGraph(graph, mode);
}

function activateTraceGraph(graph, mode) {
  state.routeGraphMode = graph.mode || mode;
  state.routeGraphCache[state.routeGraphMode] = graph;
  state.routeGraph = graph;
  Object.keys(traceNodeLabels).forEach((key) => delete traceNodeLabels[key]);
  (state.routeGraph.nodes || []).forEach((node) => {
    traceNodeLabels[node.id] = node.label;
  });
  traceGraphRenderKey = "";
}

async function setTraceGraphMode(mode) {
  if (mode === state.routeGraphMode) return;
  byId("trace-graph-mode-simple").classList.toggle("active", mode === "simple");
  byId("trace-graph-mode-detailed").classList.toggle("active", mode === "detailed");
  try {
    if (state.routeGraphCache[mode]) {
      activateTraceGraph(state.routeGraphCache[mode], mode);
    } else {
      await loadRouteGraph(mode);
    }
    traceGraphRenderKey = "";
    state.selectedTraceNodeId = null;
    traceGraphNeedsInitialFocus = true;
    if (state.selectedTrace && traceEnteredRouting(state.selectedTrace)) {
      await ensureTraceGraphRendered();
    }
  } catch (error) {
    notice(error.message, true);
  }
}

function traceFilterQuery(cursor = null) {
  const query = new URLSearchParams({
    limit: "50",
    request_mode: byId("trace-mode-filter").value || "auto",
  });
  const values = {
    review_status: byId("trace-review-filter").value,
    privacy_decision: byId("trace-privacy-filter").value,
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
  const selectedAtStart = state.selectedTraceId;
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
    const selectionChanged = state.selectedTraceId !== selectedAtStart;
    const keepSelection = selectionChanged
      || (silent && state.selectedTrace?.request_id === state.selectedTraceId);
    if (!append && state.routeTraces.length && !selectedStillVisible && !keepSelection) {
      await selectRouteTrace(state.routeTraces[0].request_id);
    } else if (!append && !state.routeTraces.length && !keepSelection) {
      clearTraceDetail();
    } else if (
      !selectionChanged
      &&
      !traceReviewInProgress() &&
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
        ${privacyBadge(item.privacy_assessment)}
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

const privacyDecisionLabels = {normal: "正常任务", internal_info: "探询内部信息", uncertain: "无法确定"};
const privacyReasonLabels = {
  technical_task: "常规技术任务", internal_identity: "内部身份",
  internal_infrastructure: "内部架构", ambiguous: "意图不明确",
  queued: "等待审核", local_busy: "审核器繁忙", shared_busy: "其他实例正在审核",
  sample_limit: "达到采样限额", view_or_budget: "输入不明确或超出审核预算",
  review_unavailable: "审核不可用", cancelled: "审核取消", expired: "审核超时或服务重启",
  backend_busy: "模型繁忙", not_resident: "模型不可用", context_mismatch: "审核上下文不匹配",
};

const traceReviewFields = {
  privacy: [
    ["privacy-feedback-decision", "decision"],
    ["privacy-feedback-note", "note"],
  ],
  routing: [
    ["trace-review-verdict", "verdict"],
    ["trace-expected-task", "expected_task"],
    ["trace-expected-model", "expected_model"],
    ["trace-review-note", "note"],
  ],
};

function traceReviewValues(kind) {
  return traceReviewFields[kind].map(([id]) => byId(id).value);
}

function traceReviewHasDraft(kind, trace = state.selectedTrace) {
  if (!trace) return false;
  const saved = kind === "privacy" ? trace.privacy_feedback?.[0] : trace.current_review;
  return traceReviewFields[kind].some(
    ([id, field]) => byId(id).value !== (saved?.[field] || ""),
  );
}

function traceReviewInProgress() {
  if (byId("privacy-feedback-form").contains(document.activeElement)
      || byId("trace-review-form").contains(document.activeElement)) return true;
  return Object.keys(traceReviewFields).some((kind) => traceReviewHasDraft(kind));
}

function privacyBadge(value) {
  if (!value) return '<span class="badge neutral">隐私未审核</span>';
  const label = value.status === "completed"
    ? privacyDecisionLabels[value.decision] || "无法确定"
    : {pending: "审核中", skipped: "已跳过", unavailable: "审核不可用"}[value.status] || "未审核";
  const tone = value.status === "completed" && value.decision === "internal_info" ? "warning" : "neutral";
  return `<span class="badge ${tone}" title="${escapeHtml(privacyReasonLabels[value.reason] || value.reason || "")}">旁路 · ${escapeHtml(label)}</span>`;
}

function renderPrivacyAssessment(trace, preserveDraft = false) {
  const value = trace.privacy_assessment;
  const fields = value ? [
    privacyBadge(value),
    `依据 <strong>${escapeHtml(privacyReasonLabels[value.reason] || value.reason)}</strong>`,
    `审核模型 <code>${escapeHtml(value.review_model || "—")}</code>`,
    `策略 <code>${escapeHtml(value.policy_version || "—")}</code>`,
    `耗时 <strong>${value.elapsed_ms == null ? "—" : formatDuration(value.elapsed_ms)}</strong>`,
    `输入来源 <code>${escapeHtml(value.source || "—")}</code>`,
    `审核时间 <strong>${formatTime(value.updated_at)}</strong>`,
    ...(value.review_request_id ? [`审核请求 <code>${escapeHtml(value.review_request_id)}</code>`] : []),
  ] : [privacyBadge(null)];
  byId("trace-privacy-result").innerHTML = fields.map((field) => `<span>${field}</span>`).join("");
  const feedback = trace.privacy_feedback || [];
  if (!preserveDraft) {
    byId("privacy-feedback-decision").value = feedback[0]?.decision || "";
    byId("privacy-feedback-note").value = feedback[0]?.note || "";
  }
  byId("privacy-feedback-history").innerHTML = feedback.map((item) =>
    `<span>${formatTime(item.created_at)} · 人工：${escapeHtml(privacyDecisionLabels[item.decision])} · ${escapeHtml(item.note || "无备注")}</span>`
  ).join("");
}

async function submitPrivacyFeedback(event) {
  event.preventDefault();
  const requestId = state.selectedTraceId;
  if (!requestId) return;
  const submittedReview = {kind: "privacy", values: traceReviewValues("privacy")};
  const button = byId("privacy-feedback-submit");
  button.disabled = true;
  try {
    await api(`/api/route-traces/${encodeURIComponent(requestId)}/privacy-feedback`, {
      method: "POST", body: JSON.stringify({
        decision: byId("privacy-feedback-decision").value,
        note: byId("privacy-feedback-note").value.trim() || null,
      }),
    });
    notice("隐私复核已保存。");
    if (state.selectedTraceId === requestId) await selectRouteTrace(requestId, true, submittedReview);
  } catch (error) {
    notice(error.message, true);
  } finally {
    button.disabled = false;
  }
}

async function selectRouteTrace(requestId, silent = false, submittedReview = null) {
  if (!requestId) return;
  const sequence = ++traceSelectionSequence;
  const changed = state.selectedTraceId !== requestId;
  state.selectedTraceId = requestId;
  if (changed) {
    traceGraphScale = defaultTraceGraphScale();
    traceGraphNeedsInitialFocus = true;
  }
  renderTraceList();
  try {
    const payload = await api(`/api/route-traces/${encodeURIComponent(requestId)}`);
    if (sequence !== traceSelectionSequence || state.selectedTraceId !== requestId) return;
    const sameRequest = state.selectedTrace?.request_id === requestId;
    const preserveReviews = {};
    for (const kind of Object.keys(traceReviewFields)) {
      const unchangedSubmission = submittedReview?.kind === kind
        && JSON.stringify(traceReviewValues(kind)) === JSON.stringify(submittedReview.values);
      preserveReviews[kind] = sameRequest && traceReviewHasDraft(kind) && !unchangedSubmission;
    }
    const selectedTrace = payload.trace;
    const detailRequests = [
      api(`/api/route-traces/${encodeURIComponent(requestId)}/diagnosis`),
    ];
    if (
      selectedTrace.conversation_id
      && selectedTrace.client_id
    ) {
      detailRequests.push(
        api(
          `/api/conversations/${
            encodeURIComponent(selectedTrace.conversation_id)
          }/control?client_id=${
            encodeURIComponent(selectedTrace.client_id)
          }`,
        ),
      );
    }
    const detailResults = await Promise.allSettled(detailRequests);
    if (
      sequence !== traceSelectionSequence
      || state.selectedTraceId !== requestId
    ) return;
    state.selectedTrace = selectedTrace;
    state.routeDiagnosis = null;
    state.conversationControl = null;
    if (detailResults[0]?.status === "fulfilled") {
      state.routeDiagnosis = detailResults[0].value.diagnosis;
    }
    if (detailResults[1]?.status === "fulfilled") {
      state.conversationControl = detailResults[1].value.control;
    }
    const attempts = state.selectedTrace.attempts || [];
    const attemptNumbers = attempts.map((item) => Number(item.number));
    if (!attemptNumbers.includes(state.selectedTraceAttempt)) {
      state.selectedTraceAttempt = Math.max(...attemptNumbers, 1);
      state.selectedTraceNodeId = null;
    }
    if (!sameRequest) state.selectedTraceNodeId = null;
    renderTraceDetail(preserveReviews);
    if (state.selectedTrace.conversation_id) {
      void loadTraceTimeline(state.selectedTrace.conversation_id);
    }
    if (cacheView.stage === "routing" && traceEnteredRouting(state.selectedTrace)) {
      await ensureTraceGraphRendered();
    }
  } catch (error) {
    if (!silent) notice(error.message, true);
  }
}

function clearTraceDetail() {
  traceSelectionSequence += 1;
  state.selectedTraceId = null;
  state.selectedTrace = null;
  state.routeDiagnosis = null;
  state.conversationControl = null;
  state.selectedTraceNodeId = null;
  byId("trace-detail").hidden = true;
  byId("trace-detail-empty").hidden = false;
}

function renderTraceDetail(preserveReviews = {}) {
  const trace = state.selectedTrace;
  if (!trace) {
    clearTraceDetail();
    return;
  }
  byId("trace-detail-empty").hidden = true;
  byId("trace-detail").hidden = false;
  byId("trace-detail-title").textContent =
    trace.identity_intercepted
      ? "身份直答"
      : shortModel(trace.selected_model || trace.requested_model || "路由轨迹");
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
    `请求 <code>${escapeHtml(trace.request_id)}</code>`,
    ...(trace.client_request_id
      ? [`客户端请求 <code>${escapeHtml(trace.client_request_id)}</code>`]
      : []),
    `客户端 <strong>${escapeHtml(trace.client_id)}</strong>`,
    `披露 <strong>${trace.disclosure_mode === "public" ? "客户脱敏" : "内部详细"}</strong>`,
    `会话 <code title="${escapeHtml(trace.conversation_id || "")}">${escapeHtml(shortId(trace.conversation_id || "—", 24))}</code>`,
    `上下文 <strong>${trace.request?.context_compacted ? `${trace.request?.context_compaction_source === "client" ? "客户端" : "Router"}压缩` : trace.request?.conversation_mode === "stateful" ? "显式 ID" : "推断 ID"}</strong>`,
    `画像 <strong>${escapeHtml(trace.task || "—")}</strong>`,
    `Token <strong>${formatTokens(trace.request?.prompt_tokens || 0)} + ${formatTokens(trace.request?.output_reserve_tokens || 0)}</strong>`,
    `策略 <code>${escapeHtml(trace.settings_fingerprint || "—")}</code>`,
    `注册表 <code>${escapeHtml(trace.registry_fingerprint || "—")}</code>`,
  ].map((item) => `<span>${item}</span>`).join("");
  renderTraceRoutingState(trace);
  renderTraceTimeline();
  renderRouteDiagnosis();
  renderPrivacyAssessment(trace, preserveReviews.privacy);
  renderTraceAttempts();
  renderTraceCurrentReview(preserveReviews.routing);
  renderUnifiedAudit();
}

function renderRouteDiagnosis() {
  const diagnosis = state.routeDiagnosis;
  const trace = state.selectedTrace;
  byId("trace-diagnosis-version").textContent = diagnosis
    ? `规则版本 ${diagnosis.diagnosis_version}`
    : "诊断不可用";
  byId("trace-diagnosis-verdict").textContent = diagnosis?.verdict
    || "当前轨迹没有足够证据生成诊断。";
  byId("trace-diagnosis-chain").innerHTML = (diagnosis?.causal_chain || [])
    .map((item) => `
      <li>
        <strong>${escapeHtml(item.title)}</strong>
        <span>${escapeHtml(item.detail)}</span>
      </li>`)
    .join("");
  byId("trace-diagnosis-non-causes").innerHTML = (
    diagnosis?.non_causes || []
  ).map((item) => `
    <div>
      <strong>${escapeHtml(item.label)}</strong>
      <code>${escapeHtml(item.evidence)}</code>
    </div>`).join("") || '<span class="section-meta">没有可明确排除的因素</span>';
  byId("trace-diagnosis-alternatives").innerHTML = (
    diagnosis?.alternatives || []
  ).map((item) => `
    <div class="diagnosis-alternative ${item.eligible ? "eligible" : "rejected"}">
      <strong>${escapeHtml(item.endpoint_id || "—")}</strong>
      <span>${escapeHtml(item.node || "—")} · ${escapeHtml(item.tier || "—")}</span>
      <span>${item.eligible ? "合格" : escapeHtml(item.rejection_label)}</span>
      <span>容量 ${formatTokens(item.required_context_tokens)} / ${formatTokens(item.safe_context_tokens)}</span>
      <span>负载余量 ${item.load_headroom == null ? "—" : formatPercent(Number(item.load_headroom))}</span>
    </div>`).join("") || '<span class="section-meta">未记录候选快照</span>';
  byId("trace-diagnosis-phases").innerHTML = (
    diagnosis?.conversation_phases || []
  ).map((item, index) => `
    <div class="diagnosis-phase">
      <span>${index + 1}</span>
      <strong>${escapeHtml(item.endpoint_id)}</strong>
      <small>${item.request_count} 轮 · ${escapeHtml((item.flags || []).join(" / ") || "稳定")}</small>
    </div>`).join("") || '<span class="section-meta">当前仅有单轮证据</span>';
  byId("trace-policy-refs").innerHTML = (
    diagnosis?.policy_refs || []
  ).map((item) => `
    <button type="button" class="secondary compact"
      data-policy-ref="${escapeHtml(item.section)}">
      ${escapeHtml(item.label)}
      <code>${escapeHtml(item.field)}</code>
    </button>`).join("");

  const actions = diagnosis?.actions || [];
  const pins = actions.filter((item) => item.id === "pin_endpoint");
  const pin = state.conversationControl?.pin;
  const endpointSelect = byId("conversation-pin-endpoint");
  const endpointIds = (state.dashboard?.endpoints || [])
    .map((item) => item.endpoint)
    .filter((item) => item.enabled && item.role === "responder")
    .map((item) => item.id);
  endpointSelect.innerHTML = endpointIds.length
    ? endpointIds.map((endpointId) => `
      <option value="${escapeHtml(endpointId)}">
        ${escapeHtml(endpointId)}
      </option>`).join("")
    : '<option value="">没有已启用端点</option>';
  endpointSelect.value = (
    pin?.endpoint_id
    || pins[0]?.endpoint_id
    || endpointIds[0]
    || ""
  );
  byId("trace-conversation-actions").innerHTML = [
    `<button type="button" class="secondary" data-conversation-action="reset">重置下一轮亲和</button>`,
    ...pins.map((item) => `
      <button type="button" class="secondary"
        data-conversation-action="pin"
        data-endpoint-id="${escapeHtml(item.endpoint_id)}">
        固定 ${escapeHtml(item.endpoint_id)}
      </button>`),
    `<button type="button" class="secondary"
      data-conversation-action="pin-selected"
      ${endpointIds.length ? "" : "disabled"}>
      固定选择端点
    </button>`,
    ...(pin
      ? [
        `<button type="button" class="secondary" data-conversation-action="unpin">
          解除固定 ${escapeHtml(pin.endpoint_id)}
        </button>`,
      ]
      : []),
  ].join("");

  document.querySelectorAll("[data-policy-ref]").forEach((button) => {
    button.addEventListener("click", () => {
      switchView("settings");
      requestAnimationFrame(() => {
        byId(button.dataset.policyRef)?.scrollIntoView({
          behavior: "smooth",
          block: "center",
        });
      });
    });
  });
  byId("trace-conversation-actions")
    .querySelectorAll("[data-conversation-action]")
    .forEach((button) => {
      button.addEventListener("click", () => {
        void performConversationAction(
          button.dataset.conversationAction === "pin-selected"
            ? "pin"
            : button.dataset.conversationAction,
          button.dataset.conversationAction === "pin-selected"
            ? byId("conversation-pin-endpoint").value
            : button.dataset.endpointId,
        );
      });
    });
  if (!trace?.conversation_id || !trace?.client_id) {
    byId("trace-conversation-actions").innerHTML =
      '<span class="section-meta">此请求没有可控制的会话标识</span>';
  }
}

async function performConversationAction(action, endpointId = null) {
  const trace = state.selectedTrace;
  if (!trace?.conversation_id || !trace?.client_id) return;
  const actionLabel = {
    reset: "重置下一轮路由亲和",
    pin: `临时固定到 ${endpointId}`,
    unpin: "提前解除当前固定",
  }[action];
  if (!window.confirm(`${actionLabel}？操作只影响下一轮请求。`)) return;
  const container = byId("trace-conversation-actions");
  container.querySelectorAll("button").forEach((button) => {
    button.disabled = true;
  });
  try {
    const payload = await api(
      `/api/conversations/${
        encodeURIComponent(trace.conversation_id)
      }/actions/${encodeURIComponent(action)}`,
      {
        method: "POST",
        body: JSON.stringify({
          client_id: trace.client_id,
          endpoint_id: endpointId,
          ttl_seconds: Number(byId("conversation-pin-ttl").value),
          reason: byId("conversation-action-reason").value.trim(),
        }),
      },
    );
    state.conversationControl = payload.control;
    renderRouteDiagnosis();
    notice(`${actionLabel}已记录。`);
  } catch (error) {
    notice(error.message, true);
    renderRouteDiagnosis();
  }
}

function renderTraceTimeline() {
  const section = byId("trace-timeline-section");
  const conversationId = state.selectedTrace?.conversation_id;
  if (!conversationId) {
    section.hidden = true;
    return;
  }
  section.hidden = false;
  const page = state.requestConversationPages.get(conversationId);
  byId("trace-timeline-earlier").hidden = !page?.nextCursor;
  const stateLabel = byId("trace-timeline-state");
  const target = byId("trace-timeline");
  if (!page || (page.loading && !page.items?.length)) {
    stateLabel.textContent = "正在加载";
    target.innerHTML = "";
    return;
  }
  if (page.error) {
    stateLabel.textContent = "加载失败";
    target.innerHTML = `<p class="empty">${escapeHtml(page.error)}</p>`;
    return;
  }
  const items = page.items || [];
  const total = Math.max(items.length, page.totalCount || 0);
  stateLabel.textContent = `已加载 ${items.length} / ${total} 轮`;
  target.innerHTML = items.map(
    (item, index) => traceTimelineCard(item, total - items.length + index + 1),
  ).join("");
  bindTraceTimeline();
}

function traceTimelineCard(item, turnIndex) {
  const selected = item.request_id === state.selectedTraceId;
  const m = selected ? state.selectedTrace?.cache_audit?.request : item.cache_audit;
  return `<button class="trace-timeline-card${selected ? " selected" : ""}" type="button" data-trace-id="${escapeHtml(item.request_id)}">
    <span class="trace-timeline-index">#${turnIndex}</span>
    ${statusBadge(item.status,item.status_code,item.error?.code,Boolean(item.task||item.selected_model))}
    <strong>${escapeHtml(m?.device||item.node||item.deployment_id||item.selected_model||"未确定设备")}</strong>
    <span class="trace-timeline-meta">首个输出 ${escapeHtml(cacheFormat(m?.ttft_ms))}</span>
    <span class="trace-timeline-meta">${escapeHtml(cacheBrief(m))}</span>
    <span class="trace-timeline-time">${formatTime(item.started_at)}</span>
  </button>`;
}

function bindTraceTimeline() {
  byId("trace-timeline").querySelectorAll("[data-trace-id]").forEach((button) => {
    button.addEventListener("click", () => {
      void selectRouteTrace(button.dataset.traceId);
    });
  });
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
    if (trace.identity_intercepted) {
      target.className = "trace-routing-state info";
      target.innerHTML = `<strong>身份直答</strong><span>公共身份请求已由 Router 直接完成，未调用 evaluator 或底层模型；下方流程图展示的是拦截判断本身，终点为"身份直答"而非某个具体模型。</span>`;
      target.hidden = false;
      return;
    }
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
  return Boolean(trace?.identity_intercepted || trace?.task || trace?.selected_model);
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
  const rawSteps = (
    state.routeGraphMode === "detailed"
      ? attempt?.steps
      : attempt?.simple_steps
  ) || [];
  const graphNodeIds = new Set(
    (state.routeGraph?.nodes || []).map((item) => item.id),
  );
  const steps = rawSteps.filter(
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
  const detailed = state.routeGraphMode === "detailed";
  const preferred = requestedModel === "auto"
    ? (detailed
      ? [
          "local_sufficiency",
          "remote_expert_dispatch",
          "candidate_scope",
          "route_selected",
        ]
      : [
          "intelligent_v2_dispatch",
          "provider_priority",
          "candidate_scope",
          "route_selected",
        ])
    : (detailed
      ? [
          "explicit_model",
          "explicit_selection",
          "candidate_scope",
          "route_selected",
        ]
      : [
          "select_endpoint",
          "candidate_scope",
          "route_selected",
        ]);
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

function renderTraceCurrentReview(preserveDraft = false) {
  const review = state.selectedTrace?.current_review;
  byId("trace-current-review").textContent = review
    ? `${reviewLabels[review.verdict] || review.verdict} · ${formatTime(review.created_at)}`
    : "尚未审核";
  if (!preserveDraft) {
    byId("trace-review-verdict").value = review?.verdict || "";
    byId("trace-expected-task").value = review?.expected_task || "";
    byId("trace-expected-model").value = review?.expected_model || "";
    byId("trace-review-note").value = review?.note || "";
  }
}

async function submitTraceReview(event) {
  event.preventDefault();
  const requestId = state.selectedTraceId;
  if (!requestId) return;
  const submittedReview = {kind: "routing", values: traceReviewValues("routing")};
  const button = byId("trace-review-submit");
  button.disabled = true;
  try {
    await api(
      `/api/route-traces/${encodeURIComponent(requestId)}/reviews`,
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
    if (state.selectedTraceId === requestId) {
      await selectRouteTrace(requestId, true, submittedReview);
    }
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
  svg.style.width = `${viewBoxWidth * traceGraphScale}px`;
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
    0.01,
    Math.min(1.4, (viewport.clientWidth - 60) / viewBoxWidth),
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

function formatBytes(value) {
  const bytes = Number(value || 0);
  if (!Number.isFinite(bytes) || bytes <= 0) return "0 B";
  const units = ["B", "KiB", "MiB", "GiB", "TiB"];
  const index = Math.min(
    units.length - 1,
    Math.floor(Math.log(bytes) / Math.log(1024)),
  );
  const scaled = bytes / (1024 ** index);
  return `${scaled >= 100 ? scaled.toFixed(0) : scaled.toFixed(1)} ${units[index]}`;
}

function value(path, fallback = "") {
  let current = state.settings;
  for (const part of path.split(".")) current = current?.[part];
  return current ?? fallback;
}

function mergeObjects(base, override) {
  if (
    !base || typeof base !== "object" || Array.isArray(base)
    || !override || typeof override !== "object" || Array.isArray(override)
  ) return structuredClone(override ?? base);
  const result = structuredClone(base);
  Object.entries(override).forEach(([key, item]) => {
    result[key] = (
      item && typeof item === "object" && !Array.isArray(item)
      && result[key] && typeof result[key] === "object"
      && !Array.isArray(result[key])
    )
      ? mergeObjects(result[key], item)
      : structuredClone(item);
  });
  return result;
}

async function loadSettings() {
  if (!state.key) return;
  const [payload, poolPayload] = await Promise.all([
    api("/api/policy"),
    api("/api/prompt-directives/pool"),
  ]);
  state.policy = payload.policy;
  const selected = state.policy?.draft?.settings
    || state.policy?.active?.settings
    || {};
  state.settings = mergeObjects(payload.effective_settings, selected);
  state.directivePool = poolPayload.pool;
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
  byId("review-mode").value = value("identity.review.mode", "off");
  byId("review-backend").value = value("identity.review.backend", "ollama");
  byId("review-base-url").value = value(
    "identity.review.base_url",
    "http://agx.taild500c8.ts.net:11434",
  );
  byId("review-model").value = value(
    "identity.review.model",
    "qwen3:4b-instruct",
  );
  byId("review-sample-rate").value = value(
    "identity.review.sample_rate",
    0.1,
  );
  byId("review-rpm").value = String(
    value("identity.review.requests_per_minute", 2),
  );
  byId("review-timeout").value = value(
    "identity.review.timeout_seconds",
    15,
  );
  byId("cloud-enabled").checked = Boolean(value("cloud.enabled", false));
  byId("cloud-auto").checked = Boolean(value("cloud.auto_escalate", false));
  byId("cloud-budget").value = value("cloud.monthly_budget", 0);
  byId("cloud-providers").value = value("cloud.allowed_providers", []).join(", ");
  byId("cloud-models").value = value("cloud.allowed_models", []).join(", ");
  byId("directive-enabled").checked = Boolean(
    value("routing.prompt_directives.enabled", false),
  );
  renderPromptDirectives();
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
  byId("local-pool-enabled").checked = Boolean(value("routing.local_pool.enabled", false));
  byId("local-pool-recent").value = value("routing.local_pool.recent_seconds", 600) / 60;
  byId("stability-enabled").checked = Boolean(
    value("routing.conversation_stability.enabled", false),
  );
  byId("stability-failure-threshold").value = value(
    "routing.conversation_stability.health_failure_threshold",
    2,
  );
  byId("stability-recheck-interval").value = value(
    "routing.conversation_stability.health_recheck_interval_seconds",
    10,
  );
  byId("stability-recovery-mode").value = value(
    "routing.conversation_stability.recovery_mode",
    "manual",
  );
  byId("stability-preserve-tier").checked = Boolean(
    value(
      "routing.conversation_stability.preserve_tier_after_migration",
      true,
    ),
  );
  byId("health-refresh-seconds").value = value(
    "health.refresh_seconds",
    5,
  );
  byId("health-stale-seconds").value = value(
    "health.stale_after_seconds",
    15,
  );
  byId("health-probe-timeout").value = value(
    "health.probe_timeout_seconds",
    3,
  );
  byId("lmcache-enabled").checked = Boolean(
    value("lmcache.enabled", false),
  );
  byId("lmcache-l1-size").value = value("lmcache.l1_size_gb", 80);
  byId("lmcache-memory-max").value = value(
    "lmcache.memory_max_gb",
    96,
  );
  byId("lmcache-chunk-size").value = value(
    "lmcache.chunk_size",
    1600,
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
  renderRemoteFallbackOrder();
  renderLmcacheRuntimeStatus();
  updateStrategyBranchVisibility();
  updateWeightsTotal();
  renderPolicyState();
}

function renderPolicyState() {
  const active = state.policy?.active;
  const draft = state.policy?.draft;
  byId("policy-revision-state").textContent = draft
    ? `草稿 r${draft.revision} · ${draft.status === "validated" ? "已验证" : "待验证"}`
    : `当前激活 r${active?.revision || "—"} · 无草稿`;
  byId("policy-fingerprint-state").textContent = draft
    ? `基于 r${draft.base_revision || active?.revision || "—"} · ${draft.settings_fingerprint}`
    : active?.settings_fingerprint || "";
  const revisions = state.policy?.revisions || [];
  const select = byId("policy-rollback-revision");
  select.innerHTML = revisions.length
    ? revisions.map((item) => `
      <option value="${item.revision}">
        r${item.revision} · ${formatTime(item.activated_at || item.updated_at)}
      </option>`).join("")
    : '<option value="">暂无历史版本</option>';
  byId("policy-rollback").disabled = !revisions.length;
  byId("policy-validate").disabled = !draft;
  byId("policy-activate").disabled = draft?.status !== "validated";
  renderPolicyImpact(draft?.validation?.impact);
}

function renderPolicyImpact(impact) {
  const target = byId("policy-impact-report");
  if (!impact) {
    target.hidden = true;
    target.innerHTML = "";
    return;
  }
  target.hidden = false;
  target.innerHTML = `
    <strong>离线回放 ${Number(impact.evaluated_requests || 0)} 条 Auto 请求</strong>
    <span>健康复检候选 ${Number(impact.health_recheck_candidates || 0)}</span>
    <span>潜在迁移变化 ${Number(impact.route_changes || 0)}</span>
    <span>历史云端切换 ${Number(impact.cloud_switches || 0)}</span>
    <span>历史 429 ${Number(impact.observed_429 || 0)}</span>
    <span>不可路由 ${Number(impact.unroutable_requests || 0)}</span>
    <span>受影响客户端 ${escapeHtml((impact.affected_clients || []).join("、") || "无")}</span>
    <small>仅离线计算，不调用任何生产模型；最终是否保持原端点取决于下一次实时复检。</small>`;
}

function lmcacheRuntime() {
  const item = (state.dashboard?.endpoints || []).find(
    ({endpoint}) => endpoint.id === "ai-qwen38-27b",
  );
  return item?.status?.detail?.lmcache || null;
}

function lmcacheRestartRequired() {
  const desired = state.settings?.lmcache;
  const runtime = lmcacheRuntime();
  if (!desired || !runtime) return Boolean(desired?.enabled);
  const active = Boolean(runtime.connector_active);
  if (Boolean(desired.enabled) !== active) return true;
  if (!active) return false;
  const expectedBytes = Number(desired.l1_size_gb || 0) * (1024 ** 3);
  return (
    !runtime.healthy
    || Number(runtime.chunk_size || 0) !== Number(desired.chunk_size || 0)
    || Number(runtime.memory_total_bytes || 0) !== expectedBytes
  );
}

function renderLmcacheRuntimeStatus() {
  const target = byId("lmcache-runtime-status");
  if (!target || !state.settings) return;
  const desired = state.settings.lmcache || {};
  const runtime = lmcacheRuntime();
  if (!runtime) {
    target.textContent = "尚未取得 TP2 的 LMCache 运行状态；目标配置已保存时仍按需要重启处理。";
    return;
  }
  const active = Boolean(runtime.connector_active);
  const restartRequired = lmcacheRestartRequired();
  const requested = Number(runtime.lookup_requested_tokens || 0);
  const hits = Number(runtime.lookup_hit_tokens || 0);
  const hitRate = requested > 0
    ? `${Math.round((hits / requested) * 100)}%`
    : "—";
  const health = runtime.healthy ? "健康" : active ? "异常" : "未运行";
  target.textContent = [
    `目标：${desired.enabled ? "启用" : "关闭"} / ${desired.l1_size_gb} GiB`,
    `实际：${active ? "已连接" : "未连接"} / ${health}`,
    `内存：${formatBytes(runtime.memory_used_bytes)} / ${formatBytes(runtime.memory_total_bytes)}`,
    `注册：${Number(runtime.registered_count || 0)} / ${Number(runtime.expected_registrations || 0)}`,
    `命中：${formatTokens(hits)} / ${hitRate}`,
    `代次：${runtime.generation || "—"}`,
    restartRequired ? "状态：需要重启模型服务" : "状态：配置与运行一致",
  ].join(" · ");
}

function promptDirectiveSettings() {
  return state.settings?.routing?.prompt_directives || {};
}

function promptDirectiveEntry(id) {
  const settings = promptDirectiveSettings();
  return id === "reset" ? settings.reset : settings.routes?.[id];
}

function renderPromptDirectives() {
  const container = byId("directive-rows");
  if (!container) return;
  container.replaceChildren();
  PROMPT_DIRECTIVES.forEach(([id, label]) => {
    const entry = promptDirectiveEntry(id) || {};
    const row = document.createElement("div");
    row.className = "directive-row";
    row.innerHTML = `
      <label for="directive-phrase-${escapeHtml(id)}">
        <strong>${escapeHtml(label)}</strong>
        <span>${escapeHtml(entry.endpoint_id || "清除会话定向")}</span>
      </label>
      <div class="directive-secret">
        <input id="directive-phrase-${escapeHtml(id)}" type="password"
          value="${escapeHtml(entry.phrase || "")}"
          maxlength="120" autocomplete="off" spellcheck="false"
          data-directive-phrase="${escapeHtml(id)}">
        <button type="button" class="secondary compact"
          data-directive-reveal="${escapeHtml(id)}">显示</button>
        <button type="button" class="secondary compact"
          data-directive-copy="${escapeHtml(id)}">复制</button>
        <button type="button" class="secondary compact"
          data-directive-random="${escapeHtml(id)}">随机</button>
      </div>`;
    container.append(row);
  });
  bindPromptDirectiveControls();
  renderPromptDirectivePool();
}

function renderPromptDirectivePool() {
  const target = byId("directive-pool-status");
  const pool = state.directivePool;
  target.textContent = pool
    ? `可用 ${pool.available} / ${pool.total}`
    : "词池不可用";
}

function bindPromptDirectiveControls() {
  const container = byId("directive-rows");
  container.querySelectorAll("[data-directive-reveal]").forEach((button) => {
    button.addEventListener("click", () => {
      const input = container.querySelector(
        `[data-directive-phrase="${cssEscape(button.dataset.directiveReveal)}"]`,
      );
      const reveal = input.type === "password";
      input.type = reveal ? "text" : "password";
      button.textContent = reveal ? "隐藏" : "显示";
    });
  });
  container.querySelectorAll("[data-directive-copy]").forEach((button) => {
    button.addEventListener("click", async () => {
      const input = container.querySelector(
        `[data-directive-phrase="${cssEscape(button.dataset.directiveCopy)}"]`,
      );
      try {
        await navigator.clipboard.writeText(input.value);
        notice("暗语已复制。");
      } catch {
        input.select();
        document.execCommand("copy");
        notice("暗语已复制。");
      }
    });
  });
  container.querySelectorAll("[data-directive-random]").forEach((button) => {
    button.addEventListener("click", () => {
      void randomizePromptDirectives([button.dataset.directiveRandom]);
    });
  });
}

async function randomizePromptDirectives(ids) {
  const buttons = document.querySelectorAll(
    "#directive-random-all, [data-directive-random]",
  );
  buttons.forEach((button) => { button.disabled = true; });
  try {
    const payload = await api("/api/prompt-directives/suggest", {
      method: "POST",
      body: JSON.stringify({directive_ids: ids}),
    });
    Object.entries(payload.suggestions || {}).forEach(([id, phrase]) => {
      const input = document.querySelector(
        `[data-directive-phrase="${cssEscape(id)}"]`,
      );
      if (input) input.value = phrase;
    });
    state.directivePool = payload.pool;
    renderPromptDirectivePool();
    notice("新暗语已生成，保存后生效。");
  } catch (error) {
    notice(error.message, true);
  } finally {
    buttons.forEach((button) => { button.disabled = false; });
  }
}

function cloudEndpointIds() {
  return (state.dashboard?.endpoints || [])
    .map((item) => item.endpoint)
    .filter((item) => item.cloud)
    .map((item) => item.id);
}

function fallbackOrderList(profile) {
  const routing = state.settings.routing;
  if (!Array.isArray(routing.remote_fallback_order?.[profile])) {
    routing.remote_fallback_order = {
      ...(routing.remote_fallback_order || {}),
      [profile]: [],
    };
  }
  return routing.remote_fallback_order[profile];
}

function moveFallbackEntry(profile, index, delta) {
  const list = fallbackOrderList(profile);
  const target = index + delta;
  if (target < 0 || target >= list.length) return;
  [list[index], list[target]] = [list[target], list[index]];
  renderRemoteFallbackOrder();
}

function removeFallbackEntry(profile, index) {
  const list = fallbackOrderList(profile);
  if (list.length <= 1) return;
  list.splice(index, 1);
  renderRemoteFallbackOrder();
}

function addFallbackEntry(profile, endpointId) {
  const list = fallbackOrderList(profile);
  if (!endpointId || list.includes(endpointId)) return;
  list.push(endpointId);
  renderRemoteFallbackOrder();
}

function renderRemoteFallbackOrder() {
  const container = byId("fallback-chain-editor");
  if (!state.settings) return;
  container.replaceChildren();
  const available = cloudEndpointIds();
  FALLBACK_PROFILES.forEach(([profile, label]) => {
    const list = fallbackOrderList(profile);
    const section = document.createElement("div");
    section.className = "fallback-chain-profile";
    const heading = document.createElement("h5");
    heading.innerHTML =
      `${escapeHtml(profile)} <span class="section-meta">${escapeHtml(label)}</span>`;
    section.append(heading);

    const ol = document.createElement("ol");
    ol.className = "fallback-chain-list";
    list.forEach((endpointId, index) => {
      const li = document.createElement("li");
      li.className = "fallback-chain-row";
      li.innerHTML = `
        <span class="fallback-chain-position">${index + 1}</span>
        <span class="fallback-chain-id">${escapeHtml(endpointId)}</span>
        <span class="fallback-chain-actions">
          <button type="button" class="secondary compact"
            data-fallback-up="${escapeHtml(profile)}" data-fallback-index="${index}"
            title="上移" aria-label="上移 ${escapeHtml(endpointId)}"
            ${index === 0 ? "disabled" : ""}>↑</button>
          <button type="button" class="secondary compact"
            data-fallback-down="${escapeHtml(profile)}" data-fallback-index="${index}"
            title="下移" aria-label="下移 ${escapeHtml(endpointId)}"
            ${index === list.length - 1 ? "disabled" : ""}>↓</button>
          <button type="button" class="secondary compact"
            data-fallback-remove="${escapeHtml(profile)}" data-fallback-index="${index}"
            title="移除" aria-label="移除 ${escapeHtml(endpointId)}"
            ${list.length <= 1 ? "disabled" : ""}>×</button>
        </span>`;
      ol.append(li);
    });
    section.append(ol);

    const remaining = available.filter((id) => !list.includes(id));
    const addRow = document.createElement("div");
    addRow.className = "fallback-chain-add";
    const select = document.createElement("select");
    select.dataset.fallbackAddSelect = profile;
    select.disabled = !remaining.length;
    (remaining.length ? remaining : ["无可添加的云端端点"]).forEach((id) => {
      const option = document.createElement("option");
      option.value = remaining.length ? id : "";
      option.textContent = id;
      select.append(option);
    });
    const addButton = document.createElement("button");
    addButton.type = "button";
    addButton.className = "secondary compact";
    addButton.textContent = "添加";
    addButton.disabled = !remaining.length;
    addButton.dataset.fallbackAdd = profile;
    addRow.append(select, addButton);
    section.append(addRow);

    container.append(section);
  });
  bindFallbackChainControls();
  renderFallbackChainWarnings();
}

function bindFallbackChainControls() {
  const container = byId("fallback-chain-editor");
  container.querySelectorAll("[data-fallback-up]").forEach((button) => {
    button.addEventListener("click", () => {
      moveFallbackEntry(
        button.dataset.fallbackUp,
        Number(button.dataset.fallbackIndex),
        -1,
      );
    });
  });
  container.querySelectorAll("[data-fallback-down]").forEach((button) => {
    button.addEventListener("click", () => {
      moveFallbackEntry(
        button.dataset.fallbackDown,
        Number(button.dataset.fallbackIndex),
        1,
      );
    });
  });
  container.querySelectorAll("[data-fallback-remove]").forEach((button) => {
    button.addEventListener("click", () => {
      removeFallbackEntry(
        button.dataset.fallbackRemove,
        Number(button.dataset.fallbackIndex),
      );
    });
  });
  container.querySelectorAll("[data-fallback-add]").forEach((button) => {
    button.addEventListener("click", () => {
      const profile = button.dataset.fallbackAdd;
      const select = container.querySelector(
        `[data-fallback-add-select="${cssEscape(profile)}"]`,
      );
      if (select?.value) addFallbackEntry(profile, select.value);
    });
  });
}

function renderFallbackChainWarnings() {
  const target = byId("fallback-chain-warning");
  const order = state.settings?.routing?.remote_fallback_order || {};
  const endpoints = new Map(
    (state.dashboard?.endpoints || []).map(({endpoint}) => [endpoint.id, endpoint]),
  );
  const problems = [];
  FALLBACK_PROFILES.forEach(([profile]) => {
    (order[profile] || []).forEach((endpointId) => {
      const endpoint = endpoints.get(endpointId);
      if (!endpoint) {
        problems.push(`${profile}: ${endpointId}（未找到该端点）`);
      } else if (!endpoint.cloud) {
        problems.push(`${profile}: ${endpointId}（不是云端端点）`);
      }
    });
  });
  if (!problems.length) {
    target.hidden = true;
    target.innerHTML = "";
    return;
  }
  target.hidden = false;
  target.innerHTML = `
    <strong>回退链存在可能失效的条目</strong>
    <span>${problems.map(escapeHtml).join("；")}；保存不会被阻止，但这些条目在实际路由时会被跳过。</span>`;
}

function updateStrategyBranchVisibility() {
  const strategy = byId("routing-strategy").value;
  byId("branch-legacy_v1").classList.toggle(
    "dimmed",
    strategy !== "legacy_v1",
  );
  byId("branch-intelligent_v2").classList.toggle(
    "dimmed",
    strategy !== "intelligent_v2",
  );
}

function updateWeightsTotal() {
  const total = [...document.querySelectorAll("[data-weight]")].reduce(
    (sum, input) => sum + (Number(input.value) || 0),
    0,
  );
  const label = byId("weights-total");
  label.textContent = `总和 ${total.toFixed(2)}`;
  label.classList.toggle("invalid", Math.abs(total - 1) > 0.001);
}

function validateReviewBaseUrl(rawUrl, backend) {
  let parsed;
  try {
    parsed = new URL(String(rawUrl || ""));
  } catch {
    return "隐私旁路 base_url 不是合法的 URL";
  }
  const routerEndpoint = backend === "router"
    && String(rawUrl).replace(/\/$/, "") === "http://127.0.0.1:4000";
  if (backend === "router" && !routerEndpoint) {
    return "backend 为 router 时，base_url 必须是 http://127.0.0.1:4000";
  }
  const hostnameOk = parsed.hostname.endsWith(".taild500c8.ts.net")
    || ["localhost", "127.0.0.1", "[::1]"].includes(parsed.hostname);
  const portBlocked = ["4000", "4001"].includes(parsed.port) && !routerEndpoint;
  if (
    !["http:", "https:"].includes(parsed.protocol)
    || !hostnameOk
    || parsed.username || parsed.password || parsed.search || parsed.hash
    || !["", "/"].includes(parsed.pathname)
    || portBlocked
  ) {
    return "隐私旁路 base_url 必须是私网直连地址（详见字段说明）";
  }
  return null;
}

function validateSettingsDraft(draft) {
  const errors = [];
  const lmcache = draft.lmcache || {};
  const l1Size = Number(lmcache.l1_size_gb);
  if (
    !Number.isInteger(l1Size)
    || l1Size < 8
    || l1Size > 80
  ) {
    errors.push("LMCache CPU 缓存预算须为 8 到 80 GiB 的整数");
  }
  const weightTotal = Object.values(draft.routing.weights || {}).reduce(
    (sum, item) => sum + (Number(item) || 0),
    0,
  );
  if (Math.abs(weightTotal - 1) > 0.001) {
    errors.push(`路由权重总和须为 1.00，当前为 ${weightTotal.toFixed(2)}`);
  }
  const order = draft.routing.remote_fallback_order || {};
  if (draft.routing.strategy === "intelligent_v2") {
    const missing = FALLBACK_PROFILES
      .map(([profile]) => profile)
      .filter((profile) => !order[profile]?.length);
    if (missing.length) {
      errors.push(`云端回退顺序缺少画像：${missing.join("、")}`);
    }
  }
  const review = draft.identity.review || {};
  if (!["off", "shadow"].includes(review.mode)) {
    errors.push("隐私旁路模式必须是 off 或 shadow");
  }
  const backend = review.backend || "ollama";
  if (!["ollama", "llamacpp", "router"].includes(backend)) {
    errors.push("隐私旁路后端类型不合法");
  }
  const baseUrlError = validateReviewBaseUrl(review.base_url, backend);
  if (baseUrlError) errors.push(baseUrlError);
  const model = String(review.model || "").trim();
  if (!model || ["auto", "siyuan/auto"].includes(model.toLowerCase())) {
    errors.push("隐私旁路审核模型不能为空，也不能是 auto 或 siyuan/auto");
  }
  const sampleRate = Number(review.sample_rate);
  if (!Number.isFinite(sampleRate) || sampleRate < 0 || sampleRate > 1) {
    errors.push("隐私旁路采样率须在 0 到 1 之间");
  }
  const rpm = Number(review.requests_per_minute);
  if (![1, 2].includes(rpm)) {
    errors.push("隐私旁路每分钟请求上限只能是 1 或 2");
  }
  const timeout = Number(review.timeout_seconds);
  if (!Number.isFinite(timeout) || timeout < 1 || timeout > 120) {
    errors.push("隐私旁路超时须在 1 到 120 秒之间");
  }
  const directives = draft.routing.prompt_directives || {};
  const phrases = [
    ...Object.values(directives.routes || {}).map((item) => item.phrase),
    directives.reset?.phrase,
  ].map((item) => String(item || "").normalize("NFKC").trim().toLowerCase());
  if (phrases.some((item) => !item)) {
    errors.push("每条定向暗语都不能为空");
  } else if (new Set(phrases).size !== phrases.length) {
    errors.push("定向暗语不能重复");
  }
  const stability = draft.routing.conversation_stability || {};
  if (
    !Number.isInteger(Number(stability.health_failure_threshold))
    || Number(stability.health_failure_threshold) < 1
    || Number(stability.health_failure_threshold) > 5
  ) {
    errors.push("连续健康失败阈值须为 1 到 5");
  }
  const recheck = Number(stability.health_recheck_interval_seconds);
  if (!Number.isFinite(recheck) || recheck < 0 || recheck > 60) {
    errors.push("健康复检间隔须在 0 到 60 秒之间");
  }
  if (!["manual", "next_turn", "when_idle"].includes(stability.recovery_mode)) {
    errors.push("迁移恢复方式不合法");
  }
  const health = draft.health || {};
  if (
    Number(health.refresh_seconds) < 1
    || Number(health.refresh_seconds) > 60
  ) {
    errors.push("健康刷新间隔须在 1 到 60 秒之间");
  }
  if (
    Number(health.stale_after_seconds) < Number(health.refresh_seconds)
    || Number(health.stale_after_seconds) > 300
  ) {
    errors.push("健康过期时间须不小于刷新间隔且不超过 300 秒");
  }
  if (
    Number(health.probe_timeout_seconds) < 1
    || Number(health.probe_timeout_seconds) > 60
  ) {
    errors.push("健康探测超时须在 1 到 60 秒之间");
  }
  return errors;
}

function collectSettings() {
  const weights = {};
  document.querySelectorAll("[data-weight]").forEach((input) => {
    weights[input.dataset.weight] = Number(input.value);
  });
  const currentDirectives = promptDirectiveSettings();
  const directiveRoutes = {};
  Object.entries(currentDirectives.routes || {}).forEach(([id, route]) => {
    directiveRoutes[id] = {
      ...route,
      phrase: document.querySelector(
        `[data-directive-phrase="${cssEscape(id)}"]`,
      )?.value.trim() || "",
    };
  });
  const promptDirectives = {
    ...currentDirectives,
    enabled: byId("directive-enabled").checked,
    routes: directiveRoutes,
    reset: {
      ...currentDirectives.reset,
      phrase: document.querySelector(
        '[data-directive-phrase="reset"]',
      )?.value.trim() || "",
    },
  };
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
      review: {
        ...state.settings.identity.review,
        mode: byId("review-mode").value,
        backend: byId("review-backend").value,
        base_url: byId("review-base-url").value.trim(),
        model: byId("review-model").value.trim(),
        sample_rate: Number(byId("review-sample-rate").value),
        requests_per_minute: Number(byId("review-rpm").value),
        timeout_seconds: Number(byId("review-timeout").value),
      },
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
    health: {
      ...state.settings.health,
      refresh_seconds: Number(byId("health-refresh-seconds").value),
      stale_after_seconds: Number(byId("health-stale-seconds").value),
      probe_timeout_seconds: Number(byId("health-probe-timeout").value),
    },
    lmcache: {
      ...state.settings.lmcache,
      enabled: byId("lmcache-enabled").checked,
      l1_size_gb: Number(byId("lmcache-l1-size").value),
    },
    queue: {
      ...state.settings.queue,
      timeout_seconds: Number(byId("queue-timeout").value),
    },
    routing: {
      ...state.settings.routing,
      prompt_directives: promptDirectives,
      local_pool: {
        ...(state.settings.routing.local_pool || {}),
        enabled: byId("local-pool-enabled").checked,
        recent_seconds: Number(byId("local-pool-recent").value) * 60,
      },
      conversation_stability: {
        ...state.settings.routing.conversation_stability,
        enabled: byId("stability-enabled").checked,
        health_failure_threshold: Number(
          byId("stability-failure-threshold").value,
        ),
        health_recheck_interval_seconds: Number(
          byId("stability-recheck-interval").value,
        ),
        recovery_mode: byId("stability-recovery-mode").value,
        preserve_tier_after_migration:
          byId("stability-preserve-tier").checked,
      },
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
  const draft = collectSettings();
  const errors = validateSettingsDraft(draft);
  if (errors.length) {
    notice(errors[0], true);
    return;
  }
  button.disabled = true;
  try {
    const currentDraft = state.policy?.draft;
    const payload = await api("/api/policy/draft", {
      method: "PATCH",
      body: JSON.stringify({
        changes: draft,
        expected_revision: currentDraft?.revision
          || state.policy?.active?.revision,
        expected_fingerprint: currentDraft?.settings_fingerprint
          || state.policy?.active?.settings_fingerprint,
      }),
    });
    state.policy = {
      ...state.policy,
      draft: payload.draft,
    };
    state.settings = mergeObjects(state.settings, payload.draft.settings);
    renderSettings();
    notice("策略草稿已保存，尚未影响运行中的新请求。");
  } catch (error) {
    notice(error.message, true);
  } finally {
    button.disabled = false;
  }
}

async function validatePolicyDraft() {
  const draft = state.policy?.draft;
  if (!draft) return;
  const button = byId("policy-validate");
  button.disabled = true;
  try {
    const payload = await api("/api/policy/draft/validate", {
      method: "POST",
      body: JSON.stringify({
        expected_revision: draft.revision,
        expected_fingerprint: draft.settings_fingerprint,
        limit: 100,
      }),
    });
    state.policy = {...state.policy, draft: payload.draft};
    renderPolicyState();
    notice("草稿验证通过，影响报告已更新。");
  } catch (error) {
    notice(error.message, true);
  } finally {
    button.disabled = false;
  }
}

async function activatePolicyDraft() {
  const draft = state.policy?.draft;
  if (!draft || draft.status !== "validated") return;
  if (!window.confirm(
    `激活策略 r${draft.revision}？变更只作用于之后的新一轮请求。`,
  )) return;
  const button = byId("policy-activate");
  button.disabled = true;
  try {
    await api("/api/policy/draft/activate", {
      method: "POST",
      body: JSON.stringify({
        expected_revision: draft.revision,
        expected_fingerprint: draft.settings_fingerprint,
      }),
    });
    await loadSettings();
    await loadDashboard(true);
    notice(
      lmcacheRestartRequired()
        ? "策略已激活；LMCache 目标变化仍需另行受控重启模型服务。"
        : "策略已激活，新请求将使用该版本。",
    );
  } catch (error) {
    notice(error.message, true);
  } finally {
    button.disabled = false;
  }
}

async function rollbackPolicyRevision() {
  const revision = Number(byId("policy-rollback-revision").value);
  if (!revision) return;
  const button = byId("policy-rollback");
  button.disabled = true;
  try {
    const payload = await api(
      `/api/policy/revisions/${revision}/rollback`,
      {
        method: "POST",
        body: JSON.stringify({
          expected_active_revision: state.policy?.active?.revision,
          expected_active_fingerprint:
            state.policy?.active?.settings_fingerprint,
        }),
      },
    );
    state.policy = {...state.policy, draft: payload.draft};
    state.settings = mergeObjects(
      state.settings,
      payload.draft.settings,
    );
    renderSettings();
    notice(`已从 r${revision} 创建回滚草稿，请重新验证后激活。`);
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
  if (view === "requests") void loadRequestTraces();
  if (view === "audit") return cacheView.view === "overview" ? loadCacheOverview() : loadRouteAudit();
  if (view === "clients") loadClients(true);
  if (view === "cache-deployments") void loadCacheDeployments();
}

function startPolling() {
  clearInterval(state.timer);
  if (!byId("auto-refresh").checked) return;
  state.timer = setInterval(() => {
    if (!document.hidden && state.key) {
      loadDashboard(true);
      if (state.view === "clients") loadClients(true);
      if (state.view === "cache-deployments") {
        void loadCacheDeployments(true);
      }
      if (state.view === "requests") loadRequestTraces(true);
      if (state.view === "audit") {
        if (cacheView.view === "overview") void loadCacheOverview();
        else loadRouteTraces(true);
      }
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
byId("refresh").addEventListener("click", () => {
  if (state.view === "requests") {
    void loadRequestTraces();
  } else if (state.view === "audit") {
    void loadRouteAudit();
  } else if (state.view === "cache-deployments") {
    void loadCacheDeployments();
  } else {
    void loadDashboard();
  }
});
byId("reload").addEventListener("click", async () => {
  try {
    await loadSettings();
    notice("设置已重新加载。");
  } catch (error) {
    notice(error.message, true);
  }
});
byId("settings-form").addEventListener("submit", saveSettings);
byId("policy-validate").addEventListener("click", () => {
  void validatePolicyDraft();
});
byId("policy-activate").addEventListener("click", () => {
  void activatePolicyDraft();
});
byId("policy-rollback").addEventListener("click", () => {
  void rollbackPolicyRevision();
});
document.querySelectorAll("[data-policy-section]").forEach((button) => {
  button.addEventListener("click", () => {
    byId(button.dataset.policySection)?.scrollIntoView({
      behavior: "smooth",
      block: "center",
    });
  });
});
byId("directive-random-all").addEventListener("click", () => {
  void randomizePromptDirectives(PROMPT_DIRECTIVES.map(([id]) => id));
});
byId("routing-strategy").addEventListener(
  "change",
  updateStrategyBranchVisibility,
);
byId("cache-deployment-anomalies").addEventListener("change", (event) => {
  state.cacheDeploymentAnomaliesOnly = event.target.checked;
  const visible = cacheDeploymentItems();
  if (
    !visible.some(({id}) => id === state.selectedCacheDeploymentId)
  ) {
    state.selectedCacheDeploymentId = visible[0]?.id || null;
    state.selectedCacheLayerId = null;
  }
  renderCacheDeployments();
});
document.querySelectorAll("[data-cache-deployment-tab]").forEach((button) => {
  button.addEventListener("click", () => {
    state.selectedCacheDeploymentTab =
      button.dataset.cacheDeploymentTab;
    const item = selectedCacheDeployment();
    if (item) renderCacheDeploymentInspector(item);
  });
});
byId("weights").addEventListener("input", updateWeightsTotal);
byId("review-backend").addEventListener("change", () => {
  if (byId("review-backend").value === "router") {
    byId("review-base-url").value = "http://127.0.0.1:4000";
  }
});
byId("auto-refresh").addEventListener("change", startPolling);
["node-filter", "status-filter"].forEach((id) => {
  byId(id).addEventListener("change", () => {
    resetRequestPagination();
    void loadRequestTraces();
  });
});
byId("request-search").addEventListener("input", () => {
  clearTimeout(requestSearchTimer);
  requestSearchTimer = setTimeout(() => {
    resetRequestPagination();
    void loadRequestTraces();
  }, 350);
});
byId("request-prev").addEventListener("click", () => {
  if (state.requestTracePage <= 1) return;
  state.requestTracePage -= 1;
  void loadRequestTraces();
});
byId("request-next").addEventListener("click", () => {
  if (!state.requestTraceNextCursor) return;
  state.requestTraceCursors[state.requestTracePage] =
    state.requestTraceNextCursor;
  state.requestTracePage += 1;
  void loadRequestTraces();
});
[
  "trace-mode-filter",
  "trace-review-filter",
  "trace-privacy-filter",
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
  byId("trace-privacy-filter").value = "";
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
byId("privacy-feedback-form").addEventListener("submit", submitPrivacyFeedback);
byId("trace-timeline-earlier").addEventListener("click", async () => {
  const conversationId = state.selectedTrace?.conversation_id;
  const cursor = state.requestConversationPages.get(conversationId)?.nextCursor;
  if (!cursor) return;
  const button = byId("trace-timeline-earlier");
  button.disabled = true;
  try {
    await loadTraceTimeline(conversationId, cursor);
  } finally {
    button.disabled = false;
  }
});
byId("trace-graph-mode-simple").addEventListener("click", () => {
  void setTraceGraphMode("simple");
});
byId("trace-graph-mode-detailed").addEventListener("click", () => {
  void setTraceGraphMode("detailed");
});
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
  traceGraphScale = Math.max(0.01, traceGraphScale - 0.1);
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
byId("client-disclosure-mode").addEventListener("change", (event) => {
  renderClientModels(
    event.target.value === "public"
      ? [publicIdentityModelId()]
      : ["auto"],
    event.target.value,
  );
});
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

initializeCacheAudit();
if (state.key) connect();
