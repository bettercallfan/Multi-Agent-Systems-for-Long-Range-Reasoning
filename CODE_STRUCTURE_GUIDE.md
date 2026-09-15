# 多智能体框架代码结构与阅读指南

> 本文基于当前项目代码编写，用于帮助第一次阅读该项目的人快速理解整体逻辑，
> 再逐步深入到每个目录、核心对象和运行阶段。

---

# 一、先用最简单的话理解整个系统

这个项目不是让一群 Agent 在群聊中自由决定下一步，而是一个由 Python 框架控制的
多智能体任务执行系统。

可以把它理解成一家公司：

- Python 工作流是项目经理，决定当前处于哪个阶段、谁可以工作、什么时候算完成；
- PlanningAgent 是规划人员，只负责提出任务应该怎么拆；
- 各类执行 Agent 是专业人员，负责分析、推理、代码、文件读取或证据核验；
- Scheduler 是派工系统，根据任务节点需要的能力选择合适的执行者；
- RunState 是项目台账，保存每一步的真实状态；
- Artifact 是工作成果，例如代码、JSON 结果和最终报告；
- Validator 是验收人员，不相信 Agent 自己声称“成功”，而是读取真实文件进行检查。

整个系统的主流程可以先记成：

```text
读取用户输入
    ↓
生成并冻结 TaskSpec
    ↓
理解任务，生成 RequirementContract
    ↓
PlanningAgent 生成层级 PlanIR
    ↓
Python 校验、细化并展平为 SemanticGraph
    ↓
GraphCompiler 补充治理节点，生成可执行 TaskGraph
    ↓
Scheduler 按 DAG 依赖选择节点
    ↓
Capability Registry 动态选择 Agent / Executor
    ↓
节点执行、记忆检索、上下文压缩、失败恢复
    ↓
业务验证与技术预审
    ↓
允许后只调用一次 ReportAgent
    ↓
Python 最终验收并写入最终状态
```

系统中最重要的一条原则是：

```text
Agent 可以提出内容，但不能推进流程；
Python 框架才有权修改状态、执行代码和宣布成功。
```

如果只想先读懂项目，建议先按下面顺序看：

1. `main.py`
2. `orchestration/core/workflow.py`
3. `orchestration/task/task_normalizer.py`
4. `orchestration/planning/plan_ir.py`
5. `orchestration/graph/task_graph.py`
6. `orchestration/graph/graph_scheduler.py`
7. `orchestration/execution/default_executors.py`
8. `orchestration/core/run_state.py`

---

# 二、项目的三层结构

整个项目可以分为控制层、智能体层和工具/数据层。

## 2.1 控制层

主要位于 `orchestration/`。

控制层负责：

- 工作流阶段推进；
- TaskSpec 冻结；
- 需求合同生成；
- 规划结构校验；
- DAG 编译和校验；
- 动态路由；
- 上下文裁剪和压缩；
- 状态持久化；
- 失败恢复；
- 产物和最终结果验收。

这部分不能交给 LLM 自由决定，因为它直接关系到系统是否真的执行和是否真的成功。

## 2.2 智能体层

位于 `agents/`。

Agent 主要负责：

- 理解任务语义；
- 生成计划；
- 分析数据；
- 进行推理；
- 生成代码；
- 分析错误；
- 核验事实；
- 审查语义；
- 生成最终报告；
- 提炼可复用工作流经验。

Agent 本身基本只是角色和 Prompt 定义。Agent 什么时候调用、拿到什么上下文、输出是否
可信，均由 `orchestration/` 控制。

## 2.3 工具和数据层

主要包括：

- `utils/`：读取 PDF、Word、Excel，保存 trace 和报告；
- `examples/`：示例输入；
- `outputs/runs/`：每次运行的独立目录；
- `memory/`：跨运行的执行者统计和工作流技能；
- `tests/`：验证框架代码是否仍符合预期。

---

# 三、三个最容易混淆的核心对象

当前规划链路中同时出现了 PlanIR、SemanticGraph 和 TaskGraph。它们不是同一个东西。

## 3.1 PlanIR：层级规划稿

定义位置：

```text
orchestration/planning/plan_ir.py
```

PlanIR 是 PlanningAgent 面向复杂任务生成的层级规划表示。

它允许两种节点：

- `compound`：复合任务，只表示一组需要继续拆分的工作；
- `primitive`：可以由一个执行者完成、产生一个独立结果并单独验收的原子任务。

PlanIR 主要回答：

- 任务需要分成哪些层级？
- 哪些任务仍然过大？
- 每个节点消费什么输入？
- 产生什么逻辑输出？
- 对应哪些需求和验收条件？

PlanIR 还不是 Scheduler 的执行图，因为 compound 节点不能直接执行。

## 3.2 SemanticGraph：纯业务叶子图

定义位置：

```text
orchestration/graph/semantic_graph.py
```

`PlanFlattener` 会删除 compound 节点，把层级关系转换为 primitive 节点之间的真实数据依赖，
最终得到 SemanticGraph。

SemanticGraph 只描述业务工作，例如：

```text
提取数据
    ↓
定义变量
    ↓
构建约束
    ↓
设计求解策略
    ↓
实现代码
```

它不应该包含：

- terminal execution；
- evidence verification；
- artifact validation；
- report；
- final validation。

这些是框架治理工作，不应由 PlanningAgent 自己编造。

## 3.3 TaskGraph：真正执行的 DAG

定义位置：

```text
orchestration/graph/task_graph.py
```

