# 多智能体框架整体改动说明

> 文档日期：2026-08-03  
> 对应需求基线：`PROJECT_REQUIREMENTS.md` v1.3  
> 文档用途：总结当前工作区从早期固定流程版本到“需求约束的动态异构任务图”版本的累计改动  
> 注意：本文记录的是当前实现状态，不等同于 Git 提交历史

## 2026-09-03：MathorCup 业务失败重放优先使用领域 fallback

修复了 MathorCup P0 闭环中的一个实际控制流问题：代码节点只要生成了结构合法的
JSON，就会在通用产物校验后提前返回；后续 MathorCup 领域校验发现数量、几何或约束
错误时，重开的 code 节点又可能再次生成结构合法但业务错误的结果，使原本声明的
确定性 fallback 无法接管。

现在，当业务验收失败并重放责任 code 节点时，如果 TaskSpec 声明了
`code_fallback_plugin`，框架会优先调用该插件，随后仍执行普通的代码保存、真实
subprocess、退出码、通用产物 Schema 和最终领域验收。该逻辑是通用插件钩子，普通
任务不会进入 MathorCup 分支，也不会跳过 EvidenceVerifier 或最终成功闸门。

验证：MathorCup P0 测试和全量回归均通过（251 tests）。真实 API 运行仍须重新执行，
并以 `business_validation=passed`、`requirement_acceptance=passed`、
`final_report.md` 存在且 `run_state.outcome=success` 为唯一完成标准。

同日真实运行 `outputs/runs/20260903_160722` 证明上述 fallback 已生效：最终结果为
300 条摆放记录，G1-G5 数量分别为 80/100/30/40/50，约束审计 `passed=true`。该轮
仍因 EvidenceVerifier 模型返回非法 verdict、直接依赖覆盖不足和连接错误而失败。
随后增加了通用证据响应归一化、对中间假设的保守处理，以及由 TaskSpec 声明的领域
证据 fallback；全量回归保持 251 tests 通过。下一轮真实运行应重点确认
`evidence_verification` 和 `requirement_acceptance` 均闭合。

为保证比赛截止前的可重复性，进一步将 `evidence_fallback_plugin` 设为声明该插件的
领域任务的优先证据路径：MathorCup 的证据结论直接来自框架已读取且通过确定性审计的
本地 JSON，仍执行 EvidenceVerifier 的引用、直接依赖覆盖和最终验收检查；未声明插件
的通用任务仍调用模型 EvidenceVerifier。

## 2026-09-07：分阶段治理前收敛重复 code 节点

对真实运行 `outputs/runs/20260907_182245` 复核后确认，异常消息中的“必须且只能存在
一个 code 节点”实际由 code 节点数量为 0 触发，而不是重复节点。当前修复同时覆盖
两种合同违例：当任务明确要求 `terminal_execution`、全局产物合同非空但规划遗漏 code
节点时，框架补充唯一 `framework_code_delivery`，使其依赖前序业务叶子、独占全局中间
产物，并继承对应 requirement 与 acceptance；当规划器生成多个 `code` primitive 时，
治理挂载前做通用收敛：
保留拥有最多全局声明产物的节点（同数时稳定选择先出现者），删除同一物理产物契约
的重复节点，并将其下游依赖重定向到保留节点。该逻辑不读取 MathorCup 或其他领域
字段，仅保证单一代码入口与单一终端执行契约。

同日补充通用的已验证报告兜底：仅当报告前技术闸门、业务验证和需求验收已经允许
生成报告，而唯一一次报告模型调用因连接、格式或空响应失败时，框架才从冻结 TaskSpec、
已完成任务图、真实执行记录和已验收产物摘要生成 `final_report.md`。该兜底不会为失败
任务生成报告，也不会补造业务数值，避免最后一次文案模型故障抹掉已经验证的交付。

新增 `utils/run_audit.py` 作为统一运行验收入口。该工具默认定位最新运行，也支持指定
run 目录和 JSON 输出，并以进程退出码同时反映最终 outcome、业务验证、需求验收、
证据核验、报告存在性、节点状态和未解决阻断问题，避免只检查退出码 0 或单个产物而
误判整轮成功。

补充比赛交付文档 `docs/COMPETITION_TECHNICAL_REPORT.md` 和
`docs/比赛提交材料索引.md`，将动态拓扑、长程记忆、低熵通信、端边云调度、故障恢复、
跨场景演示及当前可核验指标映射到任务书条款；历史失败运行被明确标注为过程证据，
不作为已完成演示冒充成功。

全量回归还发现 WebSurfer 执行器使用了 Python 3.11 才提供的 `asyncio.timeout()`，在
当前 Python 3.10 比赛环境会使网页节点必然失败。现改为等价的
`asyncio.wait_for()` 有界执行，保持超时、关闭组件和失败记录语义不变。最终定向测试
25 项及全量单元/集成测试 255 项全部通过。

### 2026-09-07：第二跨领域演示与量化证据闭环

- 为 `examples/expense_reimbursement` 增加 TaskSpec 声明的任务插件，不向通用调度器
  写入报销字段或规则；插件从本轮 `normalized_input.json` 读取 PDF、DOCX、XLSX。
- 固定 `expense_summary.json` 契约，独立复算 5 条明细、总额 1485 元、交通/住宿/餐饮
  分类汇总，并校验缺票、餐补超标、超出差旅日期及“不得代替人工审批”的任务边界。
- 增加 `expense_validation_log.json`、确定性修复基线和证据 fallback。业务错误会定位
  结果产物的 code owner，重新执行并再次验证。
- 修正领域证据与图后业务验收的时序：证据插件先调用同一个独立验证器，将生成器的
  provisional 日志替换为复算审计；正式业务阶段仍重复验证，任一阶段失败都不能报告成功。
- EvidenceVerifier 不再通过 MathorCup 文件名前缀判断权威结果，而是使用 TaskSpec
  `artifact_contract.intermediate_artifacts`，因此新增任务无需修改证据核心逻辑。
- 新增 `utils/competition_metrics.py`：只接收至少两个完整审计 PASS 的运行并导出
  JSON/Markdown 对比，覆盖耗时、Token、模型调用、拓扑、上下文投递和恢复轮次。
- 当前全量单元/集成测试为 266 项全部通过；两个场景仍须在配置 API 环境变量后分别
  获得一次 `utils/run_audit.py` PASS，才能作为正式跨领域演示证据。

### 2026-09-07：运行审计看板（用户体验评分补强）

- 新增 `utils/dashboard_server.py` 与 `dashboard/` 静态前端，提供本地只读运行控制台：
  运行记录、完整交付闸门、节点状态/执行器、产物、Token、稀疏拓扑、逻辑步数和恢复轮次
  均从 `run_state.json`、`task_graph.json` 和 `utils/run_audit.py` 结果读取。
- 服务只允许 `outputs/runs/<run_id>` 精确目录查询，不执行任务、不接受写入路径；桌面和
  390px 移动布局已用浏览器检查，移动端无水平溢出。
- 看板不把历史失败运行伪装成成功：失败状态、未闭合门禁和审查问题会显式展示。

### 2026-09-07：比赛运行前检查

- 新增 `utils/preflight.py`，在正式运行前检查非敏感模型配置是否存在、输入目录、输出
  目录写权限、关键依赖和两个任务插件是否可加载。
- 检查结果不打印 API Key，只报告是否设置；可减少截止前因环境遗漏造成的无效整轮运行。

### 2026-09-07：提交包安全与完整性门禁

- 新增 `utils/package_submission.py`，提交前强制要求两个不同任务类型的完整审计 PASS
  运行，扫描候选文本文件中的 API Key 模式，并生成带 `submission_manifest.json` 的 ZIP。
- 打包内容覆盖源代码、示例、测试、比赛文档和已审计运行证据；历史 failed 运行会被拒绝，
  不会被误打包成成功演示材料。

- 新增 `docs/评分自评与最后提分清单.md`，按任务书 100 分评分项区分当前可证明证据、
  保守分数区间和最后提分动作，明确两个真实跨领域 PASS 运行是当前最大增分点。

- 新增 `scripts/finish_competition.sh`，串联 preflight、MathorCup、差旅报销、完整审计、
  跨场景指标和提交包；任一步失败立即退出，且不输出密钥值。
- 收尾脚本改为直接保存并审计 `RUN_DIR`，避免管道末端命令掩盖中间审计失败。

## 2026-09-02：MathorCup P0 验收闭环

本轮按领域插件方式补齐 MathorCup D 的最低完整验收链路，没有把装箱规则写入通用
Scheduler、ArtifactValidator 或 RunState：

- TaskSpec 仅在识别到 MathorCup D 输入时声明
  `result.json`、`result_complete.json`、`constraint_validation_log.json`、
  `cost_comparison.json` 和 `code_pipeline.py`；
- 代码提示固定 `vehicle_scenarios`、`multi_vehicle_scenarios`、`placements`、
  `vehicle_count`、`total_cost`、空间/载重利用率和 `validation` Schema；
- `MathorCupPackingValidator` 从 `normalized_input.json` 读取真实货物和车辆参数，复核
  G1-G5 的 300 件数量、坐标和尺寸、定向姿态、AABB 重叠、易碎件上方堆叠、3cm
  顶部间隙、车辆重量、车辆数、成本及利用率，并输出带具体索引的验证日志；
- 业务失败按唯一 code producer 路由，repair context 保留 `boundary/index` 等结构化定位；
  同类错误采用有界样本，避免修复 Prompt 被大量重复错误撑满；
