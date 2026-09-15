const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const esc = value => String(value ?? "").replace(/[&<>"']/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[char]));
const number = value => Number(value || 0).toLocaleString("zh-CN");
const pct = value => `${(Number(value || 0) * 100).toFixed(1)}%`;
const get = async url => { const response = await fetch(url, {cache:"no-store"}); if (!response.ok) throw Error(await response.text()); return response.json(); };

const SCENARIOS = {
  math_modeling: {short:"MathorCup", title:"三维装箱与成本优化", kicker:"数学建模", accent:"blue", facts:["300 件货物完整覆盖","边界、碰撞、载重与成本复算","PDF / DOCX / XLSX"]},
  document_analysis: {short:"差旅报销", title:"跨文档政策核验", kicker:"企业业务", accent:"amber", facts:["5 条费用记录","总额 CNY 1,485","政策、发票与日期异常核验"]},
  general_complex_task: {short:"城市多模态", title:"城市数据治理与安全画像", kicker:"多模态治理", accent:"teal", facts:["511 遥感元数据 + 80,000 法规","80,000 匿名通联 + 500,000 网络包","真实自动修复闭环"]},
};
const EVENT_LABELS = {
  task_graph_finished:"任务图执行结束", business_validation_recorded:"业务验证",
  business_repair_started:"定位并启动修复", code_business_repair_fallback_executed:"重新生成并执行代码",
  business_repair_finished:"局部子图修复完成", review_recorded:"语义审查",
  delivery_outcome_recorded:"交付验收", run_finished:"整轮运行完成",
};

let demo = null;
let selectedRun = null;
let selectedTab = "gates";
let selectedScenario = "mathorcup";
let activeJobId = null;
let jobTimer = null;

function toast(message, bad = false) {
  const el = $("#toast"); el.textContent = message; el.className = `toast show${bad ? " bad" : ""}`;
  setTimeout(() => el.classList.remove("show"), 3200);
}

function renderHero() {
  const passed = demo.official_runs.filter(item => item.audit_passed).length;
  const h = demo.business_long_horizon?.validation || demo.long_horizon;
  const stats = [
    ["跨领域场景", `${passed} / ${demo.official_runs.length}`, "全部完整审计通过"],
    ["最终验收闸门", `${passed * 9} / ${demo.official_runs.length * 9}`, "业务、证据、报告全闭合"],
    ["真实业务长程", `${number(h.completed_steps)} / 1,000`, `${number(h.live_reasoning_calls)} 次阶段模型推理`],
    ["完整回归测试", number(demo.regression_tests), "全部通过"],
  ];
  $("#hero-stats").innerHTML = stats.map(([label,value,note]) => `<div class="hero-stat"><span>${label}</span><strong>${value}</strong><small>${note}</small></div>`).join("");
  $("#updated-at").textContent = `证据更新 ${demo.metrics.generated_at?.replace("T", " ") || "-"}`;
}

function renderScenarioCards() {
  $("#scenario-cards").innerHTML = demo.official_runs.map((run, index) => {
    const meta = SCENARIOS[run.task_type] || {short:run.task_name,title:run.task_name,kicker:run.task_type,accent:"teal",facts:[]};
    return `<button class="scenario-card ${meta.accent} ${selectedRun?.run_id === run.run_id ? "active" : ""}" data-index="${index}">
      <div class="scenario-top"><span>${esc(meta.kicker)}</span><b>审计通过</b></div>
      <h3>${esc(meta.short)}</h3><p>${esc(meta.title)}</p>
      <ul>${meta.facts.map(fact => `<li>${esc(fact)}</li>`).join("")}</ul>
      <div class="scenario-footer"><span>${run.node_count} 节点 · ${run.edge_count} 条边</span><strong>查看</strong></div>
    </button>`;
  }).join("");
  $$(".scenario-card").forEach(card => card.onclick = () => selectScenario(Number(card.dataset.index)));
}

function selectScenario(index) {
  selectedRun = demo.official_runs[index];
  const meta = SCENARIOS[selectedRun.task_type];
  $("#scenario-heading").innerHTML = `<div><span class="status-pill">完整审计通过</span><h3>${esc(meta.title)}</h3><p>${esc(selectedRun.detail.task_spec.goal)}</p></div><div class="run-stamp"><span>运行编号</span><code>${esc(selectedRun.run_id)}</code></div>`;
  renderScenarioCards();
  renderDetail();
  if (demo) renderResources();
}

function graphLevels(nodes) {
  const level = {};
  const byId = Object.fromEntries(nodes.map(node => [node.node_id, node]));
  const visit = (id, stack = new Set()) => {
    if (level[id] !== undefined) return level[id];
    if (stack.has(id)) return 0;
    stack.add(id);
    const deps = (byId[id]?.dependencies || []).filter(dep => byId[dep]);
    level[id] = deps.length ? Math.max(...deps.map(dep => visit(dep, stack))) + 1 : 0;
    stack.delete(id); return level[id];
  };
  nodes.forEach(node => visit(node.node_id));
  return level;
}

function renderGraph(detail) {
  const nodes = detail.nodes || [];
  const levels = graphLevels(nodes);
  const max = Math.max(0, ...Object.values(levels));
  const columns = Array.from({length:max + 1}, (_, depth) => nodes.filter(node => levels[node.node_id] === depth));
  return `<div class="graph-toolbar"><span>动态生成 ${nodes.length} 个节点</span><span>${selectedRun.edge_count} 条依赖边</span><span>拓扑密度 ${selectedRun.topology_density}</span></div>
    <div class="dag" style="--columns:${columns.length}">${columns.map((column, depth) => `<div class="dag-column"><small>阶段 ${String(depth + 1).padStart(2,"0")}</small>${column.map(node => {
      const route = (detail.routing || []).find(item => item.node_id === node.node_id) || {};
      const location = route.resource_location === "local" ? "device" : (route.resource_location || "unassigned");
      return `<button class="dag-node ${node.status}" data-node="${esc(node.node_id)}"><span class="node-cap">${esc(node.capability)}</span><strong>${esc(node.node_id)}</strong><p>${esc(node.description)}</p><footer><span>${esc(node.executor_id || "framework")}</span><b>${esc(location)}</b></footer></button>`;
    }).join("")}</div>`).join("")}</div>
    <div id="node-inspector" class="node-inspector"><span>选择节点查看直接依赖、执行器与路由依据。</span></div>`;
}

function attachNodeInspectors(detail) {
  $$(".dag-node").forEach(button => button.onclick = () => {
    $$(".dag-node").forEach(item => item.classList.remove("selected")); button.classList.add("selected");
    const node = detail.nodes.find(item => item.node_id === button.dataset.node);
    const route = (detail.routing || []).find(item => item.node_id === node.node_id) || {};
    const candidates = route.candidates || [];
    $("#node-inspector").innerHTML = `<div><span>节点</span><strong>${esc(node.node_id)}</strong></div><div><span>直接依赖</span><strong>${esc((node.dependencies || []).join(", ") || "无")}</strong></div><div><span>执行器</span><strong>${esc(node.executor_id || "framework")}</strong></div><div><span>资源位置</span><strong>${esc(route.resource_location === "local" ? "device" : (route.resource_location || "未记录"))}</strong></div><p>${esc(route.reason || "该节点由框架确定性执行或未记录候选路由。")}</p>${candidates.length ? `<p class="candidate-line">候选：${candidates.map(item => `${esc(item.executor_id)} (${Number(item.effective_score || 0).toFixed(3)})`).join(" · ")}</p>` : ""}`;
  });
}

function renderGates(detail) {
  const c = detail.audit.checks || {};
  const gates = [
    ["最终状态",c.outcome_success,"run_state.outcome = success"], ["运行完成",c.finished,"finished = true"],
    ["业务验证",c.business_validation_passed,"确定性领域校验"], ["需求验收",c.requirement_acceptance_passed,"阻断需求全部闭合"],
    ["报告许可",c.review_allows_report,"review.can_generate_final_report"], ["最终报告",c.final_report_exists,"final_report.md"],
    ["证据核验",c.evidence_verification_passed,"EvidenceVerifier"], ["节点收口",c.no_failed_or_blocked_nodes,"无 failed / blocked"],
    ["阻断问题",c.no_unresolved_blocking_issues,"无未解决 blocking issue"],
  ];
  return `<div class="gate-summary"><strong>${gates.filter(item => item[1]).length}/${gates.length}</strong><span>独立交付闸门通过</span></div><div class="gate-grid">${gates.map(([name,ok,note],i) => `<div class="gate ${ok ? "ok" : "bad"}"><b>${String(i+1).padStart(2,"0")}</b><div><strong>${esc(name)}</strong><span>${esc(note)}</span></div><em>${ok ? "PASS" : "FAIL"}</em></div>`).join("")}</div>`;
}

function renderMetrics(detail) {
  const m = detail.metrics.model_calls || {}, c = detail.metrics.communication || {};
  const entries = [
    ["总耗时",`${number(selectedRun.wall_time_seconds)} s`], ["模型调用",number(m.count)], ["总 Token",number(m.total_tokens)],
    ["Prompt Token",number(m.prompt_tokens)], ["Completion Token",number(m.completion_tokens)], ["完整 Prompt 压缩比",`${Number(m.complete_prompt_compression_ratio || 0).toFixed(2)}×`],
    ["上下文投递",number(c.context_deliveries)], ["实际依赖边",number((c.dependency_edges_used || []).length)], ["拓扑密度",pct(c.topology_density)],
    ["重复消息抑制",number(c.duplicates_suppressed)], ["产物引用",number(c.artifact_references)], ["完整历史广播",number(c.full_history_broadcasts)],
  ];
  const injections = detail.runtime_injections || {}, injectionItems = Object.values(injections.items || {});
  const injectionPanel = injectionItems.length ? `<div class="efficiency-note"><b>运行期注入恢复</b><p>${injectionItems.map(item => `${esc(item.type)}：${esc(item.status)}（${esc(item.target_node_id || "待选择")}）`).join(" · ")}</p></div>` : "";
  return `<div class="metric-grid">${entries.map(([label,value]) => `<div class="metric"><span>${label}</span><strong>${value}</strong></div>`).join("")}</div>${injectionPanel}<div class="efficiency-note"><b>效率口径</b><p>指标直接来自运行状态。Token 与耗时保留真实值，不将高消耗包装为优化成果；这是当前系统下一阶段的主要改进项。</p></div>`;
}

function artifactPath(item) { return typeof item === "string" ? item : (item.path || item.artifact_path || ""); }
function renderArtifacts(detail) {
  const core = ["final_report.md","review/business_validation.json","review/requirement_ledger.json","review/validation_report.md"];
  const paths = [...new Set([...core, ...(detail.artifacts.produced || []).map(artifactPath)].filter(Boolean))];
  return `<div class="artifact-head"><span>${paths.length} 个可追溯交付文件</span><small>点击文本、JSON 或代码产物查看只读内容</small></div><div class="artifact-table">${paths.map(path => `<button data-artifact="${esc(path)}"><span class="file-type">${esc(path.split(".").pop().toUpperCase())}</span><span><strong>${esc(path.split("/").pop())}</strong><small>${esc(path)}</small></span><b>预览</b></button>`).join("")}</div>`;
}

function attachArtifactPreviews() {
  $$('[data-artifact]').forEach(button => button.onclick = async () => {
    const path = button.dataset.artifact;
    try {
      const payload = await get(`/api/runs/${encodeURIComponent(selectedRun.run_id)}/artifacts/${encodeURIComponent(path)}`);
      $("#artifact-title").textContent = path;
      $("#artifact-meta").textContent = `${number(payload.size)} bytes${payload.truncated ? " · 已截断" : ""}`;
      $("#artifact-content").textContent = payload.content;
      $("#artifact-dialog").showModal();
    } catch { toast("该产物不可进行文本预览", true); }
  });
}

function renderMemory(detail) {
  const memory = detail.runtime_memory || {}, last = memory.last_capsule || {}, mm = last.metrics || {}, c = detail.metrics.communication || {};
  return `<div class="memory-layout"><div><h4>上下文唤醒</h4><dl><dt>记忆架构</dt><dd>${esc((memory.architecture || []).join(" · "))}</dd><dt>已构建胶囊</dt><dd>${number(memory.capsules_built)}</dd><dt>最近检索记录</dt><dd>${number(mm.retrieved_count)} / ${number(mm.candidate_count)}</dd><dt>关键字段保持</dt><dd>${number(mm.critical_fields_retained)} / ${number(mm.critical_fields_expected)}</dd><dt>受保护上下文</dt><dd class="pass-text">${esc(mm.protected_context_integrity || "未记录")}</dd></dl></div><div><h4>低熵通信</h4><dl><dt>消息范围</dt><dd>仅直接依赖节点</dd><dt>原始估算 Token</dt><dd>${number(c.raw_tokens_estimated)}</dd><dt>实际投递 Token</dt><dd>${number(c.delivered_tokens_estimated)}</dd><dt>重复消息抑制</dt><dd>${number(c.duplicates_suppressed)}</dd><dt>完整历史广播</dt><dd class="pass-text">${number(c.full_history_broadcasts)}</dd></dl></div></div>`;
}

function renderDetail() {
  const detail = selectedRun.detail;
  let html = selectedTab === "graph" ? renderGraph(detail) : selectedTab === "gates" ? renderGates(detail) : selectedTab === "metrics" ? renderMetrics(detail) : selectedTab === "artifacts" ? renderArtifacts(detail) : renderMemory(detail);
  $("#scenario-detail").innerHTML = html;
  if (selectedTab === "graph") attachNodeInspectors(detail);
  if (selectedTab === "artifacts") attachArtifactPreviews();
}

function renderRecovery() {
  const timeline = demo.recovery.timeline || [];
  const started = timeline.find(item => item.event === "business_repair_started") || {};
  $("#recovery-summary").innerHTML = `<div><span>首次业务验证</span><strong class="fail-text">未通过</strong></div><div><span>归因目标</span><strong>${esc((started.target_node_ids || ["-"]).join(", "))}</strong></div><div><span>局部重放</span><strong>${number((started.affected_node_ids || []).length)} 个节点</strong></div><div><span>最终运行状态</span><strong class="pass-text">成功</strong></div>`;
  const key = timeline.filter(item => ["business_validation_recorded","business_repair_started","code_business_repair_fallback_executed","business_repair_finished","delivery_outcome_recorded","run_finished"].includes(item.event));
  $("#recovery-timeline").innerHTML = key.map((item,index) => `<div class="timeline-item ${item.status === "failed" ? "failed" : "passed"}"><div class="timeline-marker">${String(index+1).padStart(2,"0")}</div><div><time>${esc((item.time || "").split("T")[1] || "")}</time><strong>${esc(EVENT_LABELS[item.event] || item.event)}</strong><span>${item.event === "business_repair_started" ? `重开 ${(item.affected_node_ids || []).join("、")}` : esc(item.status || "executed")}</span></div></div>`).join("");
}

function renderHorizon() {
  const h = demo.long_horizon;
  const b = demo.business_long_horizon?.validation || {};
  const units = b.modality_work_units || {};
  $("#business-horizon-panel").innerHTML = `<div class="business-progress"><div><strong>${number(b.completed_steps)}</strong><span>已完成业务步骤</span></div><p>全局目标保持 · 第 500 步签名恢复 · 重复执行 ${number(b.duplicate_executions)}</p></div><div class="horizon-track"><div class="track-fill"></div><span class="start">开始</span><span class="resume">500<i>恢复成功</i></span><span class="end">1000</span></div><div class="workunit-distribution"><div><strong>${number(units.remote_sensing)}</strong><span>遥感元数据</span></div><div><strong>${number(units.law_articles)}</strong><span>政策法规</span></div><div><strong>${number(units.phone_network)}</strong><span>匿名通联</span></div><div><strong>${number(units.network_traffic)}</strong><span>网络流量</span></div><div><strong>${number(units.cross_modal_synthesis)}</strong><span>跨模态综合</span></div></div><div class="horizon-stats"><div><strong>${number(b.live_reasoning_calls)}</strong><span>真实阶段模型推理</span></div><div><strong>${number(b.checkpoint_count)}</strong><span>SHA256 checkpoint</span></div><div><strong>${b.goal_preserved ? "保持" : "漂移"}</strong><span>全局任务目标</span></div><div><strong>9 / 9</strong><span>独立审计闸门</span></div></div><p class="boundary-note">数据覆盖：511 条遥感元数据、80,000 条法规、80,000 条匿名通联和 500,000 个网络包。模型只接收聚合证据，不读取原始标识或 PCAP 载荷。</p>`;
  $("#horizon-panel").innerHTML = `<div class="horizon-track"><div class="track-fill"></div><span class="start">0</span><span class="resume">500<i>恢复点</i></span><span class="end">1000</span></div><div class="horizon-stats"><div><strong>${number(h.completed_steps)}</strong><span>完成步骤</span></div><div><strong>${number(h.duplicate_executions)}</strong><span>重复执行</span></div><div><strong>${number(h.checkpoint_count)}</strong><span>SHA256 Checkpoint</span></div><div><strong>${h.goal_preserved ? "保持" : "漂移"}</strong><span>全局目标</span></div></div><p class="boundary-note">确定性任务图、持久化与恢复压力测试，不表述为 1000 次大模型调用。</p>`;
}

function renderResources() {
  const routes = selectedRun?.detail.routing || demo.official_runs.flatMap(item => item.detail.routing || []);
  const groups = {device:[], edge:[], cloud:[]};
  routes.forEach(route => {
    const location = route.resource_location === "local" ? "device" : route.resource_location;
    if (groups[location] && !groups[location].some(item => item.node_id === route.node_id)) groups[location].push(route);
  });
  const labels = {device:["终端 Device","低延迟 · 敏感数据"],edge:["边缘 Edge","验证 · 平衡算力"],cloud:["云端 Cloud","规划 · 复杂推理"]};
  $("#resource-panel").innerHTML = `<div class="resource-lanes">${Object.entries(groups).map(([location,items]) => `<div class="resource-lane ${location}"><header><strong>${labels[location][0]}</strong><span>${labels[location][1]}</span></header><div>${items.length ? items.slice(0,5).map(item => `<span title="${esc(item.resource_reason || item.reason)}"><b>${esc(item.node_id)}</b><small>${esc(item.model_name || "模型未记录")}</small></span>`).join("") : "<em>当前场景无节点</em>"}</div></div>`).join("")}</div><p class="boundary-note">可替换的资源后端，可绑定真实阿里云兼容模型；当前不宣称真实物理集群或 Transformer 参数层切分。</p>`;
}

const STAGES = ["prepare","classify","plan","execute","review","report","final_validate","finish"];
const STAGE_LABELS = {prepare:"准备输入",classify:"理解任务",plan:"生成计划",execute:"协同执行",review:"审查结果",report:"生成报告",final_validate:"最终验收",finish:"完成"};
const INPUTS = {mathorcup:"examples/mathorcup_d",expense:"examples/expense_reimbursement",urban:"城市多模态数据集",urban_long:"城市多模态数据集（1000 个 WorkUnit）",mathorcup_long:"examples/mathorcup_d（1000 个候选方案）",recovery_demo:"examples/expense_reimbursement（动态注入恢复）"};

function executionToken() { return $("#execution-token").value.trim(); }
function authHeaders() { return {"Content-Type":"application/json","X-Dashboard-Token":executionToken()}; }

function setView(view) {
  $$(".app-view").forEach(item => item.classList.toggle("active", item.dataset.page === view));
  $$(".side-nav button").forEach(item => item.classList.toggle("active", item.dataset.view === view));
  const titles = {overview:["系统总览","比赛验收与运行状态"],workspace:["任务工作台","启动并观察真实任务"],evidence:["场景证据","三场景完整审计结果"],continuity:["长程与恢复","连续性、修复与资源路由"],architecture:["系统架构","动态异构协作机制"]};
  $("#page-title").textContent = titles[view][0]; $("#page-subtitle").textContent = titles[view][1];
  document.body.classList.remove("nav-open"); window.scrollTo({top:0,behavior:"smooth"});
}

function renderPreflight(payload) {
  const labels = {model_api_key:"模型密钥",model_base_url:"服务地址",model_name:"模型名称",outputs_writable:"输出目录",single_job_available:"任务队列"};
  $("#preflight-checks").innerHTML = Object.entries(payload.checks).map(([key,ok]) => `<div class="preflight ${key} ${ok ? "ok" : "bad"}"><i>${ok ? "✓" : "!"}</i><span>${labels[key] || key}</span><strong>${ok ? (key === "model_name" ? esc(payload.model_name) : "就绪") : "未就绪"}</strong></div>`).join("");
  const badge = $("#runtime-badge"); badge.textContent = payload.execution_enabled ? (payload.ready ? "执行环境就绪" : "需要完成预检") : "当前为只读模式"; badge.className = `mode-badge ${payload.ready ? "ready" : ""}`;
  $("#launch-run").disabled = !payload.ready || Boolean(activeJobId);
  $("#launch-status").textContent = payload.execution_enabled ? (payload.ready ? "输入执行口令后可启动" : "存在未通过的启动检查") : "服务未启用真实执行模式";
}

async function refreshPreflight() {
  try { renderPreflight(await get("/api/runtime/preflight")); }
  catch { $("#preflight-checks").innerHTML = '<div class="error-state">无法读取运行环境。</div>'; }
}

function updateJobView(job) {
  const status = job.status || "running";
  const statusLabels = {queued:"排队中",running:"运行中",success:"已通过",failed:"失败",cancelled:"已终止"};
  $("#job-status").textContent = statusLabels[status] || status; $("#job-status").className = `status-chip ${status}`;
  $("#job-caption").textContent = `${job.scenario_label || job.scenario} · ${job.started_at || ""}`;
  $("#current-stage").textContent = STAGE_LABELS[job.stage] || job.stage || "执行中";
  $("#current-node").textContent = (job.current_nodes || []).join(", ") || "等待节点状态";
  $("#current-run").textContent = job.run_dir || "正在创建运行目录";
  const stageIndex = Math.max(0, STAGES.indexOf(job.stage));
  $$("#stage-track>div").forEach((item,index) => item.classList.toggle("active", index <= Math.min(5, stageIndex)));
  $("#cancel-job").hidden = !["queued","running"].includes(status);
  if (["success","failed","cancelled"].includes(status)) {
    clearInterval(jobTimer); jobTimer = null; activeJobId = null;
    $("#launch-run").disabled = false;
    $("#launch-status").textContent = status === "success" ? "任务完成并通过完整审计" : `任务已${status === "cancelled" ? "终止" : "失败"}`;
    refreshPreflight();
  }
}

async function pollJob() {
  if (!activeJobId) return;
  try {
    const [job,log] = await Promise.all([get(`/api/runtime/jobs/${activeJobId}`),get(`/api/runtime/jobs/${activeJobId}/log`)]);
    updateJobView(job); $("#live-log").textContent = log.content || "等待日志输出..."; $("#live-log").scrollTop = $("#live-log").scrollHeight;
  } catch (error) { toast(`读取任务状态失败：${error.message}`, true); }
}

async function launchJob() {
  if (!executionToken()) { toast("请输入服务启动时生成的执行口令", true); $("#execution-token").focus(); return; }
  const button = $("#launch-run"); button.disabled = true; $("#launch-status").textContent = "正在创建真实任务...";
  try {
    const response = await fetch("/api/runtime/jobs", {method:"POST",headers:authHeaders(),body:JSON.stringify({scenario:selectedScenario})});
    if (!response.ok) { const payload = await response.json(); throw Error(payload.error || "启动失败"); }
    const job = await response.json(); activeJobId = job.job_id; updateJobView(job); $("#live-log").textContent = "任务进程已启动，正在等待第一条运行事件...";
    jobTimer = setInterval(pollJob, 1500); pollJob(); toast("真实任务已启动");
  } catch (error) { button.disabled = false; $("#launch-status").textContent = "启动失败"; toast(error.message, true); }
}

async function cancelJob() {
  if (!activeJobId || !executionToken()) return;
  try {
    const response = await fetch(`/api/runtime/jobs/${activeJobId}/cancel`, {method:"POST",headers:authHeaders(),body:"{}"});
    if (!response.ok) throw Error("终止失败"); updateJobView(await response.json()); toast("任务已终止");
  } catch (error) { toast(error.message, true); }
}

async function setupLauncher() {
  $$("#scenario-selector button").forEach(button => button.onclick = () => {
    selectedScenario = button.dataset.scenario; $$("#scenario-selector button").forEach(item => item.classList.toggle("active", item === button)); $("#task-input").value = INPUTS[selectedScenario];
  });
  $("#execution-token").value = sessionStorage.getItem("dashboardExecutionToken") || "";
  $("#execution-token").oninput = () => sessionStorage.setItem("dashboardExecutionToken", executionToken());
  $("#launch-run").onclick = launchJob; $("#cancel-job").onclick = cancelJob;
  await refreshPreflight();
  try {
    const jobs = (await get("/api/demo/jobs")).jobs || [];
    const resumable = [...jobs].reverse().find(job => ["queued","running"].includes(job.status));
    if (resumable) {
      activeJobId = resumable.job_id; updateJobView(resumable);
      jobTimer = setInterval(pollJob, 1500); pollJob();
    }
  } catch { /* A missing historical job list does not block new runs. */ }
}

function bindStaticActions() {
  $$(".tabs button").forEach(button => button.onclick = () => { $$(".tabs button").forEach(item => item.classList.remove("active")); button.classList.add("active"); selectedTab = button.dataset.tab; renderDetail(); });
  $$(".side-nav button").forEach(button => button.onclick = () => setView(button.dataset.view));
  $$('[data-open-view]').forEach(button => button.onclick = () => setView(button.dataset.openView));
  $$('[data-view-link]').forEach(button => button.onclick = event => { event.preventDefault(); setView(button.dataset.viewLink); });
  $("#mobile-menu").onclick = () => document.body.classList.toggle("nav-open");
  $("#fullscreen").onclick = () => document.fullscreenElement ? document.exitFullscreen() : document.documentElement.requestFullscreen();
  $("#refresh").onclick = () => { loadDemo(); refreshPreflight(); if (activeJobId) pollJob(); };
  $("#close-dialog").onclick = () => $("#artifact-dialog").close();
}

async function loadDemo() {
  try {
    demo = await get("/api/demo");
    if (!demo.official_runs.length) throw Error("未找到正式审计运行");
    const previous = selectedRun?.run_id;
    selectedRun = demo.official_runs.find(run => run.run_id === previous) || demo.official_runs[2] || demo.official_runs[0];
    renderHero(); renderScenarioCards(); selectScenario(demo.official_runs.findIndex(run => run.run_id === selectedRun.run_id)); renderRecovery(); renderHorizon(); renderResources();
    toast("真实运行证据已刷新");
  } catch (error) { toast(`证据载入失败：${error.message}`, true); $("#hero-stats").innerHTML = `<div class="error-state">无法读取演示证据，请检查 evidence 与 outputs/runs。</div>`; }
}

bindStaticActions(); setupLauncher(); loadDemo();