`GraphCompiler` 将 SemanticGraph 编译为 TaskGraph，并根据 TaskSpec 自动增加治理节点。

TaskGraph 中的每个 `TaskNode` 包含：

- `node_id`：唯一节点 ID；
- `description`：任务描述；
- `capability`：需要的能力；
- `dependencies`：直接依赖节点；
- `dependency_fields`：希望从直接依赖结果中保留的字段；
- `input_artifacts`：输入文件；
- `output_artifacts`：声明要生成的文件；
- `success_criteria`：框架可检查的成功条件；
- `max_retries`：节点重试次数；
- `status`、`attempts`、`result`、`error`：运行期状态。

Scheduler 真正读取和执行的是 TaskGraph。

三者关系可以概括为：

```text
PlanIR
层级任务表达，包含 compound 和 primitive
    ↓ PlanValidator + PlanFlattener
SemanticGraph
只保留业务 primitive
    ↓ GraphCompiler
TaskGraph
业务节点 + 框架治理节点 + 严格运行状态
```

---

# 四、从命令启动到最终结束的完整流程

## 4.1 程序入口：main.py

启动方式：

```bash
python main.py --input examples/某个任务
```

`main.py` 主要做六件事：

1. 解析命令行参数；
2. 创建本次运行目录；
3. 读取输入文件；
4. 生成初始 TaskSpec；
5. 创建模型客户端；
6. 调用 `run_explicit_workflow()`。

`main.py` 本身不负责规划、调度和报告，只负责把运行环境准备好。

### 输入准备

调用：

```text
orchestration/task/input_loader.py
```

`load_input()` 判断输入是单文件还是文件夹，并收集其中的文件和任务文本。

### 创建运行目录

调用：

```text
orchestration/core/run_manager.py
```

创建：

```text
outputs/runs/YYYYMMDD_HHMMSS/
```

然后把原始输入复制到本轮 `inputs/`，保证每次运行可审计，不继续依赖外部路径。

### 文件预览

调用：

```text
utils/file_reader.py
```

分别读取：

- PDF；
- DOCX；
- XLSX/XLS；
- Markdown 和文本文件。

预览保存为：

```text
file_previews.json
```

### 创建模型客户端

调用：

```text
config.py
```

主模型读取：

- `MODEL_API_KEY`
- `MODEL_BASE_URL`
- `MODEL_NAME`

FileSurfer 和 WebSurfer 可以使用各自支持 tool calling 的模型客户端。

密钥不应写死在代码或上传到 Git。

---

## 4.2 TaskSpec：全流程唯一任务合同

定义和生成逻辑位于：

```text
orchestration/task/task_normalizer.py
```

调用分两步。

### 第一步：normalize_to_task_spec()

只根据：

- 用户输入文本；
- 文件名；
- `task.md` / `task.txt`；

生成初步 TaskSpec。

它会判断：

- `task_type`
- `code_policy`
- 任务名称
- 基础成功要求

### 第二步：finalize_task_spec()

文件预览生成以后，再把：

- 用户文本；
- 任务文件文本；
- 文件内容预览；

组合起来重新分类，并冻结最终合同。

最终 TaskSpec 中包含：

- 任务类型；
- 代码策略；
- 必需产物；
- 报告章节；
- 成功标准；
- capability 合同；
- 通信策略；
- 路由策略；
- 动态组队策略；
- 恢复策略；
- 记忆策略；
- 规划策略；
- 长程执行策略；
- 业务验证策略。

后续组件只能读取 TaskSpec，不应该再次自行猜测任务类别和重试次数。

---

## 4.3 workflow.py：整个系统的总导演

核心入口：

```python
run_explicit_workflow(...)
```

位置：

```text
orchestration/core/workflow.py
```

这个函数把整条链路串起来，是理解项目最关键的文件。

当前阶段顺序为：

```text
prepare
  ↓
classify
  ↓
plan
  ↓
execute
  ↓
review
  ↓
report
  ↓
final_validate
  ↓
finish
```

阶段只能向前推进，不能由 Agent 在回答中自行切换。

---

# 五、规划阶段的详细逻辑

## 5.1 通用标准化输入

位置：

```text
orchestration/task/input_preparation.py
```

生成：

```text
normalized_input.json
```

它只做通用结构化：

- 记录输入来源；
- 保存结构化文件内容；
- 保留文件预览；
- 建立统一输入边界。

它不包含报销、装箱、法律等某个特定领域的硬编码规则。

## 5.2 构建 Capability Registry

位置：

```text
orchestration/execution/default_executors.py
orchestration/execution/capability_registry.py
```

`build_default_registry()` 注册当前真正可用的执行者。

基础执行能力包括：

- prepared input；
- analysis；
- research；
- reasoning；
- math modeling；
- data analysis；
- code；
- artifact validation。

根据 TaskSpec 动态注册的外部能力包括：

- evidence verification；
- terminal execution；
- document conversion；
- file navigation；
- web research。

所以“系统有哪些 Agent”不是固定等于某个数量，而是：

```text
TaskSpec 需要什么能力
    +
当前环境有哪些真实执行器
    =
本次运行可用的异构团队
```

## 5.3 TaskUnderstandingAgent

代码位置：

```text
agents/task_understanding_agent.py
```

Prompt 构建位置：

```text
orchestration/core/prompt_builder.py
```

它输出：

- 任务摘要；
- 目标；
- 约束；
- 歧义；
- 风险；
- 推荐能力；
- 原子需求建议。