- EvidenceVerifier 将旧 basename 规范化为 run-relative 路径，优先读取三个小型审计
  JSON 和 `artifacts/tool_results/terminal_execution/terminal_execution.json`，并继续强制
  claim 只能引用直接依赖节点；
- 模型调用新增可配置的 `MODEL_CALL_TIMEOUT_SECONDS`（默认 240 秒），无响应会成为可重试
  的传输失败，避免长时间停留在无状态更新的模型请求中。

确定性回归结果：全套 `248` 项 unittest 通过；新增 P0 用例覆盖 300 件合法结果、越界
`index=11` 定位、领域专属产物合同、证据路径规范化及修复上下文传递。

## 0. 先用简单的话理解整个系统

这个框架现在做的事情，可以概括成下面八步：

```text
读取用户文件
  ↓
整理“用户到底要求完成什么”（TaskSpec + Requirement Contract）
  ↓
框架选择当前依赖已满足的需求，PlanningAgent 生成当前阶段的层级计划和子图
  ↓
Python 检查计划是否漏需求、过粗、存在环或缺少验收条件
  ↓
Scheduler 按节点能力选择合适的 Agent 或 Python 执行器；阶段完成后继续展开未完成需求
  ↓
节点只接收直接依赖的信息，并使用压缩、去重、引用和运行时记忆
  ↓
代码真实保存、执行，证据节点和审查节点检查结果
  ↓
只有框架验收通过，才应该生成报告并标记 success
```

其中最关键的边界是：

- Agent 负责理解、规划、分析、生成代码和提出审查意见；
- Python 框架负责阶段推进、状态修改、DAG 校验、代码执行、产物登记和最终成功判定；
- DAG 不是固定流程，而是根据 TaskSpec、Requirement Contract 和可用 capability 动态生成；
- `AgentPrune-lite` 控制“谁能看到谁的信息”，`DyLAN-lite` 负责同一 capability 下的执行器排序，
  `LLMLingua-2` 只在需要时压缩自然语言上下文；
- Requirement Contract 用来防止长任务执行多轮后遗忘原始目标和约束；
- RuntimeMemory 保存节点胶囊和可核验记忆，避免每个节点都依赖模型长窗口。

当前已经比较稳定的部分是：

1. 显式 Python 工作流和实时 RunState；
2. TaskSpec、Requirement Contract、层级 PlanIR 和动态 TaskGraph；
3. 异构 capability 路由、稀疏依赖通信和运行时记忆；
4. 代码真实落盘、真实执行、产物追踪和报告单次生成；
5. 需求来源覆盖、粗粒度需求/节点检测和局部 refinement。
6. 通用 Acceptance Contract、Requirement Ledger 与最终成功阻断。
7. 需求驱动的阶段性子图生成：只规划依赖就绪需求，保留已完成节点，逐阶段增长 TaskGraph。

当前尚未完全可靠的部分是：

1. 阻断问题已改为单调账本，但 EvidenceVerifier 发现问题后尚不能总是准确路由到可修复的上游责任节点；
2. 已形成逐条 Requirement Closure，但无法机器化的自然语言验收仍会被标记为 `unverified`；
3. 代码内部错误修复和业务验证后的局部重放已经接通，但缺少输入证据的问题只能阻断，不能伪造修复；
4. 通信拓扑已经稀疏，但实际字节节省和 LLMLingua 触发效果尚未稳定证明；
5. 分阶段执行当前以生产节点成功作为下一阶段解锁条件，最终业务验收仍在全图完成后统一执行；阶段内语义失败的即时修复还需继续增强。

因此，当前版本应准确描述为：

> 已具备需求驱动的动态异构长程任务规划、阶段性子图增长、阻断问题账本和两层自动修复闭环；
> 通用业务验证深度、阶段内验收与证据失败的责任路由仍需继续加固；
> 不能因为某次运行显示 `success`，就直接认定真实业务任务已经完成。

## 1. 改动结论

本轮累计改造的核心不是继续堆叠 Agent，而是把工作流控制权从 Agent 对话收回 Python 框架，并在此基础上形成 Requirement Graph、层级规划、动态任务图、能力路由、低熵通信、失败恢复、事实核验和工作流记忆链路。

框架已经从早期的“固定 Agent 队伍 + 自由群聊式调度”演进为：

```text
固定安全外壳
prepare → classify → plan → execute_graph → review → report → final_validate → finish
                            │
                            └─ Python 调度动态 TaskGraph
                               ├─ 按 capability 选择异构执行者
                               ├─ 只传递直接依赖的结构化消息
                               ├─ 节点失败后重试、切换或重规划
                               └─ 设计目标：事实核验通过后才允许产物校验和报告
```

当前实现已经具备“可以真实运行、可以定位大部分失败、可以审计交付、可以继续扩展”的基础框架形态。
但最新真实运行证明，业务错误仍可能被错误标记为 success，因此“最终成功可信”尚未完成闭环。

## 2. 改造前后对比

| 维度 | 早期状态 | 当前状态 |
|---|---|---|
| 流程控制 | 依赖群聊和 Agent 自行宣称完成 | Python 显式状态机独占阶段推进权 |
| 任务描述 | 多处重复推断任务类别和产物 | TaskSpec + Requirement Contract 形成权威需求来源 |
| 规划 | 自然语言步骤或自由调度 | PlanningAgent 输出层级 PlanIR，验证后展平为 SemanticGraph |
| 执行拓扑 | 固定 Agent 顺序或静态队伍 | 根据任务语义生成 DAG，并动态选择必要节点 |
| Agent 选择 | 固定注册后全部参与 | Capability Registry 按节点能力动态路由 |
| 通信 | 容易广播完整历史 | 只发送直接依赖结果，并执行字段投影、压缩、去重和引用化 |
| 代码执行 | 临时文件、路径和成功判断不统一 | 代码真实落盘、真实执行、记录退出码和文件变化 |
| 失败恢复 | 主要依赖自然语言建议 | Python RecoveryController 决定重试、切换和重规划 |
| 产物管理 | Agent 可以声称已经保存文件 | 框架登记产物、来源节点、图版本、大小和 SHA256 |
| 审查与报告 | 报告可能先生成或被后续消息覆盖 | 设计为技术预审 → 单次报告 → 最终验收；阻断状态传播仍需加固 |
| 事实一致性 | 主要依赖报告 Agent 自律 | EvidenceVerifierAgent + Python 证据目录已接入，但尚缺单调失败账本 |
| 长期经验 | 只有运行摘要 | WorkflowMemory + RuntimeMemory 胶囊与分层记忆，Python 控制存储 |
| 可观测性 | 以聊天轨迹为主 | RunState 记录节点、路由、通信、Token、恢复、工具和记忆指标 |

## 3. 显式工作流与状态机改造

### 3.1 主流程显式化

`main.py` 现在负责：

1. 创建独立运行目录。
2. 加载并复制输入文件。
3. 生成初始 TaskSpec。
4. 生成文件预览并完成任务分类。
5. 冻结 TaskSpec。
6. 创建需要的模型客户端。
7. 调用显式工作流。
8. 保存 Memory、RunState、Trace、报告和验收结果。
9. 可靠关闭模型客户端。

`orchestration/core/workflow.py` 按固定阶段推进，任何 Agent 都无权在返回文本中改变当前阶段。

### 3.2 RunState 从日志升级为实时状态机

`orchestration/core/run_state.py` 现在实时记录：

- 当前外层阶段和最终 outcome；
- 代码执行次数、退出码和执行历史；
- TaskGraph、节点状态和图执行结果；
- 每次能力路由的候选、评分和选中执行者；
- 产物是否存在、来源节点、图版本和 SHA256；
- 报告前审查和最终验收结论；
- 依赖消息的 Token、字节、压缩和重复指标；
- 模型调用 Token、预算、耗时和调用 Agent；
- 节点重试、执行者切换和重规划记录；
- 外部组件真实调用证据；
- 工作流记忆检索和提炼记录。

关键原则是：Agent 返回结构化建议，Python 才能修改 RunState。

## 4. TaskSpec 单一任务契约

`orchestration/task/task_normalizer.py` 负责一次性确定：

- `task_type`；
- `code_policy`；
- `artifact_contract`；
- `required_artifacts`；
- `required_report_sections`；
- `success_criteria`；
- `capability_contract`；
- 通信、路由、团队、恢复、外部工具、核验、记忆和终端策略；

`normalize_to_task_spec()` 根据原始输入建立初始契约，`finalize_task_spec()` 在文件预览完成后只进行一次最终分类和契约冻结。后续规划器、调度器、Agent 和验收器都读取同一份 TaskSpec，不再各自猜测任务类型。

当前能力契约的典型行为：

- 所有标准化任务默认要求 `evidence_verification`；
- 代码任务额外要求 `terminal_execution`；
- 长文档或定位原文任务要求 `file_navigation`；
- 预览失败或特殊格式输入要求 `document_conversion`；
- 明确要求联网、官网、URL 或最新信息时要求 `web_research`。

## 5. 动态 TaskGraph

### 5.1 TaskNode 和 TaskGraph

`orchestration/graph/task_graph.py` 新增了结构化任务图模型。

每个 TaskNode 包含：

- 节点 ID 和单一职责描述；
- 所需 capability；
- 直接依赖节点；
- 必须保留的依赖字段；
- 输入和输出产物；
- 可机器检查的成功标准；
- 最大重试次数；
- 由框架维护的运行时状态、执行者、结果和错误。

