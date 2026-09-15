/* Pure event projection shared by the browser and Node verification. */
(function (root) {
  const labels = {
    run_created: "创建运行", stage_started: "进入新阶段", task_understanding_recorded: "高层意图已理解",
    requirement_contract_compiled: "需求契约已生成", task_graph_recorded: "任务图已记录",
    requirement_stage_graph_expanded: "阶段任务图扩展", routing_decision_recorded: "选择执行单元",
    graph_node_started: "节点开始执行", graph_node_result_recorded: "节点返回结果",
    node_recovery_decision: "恢复策略已选择", node_recovery_scheduled: "安排节点恢复",
    executor_switched: "切换执行单元", task_graph_replanned: "重新规划任务图",
    runtime_injection_armed: "扰动已配置", runtime_injection_triggered: "注入扰动",
    runtime_injection_detected: "检测到异常", runtime_injection_recovery_started: "启动恢复",
    runtime_injection_recovered: "恢复动作已完成", runtime_injection_failed: "恢复未完成",
    runtime_injection_request_accepted: "接收运行中注入", runtime_injection_request_rejected: "拒绝不适用的注入",
    business_validation_recorded: "完成业务校验", requirement_ledger_recorded: "更新需求验收账本",
    evidence_verification_recorded: "完成证据核验", delivery_outcome_recorded: "最终交付判定",
    run_finished: "运行结束", business_repair_started: "局部子图修复", business_repair_finished: "子图复验完成",
  };
  const types = {node_failure: "节点失效", requirement_change: "需求变更", data_anomaly: "数据异常"};
  const phases = {armed:"待触发",triggered:"已触发",detected:"已检测",recovery_started:"恢复中",recovered:"已恢复",failed:"未恢复"};
  function eventsFor(detail) {
    return (detail.events || []).filter(e => labels[e.type]);
  }
  function project(detail, cursor) {
    const events = eventsFor(detail), end = Math.max(-1, Math.min(cursor, events.length - 1));
    const shown = events.slice(0, end + 1), nodes = {}, routes = {}, injections = {};
    let version = null, stage = "prepare", finished = false, currentNode = null;
    for (const e of shown) {
      const p = e.payload || {};
      stage = e.stage || stage;
      if (e.type === "stage_started") stage = p.stage || stage;
      if (e.type === "task_graph_recorded") version = p.version ?? version;
      if (e.type === "routing_decision_recorded") routes[p.node_id] = p;
      if (e.type === "graph_node_started") {
        nodes[p.node_id] = {status:"running", attempts:p.attempt, executor:p.executor_id};
        currentNode = p.node_id;
      }
      if (e.type === "graph_node_result_recorded") {
        nodes[p.node_id] = {...nodes[p.node_id], status:p.status, executor:p.executor_id};
        currentNode = p.node_id;
      }
      if (e.type === "node_recovery_scheduled" && p.node_id) {
        nodes[p.node_id] = {...nodes[p.node_id], status:"recovering"};
      }
      if (e.type.startsWith("runtime_injection_") && p.injection_id && p.type &&
          !e.type.includes("request_")) {
        const phase = e.type.replace("runtime_injection_", "");
        const item = injections[p.injection_id] || {id:p.injection_id,type:p.type,history:[],payload:{}};
        item.phase = phase; item.payload = {...item.payload,...p}; item.history.push(phase);
        injections[p.injection_id] = item;
        if (p.new_graph_version !== undefined) version = p.new_graph_version;
        if (p.node_id) {
          currentNode = p.node_id;
          if (["triggered","detected","recovery_started"].includes(phase)) {
            nodes[p.node_id] = {...nodes[p.node_id], status:phase === "recovery_started" ? "recovering" : "failed"};
          }
          if (phase === "recovered" && p.type !== "requirement_change") {
            nodes[p.node_id] = {...nodes[p.node_id], status:"completed", attempts:p.successful_attempt};
          } else if (p.type === "requirement_change") {
            nodes[p.node_id] = {...nodes[p.node_id], status:"changed"};
          }
        }
      }
      if (e.type === "run_finished") finished = true;
    }
    return {events, shown, nodes, routes, injections, version, stage, finished, currentNode,
      current:shown.at(-1), cursor:end};
  }
  const api = {labels, types, phases, eventsFor, project};
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.DemoModel = api;
})(typeof window !== "undefined" ? window : globalThis);