它不生成 DAG，也不能修改 TaskSpec。

结果保存为：

```text
planning/task_understanding.json
```

## 5.4 RequirementCompiler

位置：

```text
orchestration/planning/requirement_compiler.py
```

它解决的问题是：

```text
规划前先确认题目到底要求完成什么。
```

RequirementCompiler 将任务理解结果转成稳定的 RequirementContract：

- 每条需求有稳定 ID；
- 保留来源文件和定位；
- 标明是否 mandatory；
- 标明由 planner 还是 outer workflow 负责；
- 绑定预期产物；
- 绑定 blocking 验收标准；
- 建立父子需求关系。

Python 还会从文件预览中提取可能包含“必须、不得、至少、输出、验证”等词的候选片段，
检查 TaskUnderstandingAgent 是否遗漏题目要求。

如果需求仍然过粗，会再次调用 TaskUnderstandingAgent 局部细化。

主要产物：

```text
planning/requirement_contract_round_0.json
planning/requirement_validation_round_0.json
planning/requirement_contract.json
planning/requirement_validation.json
```

## 5.5 工作流经验检索

位置：

```text
orchestration/memory/workflow_memory.py
agents/workflow_memory_agent.py
```

这是跨运行的“程序性记忆”。

规划前：

- Python 从技能库搜索候选经验；
- WorkflowMemoryAgent 只能从候选中选择；
- 最终选择结果由 Python 过滤。

运行完成后：

- Agent 根据已验证的图和结果提出技能候选；
- Python 去除密钥、绝对路径、未经证实事实；
- 合格经验才写入技能库。

它与运行期 SQLite 记忆不同：

- Workflow Memory：跨任务保存可复用流程经验；
- Runtime Memory：只服务当前 run 的长程上下文连续性。

## 5.6 选择层级规划还是普通规划

位置：

```text
orchestration/planning/hierarchical_planner.py
```

`resolve_planning_policy()` 根据以下因素计算复杂度：

- 是否为数学建模、软件工程或复杂研究任务；
- 是否需要代码；
- 中间交付物数量；
- 输入文件数量；
- 推荐能力数量；
- 约束数量；
- 是否要求产物审查。

简单任务可以使用普通 SemanticGraph 规划。

复杂任务使用：

```text
Verifier-Guided Hierarchical Planning Frontend
```

## 5.7 PlanningAgent 生成 PlanIR

Agent 定义：

```text
agents/planning_agent.py
```

层级规划控制：

```text
orchestration/planning/hierarchical_planner.py
```

PlanningAgent 读取：

- TaskSpec 摘要；
- RequirementContract；
- 可用 capability；
- 任务理解结果；
- 规划深度和节点预算；
- 工作流经验。

输出 PlanIR。

PlanningAgent 不能指定具体 Agent ID，只能声明节点需要的 capability。

## 5.8 Plan 格式适配

位置：

```text
orchestration/planning/plan_format_adapter.py
```

LLM 有时会出现轻微格式偏差，例如：

- 将 dependencies 写成字符串；
- 使用不同的边字段名；
- 节点字段缺失默认值；
- PlanPatch 格式不完全一致。

适配器只修复可以确定的表示差异，不替模型编造业务计划。

它同时生成 format report，记录具体进行了哪些格式修正。

## 5.9 PlanValidator

位置：

```text
orchestration/planning/plan_validator.py
```

它检查：

- 是否存在循环；
- 节点 ID 和依赖是否合法；
- compound 是否真的拥有子节点；
- primitive 是否有明确输出；
- primitive 是否有独立验收条件；
- 交付物是否覆盖；
- 需求 ID 和验收 ID 是否覆盖；
- 数学建模节点是否跨越多个独立职责；
- code 节点是否把算法设计、实现和业务验证混在一起；
- 是否只存在一个 code primitive；
- 节点数量和深度是否超过规划策略。

如果只发现局部问题，返回：

```text
problem_node_ids
revision_instructions
```

## 5.10 refine_plan 与 PlanPatch

PlanningAgent 不重新生成整张计划，而是只对被验证器拒绝的节点返回 PlanPatch。

PlanPatch 包含：

- `target_node_id`
- `replacement_nodes`
- `replacement_edges`

`hierarchical_planner.py` 会：

1. 校验 target 是否真实存在；
2. 限制新增节点数；
3. 把过粗 primitive 转为 compound；
4. 合并局部子节点；
5. 再次调用 PlanValidator；
6. 达到最大修订轮数后停止。

每轮保存：

```text
planning/plan_ir_round_N.json
planning/plan_validation_round_N.json
planning/plan_patch_round_N.json
```

## 5.11 PlanFlattener

位置：

```text
orchestration/planning/plan_flattener.py
```

它把 PlanIR 转成 SemanticGraph：

- 删除 compound；
- 计算每个 compound 的入口和出口 primitive；
- 把指向 compound 的依赖映射到实际叶子节点；
- 保留 requirement_ids、acceptance_refs 和层级路径元数据；
- 检查展平后是否有环。

## 5.12 GraphCompiler

位置：

```text
orchestration/graph/graph_compiler.py
```

它完成从语义规划到可执行图的安全边界。

主要工作：

- 丢弃 Planner 自行生成的治理节点；
- 检查 capability 是否已经注册；
- 绑定代码产物；
- 为代码节点增加 terminal execution；
- 为事实任务增加 evidence verification；
- 增加 artifact validation；
- 设置节点重试次数和成功标准；
- 生成严格 TaskGraph。