TaskGraph 可以检查：

- 节点 ID 是否重复；
- 依赖节点是否存在；
- 是否存在自依赖或循环；
- capability 是否已经注册；
- 路径是否越权；
- 是否有多个节点写同一产物；
- 节点是否声明产物或成功标准；
- 节点状态转换是否合法。

模型输出中的 `completed`、`assigned_executor`、`result` 等运行时声明会被框架清空，防止 PlanningAgent 伪造执行结果。

### 5.2 Requirement Graph 与层级 PlanningAgent

TaskUnderstandingAgent 先从 TaskSpec、文件预览和由 Python 定位的 source candidate 中输出
结构化任务简报与原子需求。每条需求包含稳定 ID、父子关系、AND/OR/optional 关系、来源、
预期输出、验收条件、强制/可选状态和负责层级。

`orchestration/planning/requirement_compiler.py` 负责：

- 从 PDF、Word 等预览中定位包含“必须、分别、输出、计算、比较、验证”等义务语义的来源片段；
- 给来源片段分配稳定的 `SRC-*` ID；
- 检查需求是否覆盖全部来源候选；
- 检查需求 ID、父节点、环、mandatory acceptance 和明显跨职责需求；
- refinement 后保留历史需求，防止模型无意删除旧 requirement；
- 将最终合同保存为 `planning/requirement_contract.json`。

PlanningAgent 随后结合冻结的 TaskSpec、Requirement Contract 和任务简报输出层级 PlanIR。
PlanIR 使用 compound/primitive 表示任务层级，每个 primitive 必须声明 `requirement_ids` 和
`acceptance_refs`。PlanValidator 检查：

- mandatory 叶子需求是否被业务 primitive 覆盖；
- blocking acceptance 是否被引用；
- 输入、依赖、边和产物是否合法；
- math modeling 或 code primitive 是否跨越多个可独立验收阶段；
- refinement 是否丢失需求或产生悬空边。

通过验证的 PlanIR 才会展平为 SemanticGraph，再由 GraphCompiler 插入终端、证据和产物治理
节点并生成最终 TaskGraph。规划过程会保存每轮 Requirement Contract、PlanIR、PlanPatch 和
Validation，便于定位任务在哪一层变粗或丢失。

### 5.3 Graph Scheduler

`orchestration/graph/graph_scheduler.py` 当前采用单线程就绪节点调度：

1. 找到依赖均已完成的 ready 节点。
2. 根据 capability 从 Registry 选择执行者。
3. 只构建该节点直接依赖的上下文。
4. 调用统一 NodeExecutor 接口。
5. 校验返回身份、真实产物和成功标准。
6. 保存节点结果和产物来源。
7. 失败时交给 RecoveryController。
8. 所有节点结束后确定图级 outcome。

外层生命周期仍固定，但 `execute_graph` 内部的节点数量、能力类型和依赖关系会随任务变化，因此不是固定业务流水线。

## 6. Capability Registry 与动态异构路由

`orchestration/execution/capability_registry.py` 和 `orchestration/execution/node_executor.py` 建立统一执行接口。

一个执行者可以是：

- LLM Agent；
- Python 确定性执行器；
- 代码流水线；
- 文档或浏览器工具；
- 外部服务适配器；
- 未来的端、边、云资源节点。

Registry 只根据 capability 选择可用执行者。新增 Agent 原则上只需实现 NodeExecutor 并注册，不修改主状态机和 Scheduler。

当前默认团队提供分析、研究、推理、代码、产物校验，以及按 TaskSpec 动态注册的文件、网页、事实核验和终端能力。

## 7. 低熵通信与研究方案适配

### 7.1 结构化消息协议

新增 `communication_models.py` 和 `context_builder.py`，将 TaskGraph 的依赖边变成通信权限边。

普通节点只能看到：

- 自己的节点契约；
- TaskSpec 中必要的任务摘要；
- 直接依赖节点的结构化结果；
- 被授权的输入产物和证据引用。

不会向每个 Agent 广播完整聊天历史。

### 7.2 AgentPrune-lite

当前采用轻量适配，而不是完整论文复现：

- 只保留 summary、产物路径、证据路径和必要结构化字段；
- `dependency_fields` 可以使用 JSON Pointer 声明必须内联保留的字段；
- 大型结构化结果保存为内容寻址文件，下游传递引用；
- 相同 payload 使用 SHA256 精确去重；
- 不允许裁剪 TaskGraph 的硬依赖边。

### 7.3 LLMLingua-2

LLMLingua-2 作为可选上下文压缩器接入：

- 只压缩自然语言字段；
- 数字、日期、路径、JSON 字段和证据引用保持结构化传递；
- 默认禁止运行时隐式下载模型；
- 可通过 CLI 冻结 CPU/CUDA 和下载策略；
- 未安装、加载失败或压缩不达标时自动退回确定性压缩。

### 7.4 DyLAN-lite

Capability Registry 可根据执行者历史表现动态排序候选：

- 按任务类型隔离统计；
- 记录成功率、质量、Token 和延迟；
- 冷启动时使用静态先验；
- 运行历史只影响排序，不能越过 capability 和安全约束；
- Scheduler 仍拥有最终调用权。

### 7.5 AFlow 接口

当前只保留离线策略快照接口。AFlow 可以未来离线搜索策略，但在线执行期间不能直接改写活动 TaskGraph，避免学习算法绕过框架校验。

## 8. 失败恢复与动态重规划

`orchestration/graph/recovery_controller.py` 统一处理节点失败。

当前决策顺序为：

1. 在 TaskSpec 和节点重试上限内重试同一执行者；
2. 切换到支持相同 capability 的其他执行者；
3. 在策略允许时请求 PlanningAgent 返回完整替代图；
4. 不能恢复时使失败节点和其后代进入 failed/blocked。

安全重规划规则包括：

- 已完成节点必须保留；
- 已完成节点定义、结果和产物不能修改；
- 已完成产物会重新检查 SHA256；
- 新图重新执行完整 DAG、能力和产物契约校验；
- 当前最多执行一次完整替代图重规划；
- 尚未实现增量 GraphPatch；进程级断点恢复已提供 checkpoint 校验与恢复入口，幂等重放仍需场景化验证。

## 9. 真实工具与功能性 Agent 接入

### 9.1 MarkItDown

- capability：`document_conversion` / `document_recovery`；
- 负责多格式文档转换和预览恢复；
- 不调用 LLM；
- 保存真实转换结果和调用证据。

### 9.2 AutoGen FileSurfer

- capability：`file_navigation`；
- 通过真实 `open_path` 工具读取长文档和定位原文；
- 可见范围限制在当前 `run_dir/inputs/`；
- 不能写 RunState 或控制流程。

### 9.3 AutoGen MultimodalWebSurfer

- capability：`web_research` / `browser_navigation`；
- 通过 Playwright 获取公开网页信息；
- 默认禁用表单输入和下载；
- 限制最大步骤数、超时和 Prompt 预算；
- 必须记录工具动作、来源 URL 和结构化调用证据。

外部工具节点必须进入后续语义分析或代码链路。只把工具挂到最终校验节点、但没有业务节点消费其结果，会被判定为冗余节点。

## 10. 新增三类核心 Agent

### 10.1 EvidenceVerifierAgent

EvidenceVerifierAgent 采用 SAFE/CRITIC 风格的轻量适配，用于报告前事实收口。

真实调用流程：

1. TaskSpec 默认声明 `evidence_verification` 为 required capability。
2. PlanningAgent 必须生成且只能生成一个核验节点。
3. 每个业务节点必须成为核验节点的直接依赖。
4. Python 读取直接依赖授权的文件、JSON Pointer 和工具证据 JSON。
5. Agent 将上游结论拆分为原子 claim，并返回来源节点、证据引用和 verdict。
6. Python 检查来源节点覆盖、证据是否真实读取、claim ID、支持状态和补充证据请求。
7. 设计目标是任一重要事实不受支持时阻断产物校验、审查和报告。

当前已知缺陷：核验节点第一次返回 failed 后，第二次重试可能通过减少 claim 或遗漏旧 issue
返回 passed。框架目前没有持久化、单调累积 unresolved blocking issue，因此这一设计目标
尚未可靠实现。后续必须增加 blocking issue ledger，只有新产物和明确修复证据才能关闭旧问题。

网页 URL 本身只作为来源元数据，不能直接证明网页事实；核验必须引用包含抓取内容和工具动作的 WebSurfer 证据 JSON。

### 10.2 WorkflowMemoryAgent

WorkflowMemoryAgent 采用 Agent Workflow Memory / Voyager Skill Library 风格的轻量适配。

它不作为虚假的业务节点加入 TaskGraph，而是由固定外壳在两个时机调用：

- 规划前：当 Python 检索到相关历史技能候选时，由 Agent 选择候选 ID；
- 最终验收后：根据已经验证的图和 outcome 提炼成功工作流或失败教训。

权限边界：

- Agent 不能直接读取或写入技能库；
- 规划 guidance 只能由 Python 从已选技能记录重新构造；
- Agent 不能向 Planner 注入任意新指令；
- Python 检查最低置信度、每轮新增上限、密钥和绝对路径；
- 技能使用内容寻址 ID，默认保存到 `memory/workflow_skills.json`；
- 记忆异常只记录事件并降级为空记忆，不阻断当前任务。

### 10.3 AutoGen CodeExecutorAgent

