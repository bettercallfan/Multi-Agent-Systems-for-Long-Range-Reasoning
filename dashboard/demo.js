"use strict";
const $ = selector => document.querySelector(selector);
const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const M = window.DemoModel;
const stages = {prepare:"准备",classify:"理解",plan:"规划",execute:"执行",review:"审查",report:"报告",final_validate:"验收",finish:"完成"};
const capabilities = {analysis:"分析推理",data_analysis:"数据分析",document_extraction:"信息提取",document_conversion:"文档转换",file_navigation:"文件读取",code_generation:"代码生成",code_execution:"代码执行",coding:"代码处理",evidence_verification:"证据核验",artifact_validation:"产物验证",terminal_execution:"终端复核",reporting:"报告生成",math_modeling:"数学建模",reasoning:"专业推理",research:"研究分析"};
const gateLabels = {outcome_success:"最终状态",finished:"运行完成",business_validation_passed:"业务验证",requirement_acceptance_passed:"需求验收",review_allows_report:"报告许可",final_report_exists:"最终报告",evidence_verification_passed:"证据核验",no_failed_or_blocked_nodes:"节点收口",no_unresolved_blocking_issues:"阻断清零"};
Object.assign(capabilities,{input_normalization:"输入标准化",calculation:"计算与核验",code:"代码生成与执行"});
let detail = null, view = null, cursor = 0, mode = "replay", timer = null, job = null, selected = null;
let runId = null, catalog = [], executionEnabled = false, loading = 0, polling = false, chapters = [];
async function api(path, body) {
  const res = await fetch(path, body ? {method:"POST",headers:{"Content-Type":"application/json","X-Dashboard-Token":$("#token").value},body:JSON.stringify(body)} : {});
  const data = await res.json(); if (!res.ok) throw new Error(data.error || `服务返回 ${res.status}`); return data;
}
function notice(text = "") { $("#notice").textContent = text; }
function stop() { clearInterval(timer); timer = null; $("#play").textContent = "▶ 自动播放"; }
function showDialog(title, html) { $("#dialog-title").textContent = title; $("#dialog-body").innerHTML = html; if (!$("#detail-dialog").open) $("#detail-dialog").showModal(); }
function rawDialog(title, data) { showDialog(title, `<pre>${esc(JSON.stringify(data,null,2))}</pre>`); }
async function artifact(path) {
  try {
    const data = await api(`/api/runs/${encodeURIComponent(runId)}/artifacts/${encodeURIComponent(path)}`);
    showDialog(path.split("/").at(-1), `<p class="muted">${esc(runId)} / ${esc(data.path)}${data.truncated ? " · 预览已截断，可下载完整文件" : ""}</p><a class="text-button" href="/api/runs/${encodeURIComponent(runId)}/download/${encodeURIComponent(path)}" download>下载原文件 ↓</a><div class="report-text">${esc(data.content)}</div>`);
  } catch (e) { notice(e.message); }
}
function chapterList() {
  const events = M.eventsFor(detail), find = predicate => events.findIndex(predicate);
  const last = find(e => e.type === "run_finished");
  return [
    ["任务意图",0,"理解目标，形成交付约束","用户提供跨文档审查目标，系统读取输入材料并建立结构化任务契约。"],
    ["协作规划",find(e => e.type === "task_graph_recorded"),"任务驱动的异构协作","从任务依赖组织执行节点，通过能力匹配和历史表现选择执行单元。"],
    ["节点失效",find(e => e.type === "runtime_injection_triggered" && e.payload.type === "node_failure"),"节点失效后继续推进","查看执行失败、恢复决策和实际重试。一次受控失效由主调度器处理。"],
    ["需求变更",find(e => e.type === "runtime_injection_triggered" && e.payload.type === "requirement_change"),"运行中更新待执行契约","保留已完成工作，更新目标节点要求并递增任务图版本。验收仍在执行后进行。"],
    ["数据异常",find(e => e.type === "runtime_injection_triggered" && e.payload.type === "data_anomaly"),"检测完整性变化，恢复可信输入","以 SHA256 检测输入副本变化，隔离异常数据、恢复备份并重新执行。"],
    ["可信交付",last,"交付结果与独立验收","查看业务结果、需求覆盖和九项审计。未闭合的运行不会被展示为成功。"],
  ];
}
async function loadRun(id) {
  const request = ++loading; stop(); notice();
  try {
    const loaded = await api(`/api/runs/${encodeURIComponent(id)}`);
    if (request !== loading) return;
    detail = loaded; runId = id; selected = null; cursor = 0; chapters = chapterList(); render();
    $("#connection").textContent = `证据已连接 · ${id}`;
  } catch(e) { if(request === loading) notice(e.message); }
}
function renderGraph() {
  const nodes = detail.nodes || [], depth = {}, byId = Object.fromEntries(nodes.map(n => [n.node_id,n]));
  function level(id, seen = new Set()) {
    if (depth[id] !== undefined) return depth[id];
    if (seen.has(id)) return 0;
    const visited = new Set(seen); visited.add(id);
    const deps = (byId[id]?.dependencies || []).filter(d=>byId[d]);
    return depth[id] = deps.length ? 1 + Math.max(...deps.map(d=>level(d,visited))) : 0;
  }
  nodes.forEach(n=>level(n.node_id));
  const groups = {}; nodes.forEach(n=>(groups[depth[n.node_id]] ||= []).push(n));
  const lanes = Math.max(2,...Object.values(groups).map(g=>g.length)), width = lanes * 220 + 20;
  const height = Math.max(270,(Math.max(0,...Object.values(depth))+1)*92+30), positions = {};
  Object.entries(groups).forEach(([rank,group]) => group.forEach((n,i) => {
    positions[n.node_id] = {x:width/2-group.length*220/2+i*220+12,y:Number(rank)*92+16};
  }));
  const lines = nodes.flatMap(n=>(n.dependencies||[]).filter(d=>positions[d]).map(d=>{
    const a=positions[d], b=positions[n.node_id], done=view.nodes[d]?.status==="completed";
    return `<path class="edge ${done?"done":""}" marker-end="url(#arrow)" d="M${a.x+98},${a.y+60} C${a.x+98},${a.y+80} ${b.x+98},${b.y-20} ${b.x+98},${b.y}"/>`;
  })).join("");
  const active=selected || view.currentNode;
  const cards=nodes.map(n=>{
    const p=positions[n.node_id], state=view.nodes[n.node_id] || {status:"future"};
    const route=view.routes[n.node_id], unit=state.executor || route?.selected_executor_id || "等待路由";
    return `<g class="node ${esc(state.status)} ${active===n.node_id?"selected":""}" transform="translate(${p.x},${p.y})" data-node="${esc(n.node_id)}" tabindex="0" role="button" aria-label="${esc(n.description)}"><title>${esc(n.node_id)} · ${esc(n.description)}</title><rect width="196" height="60"/><text class="node-caption" x="12" y="22">${esc(capabilities[n.capability] || n.capability || "执行节点")}</text><text class="node-sub" x="12" y="40">${esc(unit.length>26?unit.slice(0,24)+"…":unit)}</text><text class="node-sub" x="176" y="22" text-anchor="end">${state.attempts?"#"+state.attempts:""}</text></g>`;
  }).join("");
  $("#graph").setAttribute("viewBox",`0 0 ${width} ${height}`);
  $("#graph").innerHTML=`<defs><marker id="arrow" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="5" markerHeight="5" orient="auto"><path d="M0 0 L10 5 L0 10" fill="#a3b6c3"/></marker></defs>${lines}${cards}${!nodes.length?'<text x="30" y="60" fill="#697b8e">当前记录尚无可展示任务图</text>':""}`;
  $("#graph").querySelectorAll("[data-node]").forEach(el=>{
    const choose=()=>{selected=el.dataset.node;renderGraph();renderInspector();};
    el.onclick=choose; el.onkeydown=e=>{if(e.key==="Enter" || e.key===" "){e.preventDefault();choose();}};
  });
  if(active && positions[active] && !selected) {
    const viewport=$("#graph-viewport"), scale=$("#graph").getBoundingClientRect().width/width;
    const y=positions[active].y*scale;
    if(y<viewport.scrollTop || y>viewport.scrollTop+viewport.clientHeight-65) viewport.scrollTop=Math.max(0,y-120);
  }
}
function renderInspector() {
  const id=selected || view.currentNode, node=(detail.nodes||[]).find(n=>n.node_id===id);
  if(!node){$("#node-inspector").innerHTML="<p>点击节点查看职责、执行单元与路由依据。</p>";return;}
  const route=view.routes[id], runtime=view.nodes[id]||{};
  $("#node-inspector").innerHTML=`<h3>${esc(id)}</h3><p>${esc(node.description)}</p><div class="inspector-facts"><span>尝试 ${runtime.attempts||0} 次</span><span>${esc(runtime.executor||route?.selected_executor_id||"等待路由")}</span><span>${esc(route?.resource_location||"资源尚未记录")}</span></div><p>${esc(route?.reason||"当前事件前尚无该节点的路由决策。节点描述来自保存的任务图。")}</p>${route?'<button id="route-detail" class="text-button">查看候选评分与依据 ↗</button>':""}`;
  if($("#route-detail"))$("#route-detail").onclick=()=>rawDialog("路由决策与候选评分",route);
}
function eventDescription(e) {
  const p=e.payload||{}, type=M.types[p.type];
  return [type,p.node_id,p.action,p.successful_attempt?`第 ${p.successful_attempt} 次执行成功`:"",p.new_graph_version?`图版本 → v${p.new_graph_version}`:"",p.status,p.reason].filter(Boolean).join(" · ") || stages[p.stage] || "框架事件已持久化";
}
function renderInjections() {
  $("#injections").innerHTML=Object.entries(M.types).map(([type,label],i)=>{
    const item=Object.values(view.injections).filter(x=>x.type===type).at(-1), phase=item?.phase;
    const available=view.events.findIndex(e=>e.type==="runtime_injection_triggered" && e.payload.type===type);
    const text=type==="node_failure"?"受控节点执行失败 → 恢复决策 → 实际重试":type==="requirement_change"?"更新待执行节点契约 → 图版本递增 → 后续验收":"副本哈希异常 → 隔离与恢复 → 节点重试";
    const steps=type==="requirement_change"?["triggered","recovery_started","recovered"]:["triggered","detected","recovery_started","recovered"];
    const recoveryLabel=phase==="recovered" && type==="requirement_change"?"契约已更新":M.phases[phase]||"尚未触发";
    return `<article class="injection-card ${phase==="recovered"?"recovered":phase&&phase!=="armed"?"active":""}"><div class="injection-top"><h3><span class="muted">0${i+1}</span> ${label}</h3><span class="tag">${recoveryLabel}</span></div><p>${esc(item?.payload.node_id || text)}</p><div class="injection-steps">${steps.map(s=>`<span class="${item?.history.includes(s)?"done":""}">${type==="requirement_change"&&s==="recovered"?"已更新":M.phases[s]}</span>`).join("<small>→</small>")}</div><div class="injection-actions">${mode==="live"?`<button class="text-button" data-inject="${type}" ${!canInject()?"disabled":""}>注入${label} ↗</button>`:`<button class="text-button" data-jump="${available}" ${available<0?"disabled":""}>定位注入事件 →</button>`}<button class="text-button" data-evidence="${type}" ${!item || phase==="armed"?"disabled":""}>查看恢复证据</button></div></article>`;
  }).join("");
  document.querySelectorAll("[data-jump]").forEach(b=>b.onclick=()=>{stop();cursor=Number(b.dataset.jump);selected=null;render();});
  document.querySelectorAll("[data-inject]").forEach(b=>b.onclick=()=>inject(b.dataset.inject,b));
  document.querySelectorAll("[data-evidence]").forEach(b=>b.onclick=()=>{
    const item=Object.values(view.injections).filter(x=>x.type===b.dataset.evidence).at(-1);
    const evidence=detail.injection_evidence?.[item.id]||{};
    // Only reveal evidence recorded up to this replay position.
    const payload=item.phase==="recovered"?evidence:item.payload;
    const rows = [
      ["目标节点",payload.target_node_id||payload.node_id],
      ["恢复动作",({retry_or_switch_executor:"重试或切换执行单元",amend_pending_node_contract:"更新待执行节点契约",quarantine_and_restore_then_retry:"隔离异常副本、恢复可信输入并重试"})[payload.recovery_action]||payload.action],
      ["成功尝试",payload.successful_attempt?`第 ${payload.successful_attempt} 次`:null],
      ["实际执行单元",payload.selected_executor_id],
      ["原始要求",payload.old_description], ["更新后要求",payload.new_description||payload.change_text],
      ["任务图版本",payload.old_graph_version!==undefined?`v${payload.old_graph_version} → v${payload.new_graph_version}`:null],
      ["保留的已完成节点",payload.completed_nodes_preserved?.join("、")],
      ["原始 SHA256",payload.original_sha256||payload.expected_sha256],
      ["异常 SHA256",payload.corrupted_sha256||payload.actual_sha256],
      ["恢复 SHA256",payload.restored_sha256], ["隔离副本",payload.quarantine_ref],
    ].filter(([,value])=>value!==undefined && value!==null && value!=="");
    showDialog(`${M.types[item.type]} · ${M.phases[item.phase]}`,`<p class="muted">${esc(runId)} · ${esc(item.id)}</p><div class="injection-steps">${item.history.map(s=>`<span class="done">${M.phases[s]||esc(s)}</span>`).join(" → ")}</div>${rows.map(([label,value])=>`<div class="evidence-pair"><span>${label}</span><div>${esc(value)}</div></div>`).join("")}<details><summary>原始证据字段</summary><pre>${esc(JSON.stringify(payload,null,2))}</pre></details>`);
  });
  const target=$("#target-node"), prior=target.value;
  target.innerHTML='<option value="">自动选择下一个业务节点</option>'+(detail.nodes||[]).filter(n=>!["completed","failed","blocked","running"].includes(n.status)&& !["artifact_validation","evidence_verification","reporting","terminal_execution"].includes(n.capability)).map(n=>`<option value="${esc(n.node_id)}">${esc(n.node_id)}</option>`).join("");
  if([...target.options].some(o=>o.value===prior))target.value=prior;
  $("#queue-status").textContent=mode==="live"?(detail.injection_queue?.requests||[]).map(r=>`${M.types[r.type]}：${({queued:"等待调度边界",accepted:"调度器已接收",rejected:"未应用"})[r.status]||r.status}${r.reason?"（"+r.reason+"）":""}`).join(" ｜ "):"";
}
function canInject(){return mode==="live" && job?.status==="running" && detail?.accept_runtime_injections && !detail?.summary.finished && !detail?.injection_queue?.closed;}
async function inject(type,button){
  button.disabled=true;
  try{await api(`/api/runtime/jobs/${job.job_id}/injections`,{type,target_node_id:$("#target-node").value||null,change_text:$("#change-text").value});notice(`${M.types[type]}已提交；等待调度器确认。恢复过程无需人工操作。`);await pollLive();}
  catch(e){notice(e.message);}finally{button.disabled=!canInject();}
}
function renderDelivery(){
  const closed=mode==="live"?detail.summary.finished:view.finished;
  const checks=detail.audit?.checks||{};
  $("#gates").innerHTML=Object.entries(gateLabels).map(([key,label])=>`<div class="gate ${closed?(checks[key]?"pass":"fail"):""}">${label}<span>${closed?(checks[key]?"PASS":"FAIL"):"等待验收"}</span></div>`).join("");
  $("#stat-audit").textContent=closed?`${Object.keys(gateLabels).filter(k=>checks[k]).length}/9`:"等待";
  $("#stat-audit-note").textContent=closed?(detail.audit?.passed?"独立审计通过":"存在未闭合项"):"最终独立审计";
  $("#delivery-button").disabled=!closed || !checks.final_report_exists;
  if(!closed){$("#delivery-content").textContent="工作流尚未推进至最终验收。完成后在此查看报告、业务结果及证据。";return;}
  const artifacts=["final_report.md","review/business_validation.json","review/requirement_ledger.json",...(detail.artifacts?.produced||[]).map(a=>typeof a==="string"?a:a.path||a.artifact_path)].filter(Boolean);
  const paths=[...new Set(artifacts)].filter(p=>/\.(md|json|csv|txt)$/.test(p));
  const injectionAudit=detail.injection_audit||{}, injectionChecks=Object.values(injectionAudit.checks||{});
  const extra=injectionChecks.length?` · 注入独立审计 ${injectionChecks.filter(Boolean).length}/${injectionChecks.length} ${injectionAudit.passed?"PASS":"FAIL"}`:"";
  $("#delivery-content").innerHTML=`<p>${detail.audit?.passed?"运行已闭合。最终状态来自持久化记录和独立审计。":"运行有未完成的验收项，请查看原始证据。"}${extra}</p>${paths.slice(0,10).map(p=>`<button class="artifact" data-artifact="${esc(p)}">${esc(p.split("/").at(-1))} ↗</button>`).join("")}`;
  document.querySelectorAll("[data-artifact]").forEach(b=>b.onclick=()=>artifact(b.dataset.artifact));
}
function render(){
  if(!detail)return;
  view=M.project(detail,mode==="live"?M.eventsFor(detail).length-1:cursor);
  const spec=detail.task_spec||{};
  $("#task-title").textContent=spec.task_name||"跨文档差旅审查";$("#task-goal").textContent=spec.goal||"读取任务输入，生成可核验的最终交付物。";
  $("#source-run").textContent=runId;
  $("#input-files").innerHTML=(detail.input_files||[]).map(p=>`<div class="file-item"><b>${esc(p.split(".").at(-1).toUpperCase())}</b>${esc(p)}</div>`).join("")||'<span class="muted">当前记录未提供输入清单</span>';
  const deliverables=(spec.required_artifacts||detail.artifacts?.required||[]).filter(p=>typeof p!=="string"||p.startsWith("artifacts/")||p==="final_report.md");
  $("#requirements").innerHTML=deliverables.map(p=>`<div class="file-item">${esc(typeof p==="string"?p:JSON.stringify(p))}</div>`).join("")||'<span class="muted">等待需求契约生成</span>';
  $("#graph-version").textContent=view.version?`v${view.version}`:"未记录";$("#current-stage").textContent=stages[view.stage]||view.stage;
  const runtimeNodes=mode==="live"?(detail.nodes||[]):Object.values(view.nodes);
  $("#stat-progress").textContent=`${runtimeNodes.filter(n=>n.status==="completed").length} / ${(detail.nodes||[]).length}`;
  $("#stat-agents").textContent=new Set(Object.values(view.routes).map(r=>r.selected_executor_id).filter(Boolean)).size;
  const items=Object.values(view.injections), triggered=items.filter(i=>i.phase!=="armed");
  $("#stat-recovery").textContent=`${items.filter(i=>i.phase==="recovered").length} / ${triggered.length}`;
  $("#scrubber").max=Math.max(0,view.events.length-1);$("#scrubber").value=view.cursor;
  $("#event-clock").textContent=view.current?`${view.current.time} · 事件 ${view.current.sequence} · ${view.cursor+1}/${view.events.length}`:"等待首个运行事件";
  chapters=chapterList();let active=0,nearest=-1;chapters.forEach((c,i)=>{if(c[1]>=0 && view.cursor>=c[1] && c[1]>=nearest){active=i;nearest=c[1];}});
  $("#chapter-number").textContent=`0${active+1} / 06`;$("#chapter-title").textContent=chapters[active][2];$("#chapter-copy").textContent=chapters[active][3];
  $("#chapters").innerHTML=chapters.map((c,i)=>`<button data-chapter="${i}" class="${i===active?"active":""}" ${c[1]<0||mode==="live"?"disabled":""}>0${i+1} ${c[0]}</button>`).join("");
  document.querySelectorAll("[data-chapter]").forEach(b=>b.onclick=()=>{stop();cursor=chapters[Number(b.dataset.chapter)][1];selected=null;render();});
  $("#timeline").innerHTML=view.shown.slice(-18).reverse().map((e,i)=>`<div class="trace-item ${e.type.startsWith("runtime_injection")?"injection":""} ${i===0?"current":""}"><time>${esc(e.time?.split("T")[1]||e.time)} · #${e.sequence}</time><strong>${esc(M.labels[e.type])}</strong><p>${esc(eventDescription(e))}</p></div>`).join("");
  $("#timeline").scrollTop=0;
  if(mode==="live")for(const n of detail.nodes||[])view.nodes[n.node_id]={...view.nodes[n.node_id],status:n.status,attempts:n.attempts,executor:n.executor_id};
  renderGraph();renderInspector();renderInjections();renderDelivery();
}
function setMode(next){
  stop(); ++loading; mode=next; $("#replay-mode").classList.toggle("active",mode==="replay");$("#live-mode").classList.toggle("active",mode==="live");
  $("#mode-label").textContent=mode==="live"?"真实执行 · 当前运行":"历史真实运行回放";$("#mode-label").classList.toggle("live",mode==="live");
  $("#run-field").hidden=mode==="live";$("#live-setup").hidden=mode!=="live";$("#live-controls").hidden=mode!=="live";
  ["play","step","restart","scrubber","speed"].forEach(id=>$("#"+id).disabled=mode==="live");
  $("#playback-note").textContent=mode==="live"?"实时读取当前运行；每 2 秒刷新":"按真实事件顺序回放；等待时长已压缩";
  $("#injection-help").textContent=mode==="live"?"请求在下一节点边界应用；已完成节点不可注入":"点击卡片可定位真实注入事件";
  if(mode==="replay")loadRun($("#run-select").value);
  else {
    detail=null;runId=null;view=null;selected=null;$("#launch").disabled=!executionEnabled||Boolean(job&&job.status==="running");
    // Never show a historical PASS under the live badge.
    $("#graph").innerHTML="";$("#timeline").innerHTML="";$("#injections").innerHTML="";$("#gates").innerHTML="";
    $("#node-inspector").textContent="启动任务后显示实时节点和路由。";$("#stat-progress").textContent="—";$("#stat-agents").textContent="—";$("#stat-recovery").textContent="—";$("#stat-audit").textContent="等待";$("#stat-audit-note").textContent="最终独立审计";
    $("#task-title").textContent="差旅报销合规审查";$("#task-goal").textContent="读取报销政策、出差申请和费用明细，核验报销合规性，输出审查结果和证据报告。";
    $("#source-run").textContent="尚未启动";$("#input-files").textContent="PDF 政策 · Word 申请 · Excel 明细 · task.md";$("#requirements").textContent="expense_summary.json / final_report.md";
    $("#delivery-content").textContent="等待当前运行的实际交付结果。";$("#delivery-button").disabled=true;$("#connection").textContent="等待实时运行";
    $("#chapter-title").textContent="从高层任务开始真实执行";$("#chapter-copy").textContent="启动后可提交三类运行期扰动，系统将自主恢复并生成交付物。";$("#chapters").innerHTML="";$("#event-clock").textContent="等待运行事件";
    notice(executionEnabled?"输入服务启动口令后启动任务；需要服务器已配置模型环境。":"当前服务只读。请用 --enable-execution 启动服务以进行真实执行。");
    if(job)pollLive();
  }
}
async function pollLive(){
  if(mode!=="live"||!job||polling)return;polling=true;
  try{
    job=await api(`/api/runtime/jobs/${job.job_id}`);
    if(mode!=="live")return;
    $("#connection").textContent=`${job.scenario_label} · ${job.status}`;
    if(job.run_dir){
      const id=job.run_dir.split("/").at(-1), next=await api(`/api/runs/${encodeURIComponent(id)}`);
      if(mode!=="live")return;
      runId=id;detail=next;render();
    }
    if(["success","failed","cancelled"].includes(job.status)){
      $("#launch").disabled=!executionEnabled;notice(job.status==="success"?"本次真实运行结束，独立审计通过。":"本次运行已结束，请检查验收项和服务日志。");
    }
  }catch(e){notice(`实时状态暂未就绪：${e.message}`);}finally{polling=false;}
}
$("#launch").onclick=async()=>{
  $("#launch").disabled=true;notice("正在启动主工作流…");
  try{job=await api("/api/runtime/jobs",{scenario:"interactive_demo"});setMode("live");notice("任务已启动。规划期间提交的注入将在业务节点调度时处理。");await pollLive();}
  catch(e){notice(e.message);$("#launch").disabled=false;}
};
$("#replay-mode").onclick=()=>setMode("replay");$("#live-mode").onclick=()=>setMode("live");
$("#run-select").onchange=e=>loadRun(e.target.value);
$("#play").onclick=()=>{
  if(timer){stop();return;}if(!detail)return;
  if(cursor>=M.eventsFor(detail).length-1)cursor=0;
  $("#play").textContent="Ⅱ 暂停";
  timer=setInterval(()=>{cursor++;selected=null;render();if(cursor>=view.events.length-1)stop();},Number($("#speed").value));
};
$("#speed").onchange=()=>{if(timer){stop();$("#play").click();}};
$("#restart").onclick=()=>{stop();cursor=0;selected=null;render();};$("#step").onclick=()=>{stop();if(detail)cursor=Math.min(cursor+1,M.eventsFor(detail).length-1);selected=null;render();};
$("#scrubber").oninput=e=>{stop();cursor=Number(e.target.value);selected=null;render();};
$("#event-detail").onclick=()=>{if(view?.current)rawDialog("当前事件 · 原始记录",view.current);};
$("#source-button").onclick=()=>{if(detail)rawDialog("任务契约摘要",detail.task_spec);};
$("#delivery-button").onclick=()=>artifact("final_report.md");$("#close-dialog").onclick=()=>$("#detail-dialog").close();
$("#fullscreen").onclick=async()=>{try{if(document.fullscreenElement)await document.exitFullscreen();else await document.documentElement.requestFullscreen();}catch(e){notice(e.message);}};
$("#recording-layout").onclick=()=>{document.body.classList.toggle("recording");$("#recording-layout").textContent=document.body.classList.contains("recording")?"标准布局":"录屏布局";};
async function init(){
  try{
    const data=await api("/api/presentation");catalog=data.runs;executionEnabled=data.execution_enabled;
    $("#run-select").innerHTML=catalog.map(r=>`<option value="${esc(r.run_id)}">${esc(r.label)}</option>`).join("");
    if(catalog.length)await loadRun(catalog[0].run_id);else notice("没有可用的运行证据。请将审计运行目录放入 outputs/runs 后刷新。");
    if(executionEnabled){
      const existing=await api("/api/demo/jobs");
      job=(existing.jobs||[]).filter(j=>j.scenario==="interactive_demo").at(-1)||null;
      if(job?.status==="running")notice("服务器有一个运行中的交互式任务，切换“真实执行”即可继续观察。");
    }
    setInterval(pollLive,2000);
  }catch(e){notice(`无法连接演示服务：${e.message}`);}
}
init();