例如业务图：

```text
extract → model → code
```

可能被编译成：

```text
extract → model → code → terminal_execution
    └──────────────┬───────────────┘
                   ↓
         evidence_verification
                   ↓
          artifact_validation
```

## 5.13 TaskGraph 最终校验

位置：

```text
orchestration/graph/graph_planner.py
orchestration/graph/task_graph.py
```

主要检查：

- 至少有一个根节点；
- 没有重复节点；
- 没有不存在的依赖；
- 没有环；
- capability 可用；
- 输出路径安全；
- 同一产物不能被多个节点写入；
- 必需能力和代码节点满足 TaskSpec；
- success criteria 可被框架识别。

只有通过校验的图才能进入 Scheduler。

---

# 六、执行阶段的详细逻辑

## 6.1 GraphScheduler

位置：

```text
orchestration/graph/graph_scheduler.py
```

当前 Scheduler 是确定性的单线程调度器。

它每次：

1. 找出所有依赖已经完成的 ready 节点；
2. 按稳定顺序选择一个节点；
3. 根据 capability 请求 Capability Registry；
4. 构造稀疏上下文；
5. 构造运行期记忆胶囊；
6. 调用执行器；
7. 校验执行结果；
8. 更新节点状态和 RunState；
9. 记录执行者质量统计；
10. 决定继续、重试、切换执行器、重规划或阻断。

Scheduler 不会让下游节点读取整个群聊历史，只允许读取直接依赖节点的结构化结果。

## 6.2 AgentPrune-lite

主要实现位置：

```text
orchestration/communication/context_builder.py
```

它不是删除 Agent，而是裁剪节点之间传输的消息。

对于当前节点 B：

```text
A → B
C → D
```

B 只能收到 A 的结果，不会收到 C、D 或整个运行历史。

具体机制：

1. 只读取 `node.dependencies`；
2. 对上游结果使用字段白名单；
3. 保留 summary、structured_output、artifact refs 和 evidence refs；
4. 大结构写入内容寻址文件，消息中只保留 `$ref`；
5. 相同内容按 SHA256 去重；
6. `dependency_fields` 指定的关键字段尽量保留在消息中。

结果写入：

```text
communication/payloads/
communication/structured/
```

统计写入 `run_state.json.communication`。

## 6.3 LLMLingua

实现位置：

```text
orchestration/communication/context_compressors.py
```

支持三种策略：

- deterministic：规则式截断和压缩；
- llmlingua：使用 LLMLingua 模型压缩长文本；
- auto：满足条件时尝试 LLMLingua，否则回退 deterministic。

压缩只作用于可以压缩的自然语言内容。

以下关键状态不会依赖有损压缩：

- 全局目标；
- TaskSpec 核心约束；
- 节点 ID 和依赖；
- 必需产物路径；
- JSON Pointer 指定字段；
- 未解决问题 ID。

默认不自动下载模型，只有显式设置后才允许下载。

LLMLingua 是否真实使用可以从以下位置确认：

```text
run_state.json
  → communication
  → compressor_usage
  → semantic_compressions
```

## 6.4 DyLAN-lite

实现位置：

```text
orchestration/execution/executor_scoring.py
orchestration/execution/capability_registry.py
```

DyLAN-lite 的作用是：

```text
同一个 capability 有多个执行者时，动态选择更合适的执行者。
```

它先做硬过滤：

- 执行器当前可用；
- capability 精确匹配；
- 不在本轮排除名单。

再综合：

- 静态质量分；
- 成本；
- 延迟；
- 历史成功率；
- 历史质量通过率；
- 历史 Token 和耗时；
- 当前任务类型。

每次运行结果会更新 executor stats，后续同类型任务再使用。

这不是神经网络训练，也不需要单独训练集；它是在线统计评分。

## 6.5 Runtime Memory

实现位置：

```text
orchestration/memory/runtime_memory.py
orchestration/memory/memory_models.py
```

每个 run 有独立 SQLite：

```text
memory/runtime_memory.sqlite3
```

保存四类记忆思想：

- working memory；
- episodic memory；
- semantic memory；
- procedural memory。

运行开始时，Python 写入不会被压缩丢失的 core memory：

- 全局目标；
- 任务类型；
- 代码策略；
- 成功条件；
- 报告章节；
- 禁止虚构等关键约束。

每个节点开始前，根据：

- 与当前节点的相关度；
- 时间新近性；
- 重要性；
- 图距离；
- 证据质量；

检索少量记忆，构成 Context Capsule。

胶囊保存于：

```text
memory/capsules/<node>-attempt-<n>.json
```

节点完成后，再把结果、证据和产物写回运行期记忆。

## 6.6 Horizon 渐进式长程执行

实现位置：

```text
orchestration/core/workflow.py
orchestration/core/workflow_policy.py
```

数学建模、数据建模或超过活动窗口的图，可以按 epoch 执行。

每个 epoch：

1. 选择当前 ready frontier；
2. 最多激活 `max_active_nodes`；
3. 调用同一个 GraphScheduler；
4. 当前窗口完成后暂时返回 workflow；
5. 写入阶段记录；
6. 再选择下一个 frontier。

保存：

```text
checkpoints/epoch-0001.json
checkpoints/epoch-0002.json
...
```

这些文件保存：

- 当前节点状态；
- 本轮激活节点；
- 本轮完成节点；
- 全局目标；
- 必需产物；
- 未解决问题；
- 已产生 Artifact；
- 下一动作。

需要注意：