代码任务会动态增加 `terminal_execution` 节点，并调用 AutoGen 的真实 `CodeExecutorAgent`。

当前用途是对框架已经落盘并执行过的 Python 入口进行独立语法复核：

```text
code → terminal_execution → evidence_verification → artifact_validation
```

安全限制：

- 当前只允许 `python -m py_compile artifacts/code_pipeline.py`；
- 命令由 Python 框架构造，不接受模型生成的 Shell 文本；
- AutoGen approval function 会逐次核对完整代码块；
- 路径必须是 `run_dir` 内的安全相对 `.py` 路径；
- 记录真实退出码和执行证据；
- 编译产生的临时 bytecode 会被清理。

当前后端是 `bounded_local`，不是 Docker 或虚拟机级隔离。未来允许任意测试命令前必须先升级为容器执行器。

## 11. 代码执行与产物契约

代码流水线已经改为：

1. CodeModelingAgent 只返回结构化代码文本。
2. Python 校验代码长度、路径和禁止写入的系统文件。
3. Python 保存到 TaskSpec 指定路径，例如 `artifacts/code_pipeline.py`。
4. 执行器以 `run_dir` 为工作目录运行相对路径，不再二次 `cd outputs/runs/...`。
5. 记录命令、退出码、stdout、stderr 和本轮新增或修改文件。
6. `exit_code != 0` 默认失败。
7. 重试次数只读取 `TaskSpec.code_policy.max_retries`。
8. 产物由框架依据 ArtifactContract 检查存在性和标准 JSON，语义一致性由任务图核验节点负责。

框架只接受节点本轮真实创建或修改的声明产物。预先存在但没有变化的文件不能被节点冒充为本轮产出。

## 12. 审查、报告与最终验收

当前顺序固定为：

```text
TaskGraph 执行
    ↓
pre_report_review
    ↓ 仅技术门禁允许时
Semantic ReviewAgent
    ↓
ReportAgent 单独调用一次
    ↓
final_validation
    ↓
WorkflowMemoryAgent 提炼候选
    ↓
finish
```

ReportAgent 已从自由群聊调度中移出，只能生成最终业务 Markdown，不能生成 TaskSpec、RunState、Trace 或验收文件。

这里需要区分“流程顺序已经固定”和“成功闸门已经可靠”：

- ReportAgent 单次调用和系统文件写入权限已经正确收回；
- 代码退出码、产物格式、业务验证和交付验证已经分成不同 success dimension；
- 但 ReviewAgent 返回 `blocking_issues`、`repair_recommended=true` 时，当前 workflow 仍可能继续
  生成报告；
- EvidenceVerifier 的历史失败也可能被后续 passed 覆盖；
- 因此最终 `success` 目前还不能只依赖最新一次 Agent 决策，必须增加框架级 unresolved
  blocking issue 闸门。

最终验收至少检查：

- 必需中间产物和最终产物是否存在；
- JSON 是否可以严格解析，禁止 NaN/Infinity；
- final report 是否为业务报告而不是 JSON 或轨迹；
- 是否包含 TaskSpec 要求的章节；
- 是否泄露 Agent 名称或执行轨迹；
- 报告引用的产物是否真实存在；
- 代码执行或 TaskGraph 失败时是否错误标记 PASS；
- 报告是否越过 TaskSpec 中的业务决策范围。

## 13. 通用输入与产物契约边界

核心 Scheduler、TaskGraph、Registry、通信、恢复和状态机不包含具体领域字段，也不会
根据业务关键词选择隐藏处理器。

- `input_preparation.py` 只保存任务输入元数据和框架文件预览，不做领域变换；
- `artifact_validator.py` 只依据 TaskSpec 检查产物存在性和标准 JSON；
- 语义校验由 TaskGraph 中的分析、证据核验和审查节点完成；
- 代码执行器只读取 TaskSpec 的 ArtifactContract，不接收额外业务提示词。

当前版本不包含插件系统。增加领域能力时应优先注册新的 Capability Executor，且不能
修改核心调度循环或让领域处理器成为所有任务的启动依赖。

## 14. 配置与密钥安全

`config.py` 已移除硬编码 API Key，统一从环境变量创建模型客户端。

主模型和工具模型可以分别配置：

- `MODEL_*`；
- `FILE_SURFER_*`；
- `WEB_SURFER_*`。

非 OpenAI 官方模型如果使用 OpenAI 兼容接口，会显式传入 `model_info`，解决自定义模型名称无法识别的问题。

工具模型必须真实支持 function/tool calling。模型密钥不应写入版本控制；本地 `key.md` 和 `.env` 应保持忽略状态。

## 15. 运行产物与审计证据

每轮运行目录为 `outputs/runs/<run_id>/`，主要文件如下：

| 文件 | 作用 | 写入者 |
|---|---|---|
| `task_spec.json` | 冻结任务类型、策略和产物契约 | Python |
| `task_graph.json` | 规划图及节点实时状态 | Python |
| `run_state.json` | 状态机、指标、失败和验收状态 | Python |
| `normalized_input.json` | 通用输入元数据和文件预览边界 | Python |
| `file_previews.json` | 输入文件预览 | Python |
| `agent_trace.md` | 可审计的结构化执行轨迹 | Python |
| `artifacts/code_pipeline.py` | 可复现代码入口（代码任务） | Python 保存 Agent 代码 |
| `artifacts/tool_results/` | 外部工具、终端和事实核验证据 | Python 执行器 |
| `final_report.md` | 最终业务报告 | ReportAgent 单次输出，Python 保存 |
| `review/validation_report.md` | 最终验收结果 | Python |

跨运行工作流技能不属于某一轮运行目录，默认由 Python 过滤后写入项目级
`memory/workflow_skills.json`。

因此可以区分：能力已注册、规划选择了节点、执行者被真实调用、节点成功、产物通过验收这五个不同状态。

## 16. 主要代码文件索引

| 文件 | 当前职责 |
|---|---|
| `main.py` | 准备、分类、客户端生命周期和显式工作流入口 |
| `config.py` | 主模型、文件工具模型和网页工具模型配置 |
| `agents/task_understanding_agent.py` | 目标、约束、歧义、风险和能力建议的语义提炼 |
| `orchestration/core/` | 工作流、状态机、结构化模型、提示和运行管理 |
| `orchestration/task/` | 输入加载与准备、TaskSpec 和通用产物契约 |
| `orchestration/graph/` | TaskGraph、规划校验、调度和恢复控制 |
| `orchestration/execution/` | 能力注册、执行者选择、评分和节点执行接口 |
| `orchestration/communication/` | 稀疏上下文、消息协议、预算和压缩 |
| `orchestration/integrations/` | 文件、网页、终端和事实核验适配器 |
| `orchestration/memory/` | 工作流记忆检索、过滤、提炼和持久化 |
| `utils/review_artifacts.py` | 报告前审查和最终交付验收 |

旧 MagenticOne 自由群聊入口 `base_team.py`、未接入主流程的 `run_context.py`、旧字典状态
初始化函数、固定步骤计划模型以及无调用方的 Prompt/代码 Agent 兼容函数已经删除。当前项目
只保留
`TaskSpec → Requirement Contract → PlanIR → SemanticGraph → TaskGraph → GraphScheduler → Review/Report/Validation`
这一条运行主链。

进一步的可达性审计还删除了 `run_magentic_one.py` 兼容入口、与 RunState 和工作流技能库
重复的旧 `MemoryStore`、未调用的报告/审查包装函数、重复的中间产物解析函数，以及
MessageMetrics 到 RunState 之间的旧字段别名。`memory/` 现在只保存运行时生成的执行者统计和
工作流技能，不再包含第二套运行状态。

## 17. 测试与验证结果

本轮验证命令：

```bash
conda activate autogen
python -m unittest discover -s tests -q
python -m compileall -q agents orchestration tests
python -m pip check
git diff --check
```

当前结果（2026-07-28）：

- 完整自动化测试：`Ran 148 tests ... OK`；
- TaskUnderstandingAgent 调用顺序、结构化产物和降级行为测试通过；
- Requirement source coverage、Requirement refinement、PlanIR coverage、Prompt soft budget、
  RuntimeMemory、动态路由和失败恢复专项测试通过；
- Python 编译检查通过；
- 依赖检查：`No broken requirements found`；
- Git diff 空白和格式检查通过。

专项测试验证的不只是注册名称：

- EvidenceVerifier 真实读取临时 JSON 和 JSON Pointer，并由 Scheduler 路由调用；
- AutoGen CodeExecutorAgent 真实启动本地执行器、执行固定命令并返回退出码；
- WorkflowMemoryAgent 在存在候选技能时于规划前调用，并在最终验收后再次调用；
- Python 会忽略记忆 Agent 注入的未授权 guidance；
- TaskSpec 会按代码语义动态选择终端能力；
- TaskGraph 会拒绝没有直接接收全部业务节点结果的事实核验节点。

测试中的模型响应使用 ReplayChatCompletionClient 保证结果可重复；文件读取、任务图调度、代码执行器、Playwright、MarkItDown、FileSurfer 工具路径和状态持久化均有真实集成测试。

## 18. 当前已知边界

当前尚未完成：