```text
当前 checkpoint 是阶段审计记录，还不是进程中断后的断点恢复快照。
```

文件中明确记录：

```json
{
  "resume_supported": false
}
```

## 6.7 节点执行器

统一接口位于：

```text
orchestration/execution/node_executor.py
```

三个核心模型：

- `ExecutorDescriptor`：执行器身份、能力、质量、成本、延迟；
- `NodeExecutionContext`：当前节点被允许看到的输入；
- `NodeExecutionResult`：统一执行结果。

主要执行器位于：

```text
orchestration/execution/default_executors.py
```

### PreparedInputExecutor

为输入准备类节点提供标准化输入边界。

### AgentAnalysisExecutor

是 AnalysisAgent、ResearchAgent、ReasoningAgent 的统一 Adapter。

它负责：

- 只提供直接依赖；
- 注入 Context Capsule；
- 限制输入预览；
- 约束 evidence refs；
- 解析结构化输出；
- 将自然语言 Agent 结果转换成 NodeExecutionResult。

### CodePipelineNodeExecutor

负责代码闭环：

```text
CodeModelingAgent 生成 JSON 代码结果
    ↓
Python AST 和安全路径检查
    ↓
保存 artifacts/code_pipeline.py
    ↓
真实执行
    ↓
记录 exit_code/stdout/stderr/文件变化
    ↓
失败时调用 ErrorAttributionAgent
    ↓
在 code_policy.max_retries 内修复
```

Agent 只生成源码，保存和执行由 Python 完成。

### ArtifactValidationExecutor

根据 TaskSpec 的通用产物合同检查：

- 文件是否存在；
- JSON 是否可解析；
- 必需字段是否存在；
- 代码执行失败时是否错误宣告成功。

## 6.8 外部工具型执行器

位于：

```text
orchestration/integrations/
```

### evidence_verifier.py

用 Python 读取真实证据目录，再调用 EvidenceVerifierAgent 对上游 claim 逐条核验。

Agent 只能引用框架已经授权并成功读取的 evidence ref。

### terminal_executor.py

使用 AutoGen 的本地命令行执行器，对已经落盘的脚本进行独立终端复核。

当前为本地执行，因此会出现安全警告；生产环境更适合替换为 Docker 沙箱。

### external_agent_executors.py

封装：

- MarkItDown 文档转换；
- FileSurfer 文件导航；
- WebSurfer 网页浏览。

这些能力只有 TaskSpec 要求、模型客户端可用且依赖已经安装时才注册。

---

# 七、失败恢复是怎么工作的

## 7.1 节点级恢复

位置：

```text
orchestration/graph/recovery_controller.py
```

失败后可能选择：

- retry：同一执行器重试；
- switch_executor：切换到另一个相同 capability 的执行器；
- replan：请求 PlanningAgent 生成替代图；
- block：无法继续时阻断。

恢复次数来自 TaskSpec：

- `max_node_retries`
- `max_executor_switches`
- `max_replans`

恢复控制器由 Scheduler 持有，因此跨 epoch 不会重置预算。

## 7.2 代码内部修复

代码节点另外有：

```text
code_policy.max_retries
```

这是 CodePipeline 内部的：

```text
生成 → 执行 → 错误归因 → 修复
```

不应与 Scheduler 的节点重试混为一谈。

## 7.3 业务结果修复

位置：

```text
orchestration/graph/repair_loop.py
```

它处理：

```text
节点执行成功，但独立业务验证发现结果错误。
```

流程为：

1. 将验证失败写入 Issue Ledger；
2. 根据失败 Artifact 找到生产节点；
3. 重开生产节点及其后继子图；
4. 把验证问题写入 `recovery_context`；
5. 复用 Scheduler 局部重放；
6. 重新进行独立验证；
7. 有新产物证据且原问题消失后才关闭 Issue。

业务修复次数：

```text
recovery_policy.max_business_repair_rounds
```

---

# 八、验证、审查、报告和最终成功

## 8.1 中间产物验证

位置：

```text
orchestration/task/artifact_validator.py
```

主要检查 TaskSpec 声明的中间产物。

## 8.2 通用业务验证

目录：

```text
orchestration/validation/
```

该模块使用 Validator Registry，而不是把某个具体题目的规则写进 Scheduler。

### protocol.py

定义 BusinessValidator 接口。

### models.py

定义：

- BusinessValidationContext；
- BusinessCheckResult；
- BusinessValidationResult。

### registry.py

注册并选择验证器。

### builtin_validators.py

当前通用验证器包括：

- InputCoverageValidator：输入是否真实存在、是否有来源；
- ResultIntegrityValidator：结果格式和基本完整性；
- CodeShortcutScanner：检测 placeholder、dummy、mock、fallback 和可疑估算捷径。

### runner.py

根据 TaskSpec 策略运行匹配的验证器，并生成：

```text
review/business_validation.json
review/business_validation.md
```

未找到适用验证器时应标记 `unverified`，不能自动当成业务成功。

## 8.3 pre_report_review

位置：

```text
utils/review_artifacts.py
```

这是 ReportAgent 之前的确定性技术闸门。

检查：

- 执行是否失败；
- TaskGraph 是否有 failed/blocked 节点；
- 中间产物是否通过；
- 业务验证是否通过；
- 是否存在未解决 blocking issue；
- 是否允许生成最终报告。

## 8.4 ReviewAgent

位置：

```text
agents/review_agent.py
```

只在技术闸门允许时进行语义审查。

它可以指出：

- 逻辑风险；
- 证据不足；
- 业务表达问题。

但它的结果是 advisory，不能覆盖 Python 技术闸门。

## 8.5 ReportAgent

位置：

```text
agents/report_agent.py
```

只在 `run_state.allow_report()` 返回 true 后调用一次。

它只能输出最终业务 Markdown 正文，不能生成：

- TaskSpec；
- RunState；
- trace；
- validation report；
- 其他系统文件。

最终保存：

```text
final_report.md
```

## 8.6 final_validation

位置：

```text
utils/review_artifacts.py
```

最终验收检查：

- 最终报告是否存在；
- 必需章节是否存在；
- 报告是否混入 Agent 名称和执行轨迹；
- JSON 是否可以解析；
- 报告引用的文件是否真实存在；
- 执行失败是否被错误写成通过；
- 业务验证是否通过；
- 是否仍有未解决 blocking issue。

最终状态映射：

```text
passed  → success
partial → partial
failed  → failed
```

---

# 九、RunState 保存了什么

位置：

```text
orchestration/core/run_state.py
```

输出：

```text
run_state.json
```

RunState 是框架唯一的实时运行台账。

主要字段：

## 9.1 stage

当前阶段：

```text
prepare / classify / plan / execute / review /
report / final_validate / finish
```

## 9.2 execution

代码执行历史：

- 尝试次数；
- 命令；
- exit code；
- stdout；
- stderr；
- 生成文件。

## 9.3 nodes

每个 TaskNode 的：

- 状态；
- executor；
- attempts；
- result；
- error；
- artifact manifest。

## 9.4 routing

每次动态选人的：

- capability；
- 候选执行者；
- 分数；
- 历史统计；
- 最终选择；
- 排除的执行者。

## 9.5 communication

低熵通信指标：

- 真实依赖边；
- 原始字节；
- 投影后字节；
- 传输字节；
- 估算 Token；
- 压缩率；
- 去重率；
- LLMLingua 调用次数；
- 引用化次数；
- 预算违规。

## 9.6 model_calls

每次模型调用的：

- Agent；
- 阶段；
- 节点；
- Prompt Token；
- Completion Token；
- 调用时间；
- 预算；
- 是否发生 soft budget warning。

## 9.7 runtime_memory

运行期记忆胶囊统计。

## 9.8 horizon

长程 epoch 状态：

- 当前 epoch；
- 活动节点；
- 完成节点；
- 总节点数；
- 逻辑步骤；
- checkpoint；
- 下一动作。

## 9.9 blocking_issues

持久化阻断问题。

后一次 Agent 声称 passed 不会自动删除旧问题，必须有新的 Artifact 或 evidence refs 证明
问题已被解决。

## 9.10 success_dimensions

分别记录：

- execution success；
- artifact success；
- business success；
- delivery success。

最终 success 不能只依赖 `exit_code=0`。

---

# 十、每个 Agent 的职责和真实调用位置

| Agent | 主要职责 | 调用位置 | 是否必调 |
|---|---|---|---|
| TaskUnderstandingAgent | 提炼目标、约束、需求、歧义 | workflow 规划前端 | 默认开启 |
| TaskPlanningAgent | 生成 PlanIR / PlanPatch / SemanticGraph | hierarchical planner / workflow | 规划需要 |
| AnalysisAgent | 提取事实、统计和证据绑定 | AgentAnalysisExecutor | DAG 选择 analysis 时 |
| ReasoningAgent | 建模、推理、策略和结果解释 | AgentAnalysisExecutor | DAG 选择 reasoning/math 时 |
| ResearchAgent | 背景和方法调研 | AgentAnalysisExecutor | DAG 选择 research 时 |
| CodeModelingAgent | 只生成 Python 源码 | CodePipelineNodeExecutor | code 节点 |
| ErrorAttributionAgent | 对真实执行错误生成修复建议 | CodePipelineNodeExecutor | 代码失败且有重试预算 |
| EvidenceVerifierAgent | 逐条核验上游事实 | EvidenceVerificationExecutor | TaskSpec 要求证据核验 |
| ReviewAgent | 语义审查建议 | workflow review | 技术预审允许时 |
| ReportAgent | 生成一次最终 Markdown | workflow report | 审查允许时 |
| WorkflowMemoryAgent | 选择和提炼流程经验 | workflow memory | memory policy 开启时 |

Agent 不等于 TaskGraph 节点。

一个 Agent 可以执行多个节点；一个 capability 也可以由多个 Agent/Executor 提供。节点和
执行者在运行时由 Capability Registry 绑定。

---

# 十一、orchestration 各文件的作用

## 11.1 core/

### core/workflow.py

整条显式工作流总入口。

### core/run_state.py

实时状态机、事件日志、成功维度、通信和恢复指标。

### core/schemas.py

Agent 与框架之间的 Pydantic 数据合同。

### core/model_calls.py

统一调用 Agent，记录 Token 和耗时，处理 Prompt 预算与模型上下文溢出。

Prompt 超过框架预算只警告；超过模型真实输入上限时先精简，仍超限才返回 recoverable
blocked 语义。

### core/prompt_builder.py

集中生成任务理解、规划、重规划、代码、错误、审查和报告 Prompt。

### core/workflow_policy.py

保存通信、路由、组队、恢复、记忆、终端、规划和 horizon 默认策略。

AFlow 当前只有离线策略快照 Adapter，没有在线接入或图搜索。

### core/run_manager.py

创建 run 目录、复制输入和保存 TaskSpec。