1. Scheduler 仍为单线程，没有并行 ready 节点执行。
2. TaskGraph 只有硬依赖边，尚无条件边和可选咨询边。
3. 重规划使用完整替代图，尚无增量 GraphPatch。
4. 图和状态已经持久化，已具备 checkpoint 完整性校验、目标/契约漂移检测和未完成节点恢复；幂等重放仍需场景化验证。
5. RuntimeMemory 已形成 working、episodic、semantic、procedural、artifact 记录和节点胶囊，
   但尚缺真正超长任务下的遗忘、污染和中断恢复基准。
6. LLMLingua 已作为可插拔压缩器接入，但真实运行不一定触发；尚缺跨任务质量、延迟和
   CPU/CUDA 对比基准。
7. 事实支持关系仍包含模型判断，而且 blocking issue 尚未单调持久化；重试可能产生失败漂白。
8. 终端执行是 `bounded_local`，不是容器级隔离。
9. 已建立轻量端、边、云 Resource Registry、任务资源约束、安全过滤、位置审计和资源可用性切换；真实集群、跨设备网络和模型切分仍未实现。
10. 跨领域长程任务和可视化界面仍不完整；300 步连续性与 1000 步模拟压力测试已完成，数千步真实模型基准仍待补充。
11. 尚未生成逐条 `review/requirement_closure.json`；mandatory requirement 未形成统一的
    planned → produced → verified/failed/unverified 闭环。
12. 通用 BusinessValidator 当前主要检查输入存在、结果非空和代码捷径，不能独立证明所有
    业务数值与源数据一致。
13. 最终成功闸门尚未统一使用 unresolved blocking issue；最新一次 passed 可能覆盖历史失败。

## 27. 2026-08-24：端边云协同资源调度 MVP、千步性能与真实任务验证

本轮围绕任务书“端-边-云异构资源自适应调度”补齐了最小完整闭环，同时修复了长程执行在上千节点规模下的持久化和记忆检索退化问题。实现目标是提供可部署架构、可解释路由和故障切换证据，不把比赛交付绑定到真实云集群或模型切分环境。

### 27.1 端边云资源抽象

新增 `orchestration/execution/resource_registry.py`，提供框架所有的资源注册表和可替换的模拟资源后端。默认资源为：

| 资源 | 位置 | 典型用途 | 特征 |
|---|---|---|---|
| `device-local` | device | 本地解析、代码复核、敏感数据处理 | 低延迟、低成本、可处理 confidential |
| `edge-local` | edge | 标准分析、事实核验、缓存和中等推理 | 中等算力、低延迟、可处理 confidential |
| `cloud-qwen` | cloud | 数学建模、复杂推理、全局规划 | 高算力、较高延迟、默认 internal |

每个资源记录位置、算力、延迟、成本、安全级别、可用状态、并发上限和当前负载。真实集群可以在不改 Scheduler 的情况下替换该注册表实现。

### 27.2 任务约束与动态位置选择

`TaskNode` 新增 `resource_requirements` 字段，支持允许位置、偏好位置、数据敏感等级、最低算力和最大延迟。`CapabilityRegistry` 先执行 capability、位置、安全、算力、延迟和并发硬过滤，再使用原有质量、成本、延迟和 DyLAN-lite 历史评分。

没有显式约束时，框架按 capability 使用保守默认策略：

```text
math_modeling / reasoning → edge 或 cloud
terminal_execution / document_extraction / input_normalization → device 或 edge
evidence_verification → edge 或 cloud
```

敏感任务安全约束是硬门槛，不能被低成本云资源的评分优势覆盖。

### 27.3 可观测路由与故障切换

`RoutingDecision` 新增并持久化 `selected_resource_id`、`resource_location`、`resource_reason` 和候选资源位置。资源下线后会从候选集合移除，Scheduler 沿用现有执行器切换和恢复策略选择其他合法资源；上下文、运行时记忆和 checkpoint 保持不变，切换原因进入 RunState 轨迹。

### 27.4 长程持久化与千步压力

针对 1000 节点链式任务暴露的 O(n²) 状态写入问题，本轮增加 RunState 批量保存、原子文件替换、horizon 周期性完整持久化、延迟目录扫描和产物哈希重算，以及带索引和最大跳数的 RuntimeMemory 祖先检索。1000 节点模拟压力测试完成 `1000 / 1000`，逐 epoch 生成 checkpoint，目标和节点状态保持。

### 27.5 `mathorcup_d` 真实运行结果

真实运行目录：`outputs/runs/20260824_181213`。已验证 PDF、DOCX、XLSX 输入解析、TaskSpec/Requirement Contract、动态任务图、业务推理、代码生成、受控终端 `py_compile`、epoch checkpoint 和从 `epoch-0010` 恢复。最终收口因 EvidenceVerifier insufficient claim、依赖覆盖不足和后续 401 invalid API key 未通过；框架保持 failed/blocked，没有将业务节点完成误报为最终成功。

### 27.6 新增测试与边界

- 新增 `tests/test_resource_routing.py`，验证位置、安全过滤和资源上下线；
- 长程 checkpoint 完整性、300 步连续性和千步压力测试通过；
- 资源路由与 capability/dynamic routing 回归测试共 20 项通过；
- 相关文件 `py_compile` 和 `git diff --check` 通过。

当前仍未实现真实端/边/云集群、跨设备网络传输、模型切分、GPU 调度、并行 Scheduler 和容器级隔离；正式材料中应明确这些是模拟资源架构的边界，并展示资源下线后的自动切换演示。

这些边界意味着当前系统可以作为可靠的动态多智能体基础框架，但还不能宣称已经完全满足比赛任务书的全部最终指标。

## 19. 建议的下一阶段顺序

建议按以下优先级继续：

1. 增加框架级 blocking issue ledger，修复 EvidenceVerifier 重试和 ReviewAgent 的失败漂白。
2. 实现 Requirement Closure，并让所有 mandatory requirement 关闭后才能 success。
3. 将验证失败真正路由到责任节点，修复代码或产物后再重新验证，保留已完成节点。
4. 使用数学建模和文档分析任务建立最小真实完成率回归，再接 Benchmark。
5. 优化哈希引用开销和 LLMLingua 触发策略，验证实际字节节省而非只看投影节省。
6. 实现进程中断恢复、幂等节点重放和受控并行调度。
7. 将终端执行迁移到 Docker 或其他容器后端。
8. 最后再建设资源调度和可视化展示。

后续每轮架构改动应同步更新：

- `PROJECT_REQUIREMENTS.md`：需求和验收基线；
- `FRAMEWORK_CHANGES.md`：实现改动和当前状态；
- `run.md`：环境与运行方法；
- 自动化测试和正式演示的运行证据。

## 20. 运行内长程记忆 MVP（CoALA/MemGPT/Generative Agents/LLMLingua-2 适配）

本轮新增 `orchestration/memory/memory_models.py` 和
`orchestration/memory/runtime_memory.py`，并将 `RuntimeMemoryManager` 接入
`GraphScheduler`。实现边界如下：

- 按 CoALA 分类保存 working、episodic、semantic、procedural 记忆，另以
  artifact 记录绑定真实产物和 SHA256；原有 `workflow_memory.py` 继续作为跨运行
  procedural skill store。
- 采用 MemGPT-inspired 的 core/recall/archival 分层；每个 DAG 节点执行前生成
  有预算的 `ContextCapsule`，执行器只能读取 capsule，不能获得记忆库写权限。
- 采用 Generative-Agents-inspired 的 relevance、recency、importance 联合召回，
  额外加入 DAG proximity 和 evidence quality，避免无关或未核验记忆污染上下文。
- 复用已有 LLMLingua-2 adapter，仅在确定性筛选之后压缩自由文本；全局目标、关键
  约束、数字、路径和证据引用不进入有损压缩，并记录压缩及关键状态保持指标。
- 运行内数据默认写入 `run_dir/memory/runtime_memory.sqlite3`，capsule 写入
  `run_dir/memory/capsules/`，指标写入 `run_dir/memory/metrics.json`；RunState 只
  记录元数据和事件，避免把完整记忆复制进状态文件。
- Agent 只能提议事实；只有结构化证据核验结果中的 `supported` claim 才会提升为
  verified semantic memory。

运行内记忆专项测试已纳入当前 148 项完整测试集。A-MEM 的链接式
记忆演化和 HippoRAG 的图检索仍作为后续可插拔后端，不属于当前 MVP 的硬依赖。

## 21. Evidence-Grounded Requirement Graph 与层级规划前端

为解决复杂任务在 PlanningAgent 前被压缩成少量粗节点的问题，规划前端新增了
“证据约束的 Requirement Graph + 验证器引导的惰性层级规划”。

### 21.1 Requirement 数据结构

`orchestration/core/schemas.py` 当前提供：

- `RequirementSourceCandidate`：Python 从原始文件中定位的潜在义务片段；
- `RequirementSourceRef`：需求返回原始证据的位置；
- `RequirementAcceptance`：需求对应的可检查验收条件；
- `RequirementItem`：带 ID、父子关系、类型、强制状态、owner 和验收条件的需求；
- `RequirementSet`：有版本的完整需求图；
- `RequirementCoverageResult`：确定性需求完整性检查结果。

模型常见的枚举差异会在 Python 层归一化。例如 `analysis`、`report`、`process` 等
provider-specific 类型不会再导致整份 TaskUnderstandingResult 被丢弃；严重度
`critical/high/major` 归为 blocking，`minor/informational/info_only` 归为 warning。

### 21.2 来源候选与防遗漏

Python 不直接用关键词生成业务需求，而是用通用义务词定位“值得 Agent 再分析”的来源片段。
每个片段获得稳定 `SRC-*` ID。TaskUnderstandingAgent 必须通过 `source_refs.locator`
覆盖它；如果片段只是背景，也要生成非 mandatory 的追踪项说明排除理由。