## 11.2 task/

### task/input_loader.py

读取用户指定的输入路径。

### task/input_preparation.py

生成领域无关的 normalized input。

### task/task_normalizer.py

分类任务并冻结 TaskSpec。

### task/artifact_validator.py

确定性检查中间产物。

## 11.3 planning/

### planning/plan_ir.py

层级计划数据结构。

### planning/hierarchical_planner.py

层级计划生成、验证、局部 refine 和最终展平入口。

### planning/plan_validator.py

确定性检查任务粒度、依赖、输出、验收和需求覆盖。

### planning/plan_format_adapter.py

修复 LLM 返回的轻微结构差异。

### planning/plan_flattener.py

将 compound/primitive 层级计划转换为纯 primitive 业务图。

### planning/requirement_compiler.py

构建和验证 RequirementContract。

## 11.4 graph/

### graph/semantic_graph.py

模型面对的轻量业务图。

### graph/graph_compiler.py

把业务图编译为严格执行图并补治理节点。

### graph/task_graph.py

TaskNode、TaskGraph、DAG 校验和节点状态转换。

### graph/graph_planner.py

TaskGraph 与 TaskSpec 的联合校验，以及规划失败时的确定性 fallback graph。

### graph/graph_scheduler.py

按依赖执行节点，动态路由、构造上下文、写入记忆并处理失败。

### graph/recovery_controller.py

决定 retry、switch executor、replan 或 block。

### graph/repair_loop.py

业务验证失败后的责任节点定位和局部子图重放。

## 11.5 execution/

### execution/node_executor.py

统一 Executor 接口和输入输出模型。

### execution/capability_registry.py

注册异构执行者并按 capability 选择。

### execution/executor_scoring.py

静态评分和 DyLAN-lite 历史评分。

### execution/default_executors.py

基础 Agent Adapter、代码闭环和产物验证执行器。

## 11.6 communication/

### communication/communication_models.py

消息信封、预算、压缩结果和通信指标模型。

### communication/context_builder.py

实现直接依赖通信、字段投影、内容寻址、引用化和去重。

### communication/context_compressors.py

确定性压缩、LLMLingua 和自动回退。

## 11.7 memory/

### memory/memory_models.py

MemoryRecord、RetrievedMemory 和 ContextCapsule。

### memory/runtime_memory.py

当前 run 的 SQLite 分布式记忆和检索。

### memory/workflow_memory.py

跨 run 的工作流技能检索、过滤和提炼。

## 11.8 integrations/

### integrations/evidence_verifier.py

真实证据读取与 EvidenceVerifierAgent Adapter。

### integrations/terminal_executor.py

独立终端复核。

### integrations/external_agent_executors.py

MarkItDown、FileSurfer 和 WebSurfer Adapter。

## 11.9 validation/

通用业务验证协议、注册表、内置验证器和执行入口。

---

# 十二、一次运行会生成哪些文件

典型目录：

```text
outputs/runs/<run_id>/
├── inputs/
│   └── 原始输入副本
├── task_spec.json
├── file_previews.json
├── normalized_input.json
├── task_graph.json
├── run_state.json
├── agent_trace.md
├── final_report.md                 # 只有审查允许才存在
├── planning/
│   ├── task_understanding.json
│   ├── requirement_contract.json
│   ├── requirement_validation.json
│   ├── plan_ir_round_0.json
│   ├── plan_validation_round_0.json
│   ├── plan_ir_final.json
│   ├── semantic_graph.json
│   └── graph_compile_report.json
├── artifacts/
│   ├── code_pipeline.py
│   ├── result.json
│   └── tool_results/
├── communication/
│   ├── payloads/
│   └── structured/
├── memory/
│   ├── runtime_memory.sqlite3
│   ├── metrics.json
│   └── capsules/
├── checkpoints/
│   └── epoch-XXXX.json
└── review/
    ├── business_validation.json
    ├── business_validation.md
    └── validation_report.md
```

想快速判断一次运行，应优先按这个顺序看：

1. `run_state.json`：最终状态和根失败；
2. `review/validation_report.md`：为什么通过或失败；
3. `task_graph.json`：哪个节点失败或阻断；
4. `artifacts/result.json`：真实业务结果；
5. `planning/plan_ir_final.json`：规划是否过粗或漏任务；
6. `agent_trace.md`：完整阶段和 Agent 调用；
7. `communication/`、`memory/`：需要研究通信和记忆时再看。

---

# 十三、现有研究机制在系统中的对应关系

## LLMLingua

作用：压缩节点之间过长的自然语言上下文。

状态：真实库可选接入，未安装或未加载时自动使用确定性压缩。

## AgentPrune-lite

作用：只传直接依赖和必要字段，抑制全连接通信与噪声级联。

状态：框架原生实现的轻量适配，不是完整复现 AgentPrune 训练算法。

## DyLAN-lite

作用：根据 capability、静态质量和历史表现动态选择执行者。

状态：在线统计打分，无需训练。

## CoALA / MemGPT / Generative Agents

作用：指导 Runtime Memory 的分层、分页和多信号检索设计。

状态：研究思想适配，不是整体引入这些外部框架。

## SAFE / CRITIC

作用：指导 EvidenceVerifier 把结论拆为 claim 并绑定真实证据。

状态：使用当前 Agent 和 Python 证据目录实现的 Adapter。

## AFlow

作用：预留离线工作流策略优化接口。

状态：尚未在线接入。当前只有 `AFlowPolicyAdapter` 可以读取外部预计算的只读策略快照。