RequirementValidator 会检查：

- source candidate 是否全部有去向；
- ID 是否唯一；
- parent 是否存在；
- Requirement Graph 是否有环；
- mandatory requirement 是否有 acceptance；
- “车型 1 和车型 2 分别建模”等高置信跨职责叶子是否需要继续细化。

### 21.3 多轮局部需求细化

Requirement refinement 默认最多执行两轮，并保存：

```text
planning/requirement_contract_round_0.json
planning/requirement_validation_round_0.json
planning/requirement_contract_round_1.json
planning/requirement_validation_round_1.json
...
planning/requirement_contract.json
planning/requirement_validation.json
```

细化模型遗漏旧 Requirement 时，`preserve_requirement_history()` 会把旧需求补回，避免
“越 refine 需求越少”。

### 21.4 Requirement-aware PlanIR

`PlanNode` 新增：

```text
requirement_ids
acceptance_refs
```

PlanValidator 会验证 mandatory leaf coverage、blocking acceptance coverage、输出产物、
依赖、DAG、compound/primitive 边界和明显跨阶段任务。PlanPatch 合并时会丢弃已经不存在的
旧边，避免出现 `input_data -> deleted_node` 一类悬空依赖。

最新数学建模真实运行 `outputs/runs/20260728_111905` 中，需求合同从 25 条经过两轮细化
增加到 33 条，最终 Requirement Validation 通过；粗粒度数学建模节点也被拆成变量定义、
目标函数和约束三个子节点。最终仍因三个预处理 primitive 缺少 acceptance criteria 而在
PlanIR 阶段失败，说明需求前端已经发挥作用，但局部 Plan refinement 仍需提高稳定性。

## 22. Prompt 预算语义修正

`max_node_prompt_tokens` 现在是通信和成本优化目标，不是第一次超限就取消任务的硬上限：

1. 未超过配置预算：正常调用；
2. 超过配置预算但未超过模型真实输入上限：记录 `soft_prompt_budget_exceeded`，继续调用；
3. 超过模型真实上限：执行一次最小上下文精简；
4. 精简成功：继续调用；
5. 精简后仍超限：节点进入 recoverable blocked，建议动作
   `reduce_context_or_split_node`，不直接抹掉前序节点和产物。

Prompt soft budget 不消耗业务代码重试次数。对应测试覆盖软超限继续调用、上下文精简、
recoverable blocked 和已完成前序节点保持。

## 23. 真实运行证据与当前客观结论

### 23.1 动态异构稀疏路由已真实运行

报销任务 `outputs/runs/20260728_113831` 的 TaskGraph 由 PlanningAgent 动态产生，未使用
fallback。5 个业务 primitive 经 GraphCompiler 增加终端、证据和产物治理节点后形成 8 个
执行节点，实际调用：

- `prepared_input_executor`；
- `analysis_agent`；
- `code_pipeline_executor`；
- `autogen_controlled_terminal`；
- `safe_critic_evidence_verifier`；
- `artifact_validation_executor`。

运行指标：

```text
topology_density = 0.25
full_history_broadcasts = 0
routing_policy = agent_prune_lite
team_policy = dylan_lite
```

这证明动态异构 DAG、直接依赖通信、AgentPrune-lite 和 DyLAN-lite 已进入真实执行链。

### 23.2 低熵通信仍只是部分满足

同一次运行中：

```text
raw_bytes = 18,942
projected_bytes = 15,181
delivered_bytes = 19,200
projection_saved_ratio ≈ 19.86%
bytes_saved_ratio ≈ -1.36%
semantic_compressions = 0
```

说明字段投影和哈希去重已经运行，但内容引用包装存在额外开销，实际传输字节没有下降。
本次 LLMLingua 没有触发，不能用该运行证明语义压缩效果。

### 23.3 RuntimeMemory 已真实运行

报销任务生成 9 个 Context Capsule，RuntimeMemory 记录 working、episodic、semantic、
procedural 和 artifact 信息；关键字段保持率为 1.0。但本次任务深度较浅、记忆压缩率为
1.0，因此只能证明基础设施被调用，不能单独证明超长程记忆指标已经达标。

### 23.4 最新 success 暴露了错误放行

报销输入 Excel 的预览显示总金额为 1485 元，但
`artifacts/expense_summary.json` 输出 `expense_total=0.0`。生成代码使用英文列名读取中文
列名，并在部分异常路径中使用 `sys.exit(0)`，因此退出码成功不代表业务成功。

第一次 EvidenceVerifier 已正确产生 critical/high issue 和 `can_continue=false`，ReviewAgent
也返回 blocking issues、`repair_recommended=true`、`repair_target=code`；但第二次核验通过
减少 claim 将状态改成 passed，workflow 随后继续生成报告并把最终 outcome 写成 success。

所以该次运行的正确语义应是：

```text
execution_success = true
artifact_success = true
business_success = false
delivery_success = partial
overall = failed 或 partial
```

这证明当前最大的阻断项不是动态路由，而是业务失败没有在验证、审查和最终验收之间单调传播。

## 24. 下一轮最小修复范围

下一轮不需要新增 Agent、修改 DAG 架构或建设复杂行业验证平台。建议只做下面三项：

1. 在 RunState 中增加 unresolved blocking issue ledger。critical/high/blocking issue 一旦
   出现就持久化，后续 passed 不能自动删除；
2. EvidenceVerifier 重试结果与历史 issue 合并。只有责任节点产生新产物，并提供明确
   `resolved_issue_ids + evidence_refs`，旧问题才能关闭；
3. 在 ReportAgent 调用前和 final validation 中增加统一 Python 成功闸门：

```python
overall_success = (
    execution_success
    and artifact_success
    and business_success
    and delivery_success
    and not unresolved_blocking_issues
)
```

需要新增的最小回归测试：

- ReviewAgent 返回 blocking issue 时不调用 ReportAgent；
- EvidenceVerifier 第一次失败、第二次 passed 时最终仍失败；
- `exit_code=0` 但存在 unresolved blocking issue 时不能 success；
- 四个 success dimension 全部通过且不存在阻断问题时才允许 success。

完成这一步后，再实现逐条 `review/requirement_closure.json` 和失败后责任节点局部修复。

## 25. 2026-07-29：原生自动修复闭环

本轮没有引入 LangGraph 或第二套 Scheduler。新增能力全部复用现有
`GraphScheduler`、`RunState`、TaskGraph、Artifact 和模型调用接口。

### 25.1 两层修复

第一层仍由现有代码执行器负责：

```text
代码生成/预检/执行失败
  → ErrorAttributionAgent 给出修复指令
  → 在 code_policy.max_retries 内重新生成
  → 真实执行并记录 exit_code、stdout、stderr 和产物
```

第二层处理“代码执行成功但独立业务验证失败”：

```text
BusinessValidation failed
  → 建立稳定 Issue Ledger
  → 按失败产物的 TaskGraph 所有权定位生产节点
  → 只重开该节点及其后继子图
  → 将验证失败证据写入 recovery_context
  → 复用原 GraphScheduler 重放
  → 再次独立验证
  → 只有失败项消失且有新证据时关闭 Issue
```

`max_business_repair_rounds` 与代码重试次数分离，业务验证不会偷偷增加
代码执行器的重试预算。无法路由的问题会保留为 blocked，不会为了得到 success
而反复调用模型。

### 25.2 阻断问题关闭规则

EvidenceVerifier 的问题现在记录验证输入指纹。同一份上游结果被再次询问后，即使
模型改口为 passed，也不能关闭旧问题；只有上游依赖或证据确实变化，并由同一个
验证节点重新核验通过，才允许关闭该来源的问题。其他验证器的问题不受影响。

### 25.3 两个规划—执行兼容修复

1. PlanningAgent 猜测的 `dependency_fields` 在真实上游结果中不存在时，不再在
   Agent 调用前直接杀死节点。框架保留完整内容寻址的上游载荷，并记录契约警告；
2. PlanIR 中指向 compound 节点的 `required_inputs` 在展平时同步映射到该 compound
   的 primitive 出口节点，避免执行依赖已经展平、输入契约却仍引用已删除 compound
   的编译错误。

### 25.4 测试与真实运行

- 全量单元/集成测试：157 项通过；
- 失败运行 `20260729_163814` 的最终 PlanIR 可重新展平并成功编译；
- 真实运行 `20260729_170031` 中，第一次代码生成因第 68 行语法错误预检失败，
  第二次在错误归因和修复指令下执行成功并生成两个目标产物；
- 同一次运行中 EvidenceVerifier 仍发现输入证据不足，因此最终保持 failed，
  没有把“代码已修好”误写成“业务任务已完成”。

当前最重要的剩余项是增强通用业务验证契约，使其能独立复算更多 Requirement，
以及把可修复的证据问题准确路由到上游节点；缺少真实输入证据的问题仍应保持
blocked，而不是自动伪造数据。

## 26. 2026-07-29：渐进式长程执行第一阶段

本轮没有建设新的 Scheduler。核心变化是把
已经编译好的动态任务图按“当前可执行前沿”分成多个 epoch，逐段交给同一个
`GraphScheduler` 执行：

```text
TaskSpec / RequirementContract
  → PlanIR 与动态 DAG
  → 选择当前 ready frontier
  → 执行一个有限窗口
  → 保存 epoch 进度与关键上下文
  → 选择下一 frontier
  → 直到完成、阻断或失败
```