---

# 十四、测试目录的作用

`tests/` 不是生产系统的一部分，但用于防止修改框架时破坏已有能力。

主要测试：

- `test_task_graph.py`：DAG 和状态转换；
- `test_graph_planning.py`：规划图校验；
- `test_hierarchical_planning.py`：PlanIR、refine 和粒度验证；
- `test_requirement_compilation.py`：需求合同；
- `test_graph_compiler.py`：治理节点编译；
- `test_graph_scheduler.py`：调度、路由、通信和失败；
- `test_dynamic_routing.py`：DyLAN-lite；
- `test_communication.py`：AgentPrune-lite 和压缩；
- `test_runtime_memory.py`：运行期记忆；
- `test_horizon_execution.py`：渐进式 epoch；
- `test_recovery_controller.py`：节点恢复；
- `test_business_repair_loop.py`：业务失败局部重放；
- `test_business_validation.py`：通用业务验证；
- `test_blocking_gate.py`：阻断问题不能被后续 passed 覆盖；
- `test_prompt_budget_semantics.py`：Prompt 预算软警告和上下文阻断；
- `test_external_agents.py`：FileSurfer、WebSurfer 等真实 Adapter；
- `test_framework.py`：完整 workflow 集成。

运行所有测试：

```bash
conda activate autogen
python -m unittest discover -s tests
```

---

# 十五、当前架构已经做到什么，尚未做到什么

## 已经做到

- Python 显式阶段控制；
- TaskSpec 单一事实来源；
- 需求合同和需求覆盖检查；
- 层级 PlanIR；
- primitive 粒度验证和局部 refine；
- 动态 DAG；
- 异构 capability 注册和动态路由；
- 稀疏直接依赖通信；
- Token/字节预算、去重和压缩指标；
- LLMLingua 可插拔压缩；
- 当前 run 的分层记忆和 Context Capsule；
- 代码真实保存、执行、重试和错误归因；
- 独立证据核验；
- 通用业务验证；
- 阻断问题台账；
- 业务失败后的局部子图重放；
- 多 epoch 渐进式执行；
- Review → Report → Final Validation 显式闸门。

## 尚未完全做到

### 1. 运行期动态扩展新子图

当前 Horizon 是：

```text
对已经生成的完整 TaskGraph 分 epoch 执行。
```

还不是：

```text
只规划当前阶段 → 执行 → 根据结果生成下一阶段新子图。
```

因此当前能够证明阶段化长程执行，但还不能证明真正数百至数千步的持续图扩展。

### 2. 进程级断点续跑

虽然有 epoch checkpoint 和 SQLite memory，但尚未提供：

- `--resume` 入口；
- 幂等节点协议；
- Scheduler 快照恢复；
- 中断后自动恢复。

### 3. 全领域业务正确性

当前业务验证是通用基础层，不能独立复算所有领域的专业约束。

框架未来可以通过 Validator Registry 增加可选领域验证器，但不能把领域规则写进 Scheduler。

### 4. 大规模长程 Benchmark

现有测试证明功能正确，但尚未系统测量：

- 100/500/1000 步成功率；
- 目标保持率；
- 记忆召回率；
- 局部恢复率；
- 通信压缩率；
- 路由成本；
- 多场景泛化。

---

# 十六、推荐的代码阅读路线

## 第一遍：只看整体流程

```text
main.py
→ orchestration/core/workflow.py
→ orchestration/core/run_state.py
```

目标：理解谁控制阶段、谁写状态。

## 第二遍：看任务如何变成 DAG

```text
task/task_normalizer.py
→ planning/requirement_compiler.py
→ planning/plan_ir.py
→ planning/hierarchical_planner.py
→ planning/plan_validator.py
→ planning/plan_flattener.py
→ graph/graph_compiler.py
→ graph/task_graph.py
```

目标：理解 TaskSpec、RequirementContract、PlanIR、SemanticGraph、TaskGraph 的关系。

## 第三遍：看一个节点怎么执行

```text
graph/graph_scheduler.py
→ execution/capability_registry.py
→ execution/default_executors.py
→ execution/node_executor.py
```

目标：理解节点如何选择 Agent、如何执行、如何记录结果。

## 第四遍：看任务书相关创新模块

```text
communication/context_builder.py
→ communication/context_compressors.py
→ execution/executor_scoring.py
→ memory/runtime_memory.py
→ core/workflow.py 中 horizon 逻辑
```

目标：理解 AgentPrune-lite、LLMLingua、DyLAN-lite、长程记忆和 epoch。

## 第五遍：看成功与失败是怎么判定的

```text
task/artifact_validator.py
→ integrations/evidence_verifier.py
→ validation/
→ graph/repair_loop.py
→ utils/review_artifacts.py
```

目标：理解为什么 exit code 为 0 也不一定代表任务成功。

---

# 十七、一句话总结

当前项目的本质是：

```text
一个由 Python 掌握控制权，以 TaskSpec 和 RequirementContract 为任务合同，
以 PlanIR 和动态 TaskGraph 为执行结构，以 Capability Registry 选择异构 Agent，
以稀疏通信和分层记忆维持长程上下文，以独立验证和状态机决定最终成功的
通用多智能体框架。
```

阅读代码时始终抓住三条主线：

1. 任务要求如何从输入变成可追踪需求；
2. 需求如何变成可执行 DAG 并被动态分配给 Agent；
3. 执行结果如何经过真实证据和产物验收后才能成为最终成功。