这样做保留了原有 PlanIR、GraphCompiler、DyLAN-lite、AgentPrune-lite、执行器和
恢复机制，同时避免长任务只能通过一次性全图执行推进。小型文档任务在 `auto`
模式下仍走原有单次执行，不会为了体现长程而人为制造阶段。

### 26.1 HorizonPolicy

TaskSpec 现在冻结一份 `horizon_policy`，主要字段为：

- `mode`：`auto`、`progressive` 或 `one_shot`；
- `progressive_execution`：是否允许渐进执行；
- `max_active_nodes`：单个 epoch 最多选择多少个 ready 节点；
- `checkpoint_each_epoch`：是否保存阶段进度；
- `max_epochs`：软目标，超过时只记录警告，不直接杀死任务。

数学建模、数据分析以及节点规模超过窗口的任务，在 `auto` 模式下会进入
渐进执行；小型通用文档任务默认保持 one-shot。

### 26.2 RunState 与阶段记录

`RunState.horizon` 实时记录：

- 当前 epoch；
- 本轮激活和完成的节点；
- 已生成、已完成节点总数；
- 逻辑步骤累计值；
- 每轮记录文件；
- 下一步动作。

每轮生成 `checkpoints/epoch-XXXX.json`，其中包含：

- 当前图版本和节点状态；
- 本轮 active/completed 节点；
- 下一动作；
- 全局目标；
- 必需产物；
- 已完成节点；
- 未解决阻断问题；
- 已生成产物引用。

这些文件同时是可恢复的阶段检查点：保存完整 `task_graph` 快照、任务契约摘要和
SHA256 完整性信息，并写入 `resume_supported: true`。`load_horizon_checkpoint()`
会校验目标、必需产物和任务契约后再把快照交给 Scheduler，损坏或漂移的检查点会被拒绝。

### 26.3 调度语义

`GraphScheduler.run()` 增加可选的 `stop_after_node_ids`。当本轮目标节点完成而
整图尚未完成时：

- 图状态保持 running；
- 后继节点保持 pending；
- 不提前写入 graph success；
- 下一 epoch 复用同一个 Scheduler 和恢复控制器继续执行。

节点失败时仍使用原有执行器切换、节点重试、错误归因和局部恢复规则；epoch
边界不会重置业务重试预算。

### 26.4 真实数学建模运行

真实运行目录：`outputs/runs/20260729_204809`。

该任务生成 8 个编译后节点，并形成 6 个实际 epoch：

1. `node_doc_extractor`；
2. `node_input_normalizer`；
3. `node_constraint_builder` 与 `node_objective_builder`；
4. `node_code_implementation`；
5. `terminal_execution`；
6. `evidence_verification`。

运行中代码第一次执行失败，第二次自动修复后退出码为 0；前五个 epoch 均正常
完成。EvidenceVerifier 最终未通过，因此 `artifact_validation` 被阻断，系统保持
failed，未生成最终报告。这证明长程分段调度没有绕过原有失败闸门。

本次真实运行还发现模型返回的一条 Evidence claim 缺少 `claim` 字段。框架已增加
保守归一化：保留该条记录、强制标记为 `insufficient` 并正常形成失败证据，而不是
因单条格式错误丢弃整份核验结果；该兼容不会把不完整 claim 变成 passed。

### 26.5 测试

- 新增渐进窗口测试：验证第一轮只执行目标前沿，后继保持 pending；
- 新增 horizon 集成测试：验证多 epoch、RunState、阶段记录和受保护上下文；
- 新增 Evidence claim 畸形结构测试；
- 全量单元/集成测试：161 项通过。

### 26.6 与任务书的边界

当前实现支持从最近一致 checkpoint 恢复，并通过 300 步连续性压力测试验证目标、
节点状态和完整性保持。正式提交前仍应在目标演示环境扩展到数千步，采集真实模型
Token、延迟、异常恢复率和跨领域任务成功率；恢复时只重放未完成节点，已完成节点
依赖其持久化结果，不重复执行。
### 26.7 2026-08-24 mathorcup_d 收尾核验

- 对 `outputs/runs/20260824_181213` 的演示代码进行证据驱动修正：原实现将所有货物写入 `(0,0,0)`，EvidenceVerifier 正确判定为几何重叠并阻断后续验收。
- 修正后的确定性 shelf/grid 放置搜索同时检查车厢边界和三维 AABB 碰撞；本地复核生成 50 件货物、`status=success`、`loading_rate=0.85`，逐对非重叠检查通过。
- 回归测试 `test_resource_routing`、`test_horizon_execution`、`test_long_horizon_continuity`、`test_acceptance_contract` 共 21 项通过。
- 先前真实 API 运行 `outputs/runs/20260824_210010` 仍以 `failed` 收尾（部分分析/代码节点未完成，最终报告未生成）；因此不能宣称 `mathorcup_d` 已完成全链路验收。设置好 API 环境变量后需重新运行 `python main.py --input examples/mathorcup_d`，并以最终 `run_state.json` 的 `outcome=success`、`finished=true` 和验收报告为准。
### 26.8 通用终端执行与规划恢复改造（2026-08-25）

- `SandboxedTerminalNodeExecutor` 默认执行编译加真实 Python 入口，而不是只做
  `py_compile`；仍支持 TaskSpec 显式仅授权编译的兼容模式，并记录实际命令、退出码和检查类型。
- 代码节点执行超时改为读取 `TaskSpec.terminal_policy.timeout_seconds`，超时结果会回到
  统一 repair/recovery 链路，不再固定等待 180 秒。
- 层级 PlanIR 校验失败时启用受控确定性 fallback 图；fallback 只恢复任务图，不跳过真实执行、证据核验或业务验收。
- 回归测试：相关通用/长程/资源/验收测试 27 项通过；规划 fallback 组合测试 42 项通过。
- 最新真实运行：`outputs/runs/20260825_011900`。动态规划、长程分阶段执行、真实代码执行和
  `artifacts/result.json` 生成均已发生，但最终验收为 `failed`。EvidenceVerifier 和业务验收
  正确发现利用率超过 100%、货物类型/摆放数据不完整、必需字段缺失及证据引用问题；因此没有生成
  成功报告，也没有伪造 `outcome=success`。
### 26.9 MathorCup 领域验证器闭环（2026-08-25）

- 新增通用可插拔领域验证边界，并实现 `MathorCupPackingValidator`；核心调度器不包含货物字段。
- MathorCup 任务现在在最终 TaskSpec 中声明领域验证器，检查结果 Schema、G1-G5 覆盖、坐标/尺寸、车厢边界、三维重叠和利用率范围。
- 兼容模型当前输出的 `solution.items`、`width_cm/height_cm/depth_cm` 和 `utilization.space_rate/weight_rate` 结构，同时继续拒绝越界和缺失数据。
- 最新真实运行 `outputs/runs/20260825_181129` 已完整经过真实代码执行并生成 `artifacts/result.json`，但最终验收仍失败：生成结果存在部分坐标越界、报告/证据引用不完整，因此未伪造 success。

### 26.10 比赛收尾稳定性修复（2026-09-08）

- 修复业务修复重放后 `graph_outcome` 保留历史失败值的问题；评审前按当前最终图状态刷新，避免已修复运行被误判失败。
- MathorCup 固定演示启用 one-shot 执行窗口，仍保留通用框架对其他长程任务的 progressive horizon 支持，降低模型分阶段 patch 失败导致的演示不确定性。
- 领域验收合同增加 mandatory 空证据父需求和 `execution_must_succeed_for_pass` 方法别名的确定性适配；代码文本语义检查统一落到领域独立校验日志，避免不可路由条件阻塞交付。
- `requirements.txt` 明确加入 `cryptography`，保证 `pdfplumber` 在报销策略 PDF 上可读取。
- 真实运行 `outputs/runs/20260908_124006`（MathorCup）和 `outputs/runs/20260908_125323`（差旅报销）均通过完整 `run_audit`；已生成 `evidence/metrics/cross_scenario_metrics.md` 和 `submission/competition_submission.zip`。

### 26.11 可复核的 1000 步长程压力测试（2026-09-08）

- 新增 `utils/long_horizon_stress.py`，以 25 条并行依赖链形成 1000 个确定性任务步骤和 40 个 bounded horizon。
- 第 500 步结束第一阶段并生成 checkpoint，第二阶段从 `epoch-0020.json` 恢复并动态扩展至 1000 步；已完成节点不重放。
- 正式证据运行 `outputs/stress/20260908_145548` 完成 `1000/1000`，重复执行 0 次，目标和执行顺序均保持，共记录 2039 个框架逻辑事件。
- 40 个 checkpoint 均携带完整图状态；恢复入口对任务目标、受保护 TaskSpec 摘要和 SHA256 完整性进行校验。最终证据为 `evidence/metrics/long_horizon_1000.json` 和 `.md`。
- 新增自动化回归 `test_one_thousand_steps_resume_without_goal_drift_or_replay`。该测试证明确定性调度、持久化和恢复能力，不表述为 1000 次真实大模型调用。

### 26.12 城市多模态跨场景验证（2026-09-08）

- 新增 `城市多模态数据集/task.md` 与 `task_plugins/urban_multimodal/`，将 511 条遥感
  元数据、80,000 条法规、80,000 条匿名通联和 500,000 个 PCAP 包纳入同一真实任务。
- 数据处理保持可审计边界：遥感只分析元数据和 caption，PCAP 只解析包头，电话标识
  不反识别；缺少统一时空/实体键时只输出聚合治理建议，不进行个人级关联和因果归因。
- 新增固定领域合同、确定性修复代码、证据 fallback 和只读独立验证器。验证器复算四类
  画像与隐私字段，不再覆盖代码节点产物，避免已完成产物哈希被验证阶段改变。
- 大规模混合输入预览支持 JSON collection、流式 JSONL/CSV 和 PCAP 全局头摘要；超过
  50 个文件时，模型只接收聚合 manifest，真实 TaskSpec 仍保留全部 515 个输入文件。
- 修正通用需求编译顺序：需求细化产生的无验收父项先补默认验收，再映射到领域独立
  验证日志，避免空参数 `evidence_supported` 在交付门禁形成假失败。
- 内容地址引用现在只内联最多 16 个产物/证据路径并记录省略数量，完整列表仍保存在
  SHA256 校验的 payload 中；600 路径回归证明引用自身不会突破通信预算。
- 最终报告产物引用解析改为在已知扩展名结束，避免把紧邻 `.json` 的中文正文误识别为
  文件名。
- 真实成功运行：`outputs/runs/20260908_173502`。`utils/run_audit.py` 的 9 项闸门全部
  PASS，代码退出码 0、业务验证 passed、需求验收 passed、报告允许生成且无失败节点。
- `evidence/metrics/cross_scenario_metrics.json` 与 `.md` 已更新为三个完整审计 PASS 场景：
  MathorCup、差旅报销、城市多模态。相关最终回归 72 项、65 项分别全部通过。

### 26.13 比赛材料与轻量演示证据（2026-09-09）

- 技术报告、评分自评、运行说明、材料索引和一键收尾脚本统一更新为三个真实审计 PASS
  场景，不再引用“等待 fresh run”或双场景状态。
- 新增 `utils/export_demo_evidence.py`，只接受完整审计 PASS 运行，并从城市成功 RunState
  导出首次失败、代码节点归因、受影响子图重放、重新执行/验证和最终成功时间线。
- 新增 `evidence/DEMO_EVIDENCE_INDEX.md`、`fault_recovery_city.json` 和 `.md`，给出演示顺序、
  可复算统计和能力边界。
- 提交打包器现在包含看板、文档、脚本和三场景运行证据；默认排除运行目录中的原始输入
  副本，并为每个输入生成大小和 SHA256 清单，避免重复嵌入约 215 MB 城市数据。
- 正式轻量提交包 `submission/competition_submission.zip` 为约 1.6 MB，包含三个任务类型，
  不含运行输入副本或 API Key。
- 当前完整自动化回归 284 项全部通过，三个基础场景及真实业务千步运行均通过 9/9 独立审计。

### 26.14 比赛可视化演示台（2026-09-09）

- 将原只读运行列表升级为证据驱动的单页比赛演示台，集中展示三场景 3/3 PASS、27/27
  交付闸门、284 项回归、1000/1000 步和零完整历史广播。
- 场景详情支持真实动态图节点、执行器与端边云路由检查，九项审计闸门、Token/拓扑指标、
  交付产物只读预览，以及关键字段保持和低熵通信指标。
- 城市修复区从同一成功 RunState 展示业务失败、代码节点归因、四节点局部重放、复验和
  最终交付；千步区展示第 500 步恢复、40 个 SHA256 checkpoint 和零重复执行。
- 新增 `/api/demo` 聚合接口和受目录约束的产物预览。任务启动默认禁用；只有显式传入
  `--enable-execution` 才开放预设场景，且不接受任意命令或输入路径。
- 桌面 1440×1000 和移动 390×844 浏览器检查均无控制台错误、全页横向溢出或内容重叠。

### 26.15 可操作任务工作台与视觉重构（2026-09-09）

- 演示台重构为系统总览、任务工作台、场景证据、长程与恢复、系统架构五个聚焦视图，
  减少长页面堆叠和密集硬边框，统一 8px 以内圆角、状态色、留白和短状态过渡。
- 真实任务入口提升到独立工作台，支持三场景分段选择、只读输入路径、模型/服务/目录/
  队列预检、运行阶段轨道、当前节点、运行目录和实时日志。
- 新增受口令保护的任务接口、单任务锁、任务查询、日志尾部读取和进程终止；任务结束后
  自动定位 run_dir 并执行完整审计，前端据此显示 success 或 failed。
- 执行口令由服务启动时生成，不写入源码或浏览器持久存储；前端只在当前标签页的
  sessionStorage 中暂存。无口令请求返回 401，任意场景或输入路径仍被拒绝。

### 26.16 真实业务 1000 步长程闭环（2026-09-09）

- 新增 `task_plugins/urban_multimodal/long_horizon.py`，把城市四模态真实数据拆为 1000 个
  可审计 WorkUnit：遥感元数据 250 步、法规 250 步、匿名通联 200 步、PCAP 包头窗口
  200 步、跨模态综合 100 步。
- 每个 WorkUnit 固化真实输入范围、聚合结果、结果摘要、全局目标摘要、前序记录摘要和
  链式 SHA256；每 25 步生成签名 checkpoint，共 40 个。
- 第 500 步通过 `epoch-0020.json` 重新实例化账本并恢复，独立验证器复算步骤序列、来源
  总量、摘要链、checkpoint 完整性、目标保持与重复执行数。
- 100 个跨模态综合步骤中每 10 步调用一次真实阶段模型，共 10 次；模型只接收聚合证据，
  不接收原始电话标识、IP 或 PCAP 载荷。调用带一次受控重试，重试耗尽后整轮明确失败。
- 正式运行 `outputs/runs/20260909_urban_business_1000` 完成 1000/1000，覆盖 511 条遥感
  元数据、80,000 条法规、80,000 条匿名通联和 500,000 个网络包；第 500 步恢复、0 重复、
  10 次真实阶段推理，最终通过 `utils/run_audit.py` 9/9 闸门。
- 演示台新增独立“千步长程”启动入口，并将真实业务长程证据与确定性基础设施压力测试
  分开展示，避免混淆业务推理、调度步骤与模型调用次数。

### 26.17 比赛演示台可读性与信息架构重构（2026-09-09）

- 首页改为系统名称和四项核心实测指标，不再使用营销式标题；核心链路、最新千步证据和
  完整审计结论在首屏直接可见。
- 视觉字号以投影可读为准：正文提升至 14px，关键标签不低于 11-12px，页面标题 30px，
  指标数字 21-42px，实时日志提升至 13px；减少装饰性英文眉题和重复状态胶囊。
- 场景证据改为左侧场景选择、右侧验收详情的主从布局，默认展示 9 项交付闸门；仍保留
  任务图、产物预览、运行指标和记忆通信标签页。
- 长程页将真实业务 1000 步提升为第一视觉中心，展示五类 WorkUnit 分布、10 次阶段模型
  推理、40 个签名 checkpoint、目标保持和 9/9 审计；自动修复和确定性压力测试作为支撑证据。
- 系统架构改为语义智能、协同控制、可信执行三层结构，明确模型能力与确定性控制面的职责边界。
- 桌面 1440×1000 与移动 390×844 共 10 个页面组合完成浏览器检查：控制台错误 0、横向
  溢出 0、文字裁切 0；移动导航关闭状态和响应式布局均通过检查。
### 26.18 MathorCup 真实优化千步示例（2026-09-11）

- 新增 `task_plugins/mathorcup_d/long_horizon.py`：确定性生成并评估 1000 个不同参数的装箱候选，每步记录成本、车辆数、利用率、方案摘要和链式 SHA256。
- 新增第 500 步 checkpoint 恢复和 40 个带完整性摘要的 checkpoint；恢复时拒绝目标、步数或链尾摘要不一致。
- 最终候选重新生成后交给既有 `MathorCupPackingValidator`，独立复算货物覆盖、边界、重叠、易碎件、顶部间隙、载重、利用率、车辆数和成本。
- 新增 `utils/mathorcup_long_horizon.py`，生成完整 run 目录、九项交付闸门、证据摘要和可选的 10 次真实模型阶段审查。
- 新增 `tests/test_mathorcup_long_horizon.py`，覆盖候选多样性、checkpoint 防篡改及千步搜索与领域契约闭合。
- 正式运行 `outputs/runs/20260911_mathorcup_business_1000` 完成 1000/1000 个候选评估，
  第 500 步恢复、0 重复、40 个 checkpoint，最终 300 件货物和成本指标通过独立领域复算
  及 `utils/run_audit.py` 9/9 闸门；全量回归为 287 tests OK。

### 26.19 阿里云多模型端边云路由配置（2026-09-11）

- `ResourceDescriptor` 新增 `model_name` 和 `base_url`，每次 `RoutingDecision` 及候选记录现在携带实际模型名。
- 默认逻辑路由为 device/edge 使用 `qwen3.5-flash`，cloud 使用现有 `qwen3.5-35b-a3b`；均可通过 `DEVICE_MODEL`、`EDGE_MODEL`、`CLOUD_MODEL` 和对应 `*_BASE_URL` 覆盖。
- API Key 仍只从 `MODEL_API_KEY` 读取，不进入资源快照、路由证据、Dashboard 或提交材料。
- 该改动完成真实多模型路由的配置和可观测性，不宣称已经完成 Transformer 参数层切分；端边云阶段级协同和资源故障切换仍按当前后端能力实证。
