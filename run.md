# 运行说明

评审与录屏入口：`bash scripts/start_demo.sh --replay`，浏览器访问
`http://127.0.0.1:8765/demo`。此模式使用历史真实运行，不需要模型密钥。
安装下述模型环境后可运行 `bash scripts/start_demo.sh --live`，在页面切换“真实执行”，
输入服务启动口令，启动任务后提交节点失效、需求变更或数据异常。
请求在下一业务节点边界应用，恢复由主调度器自主完成。
完整要求核验、录屏步骤及边界见 [演示改造与提交要求核验](docs/演示改造与提交要求核验.md)。

不要把真实密钥写入仓库文件。请在当前终端设置环境变量，或使用已被
`.gitignore` 排除的本地 `.env`/`key.md` 文件自行加载。

```bash
export MODEL_API_KEY="<your-api-key>"
export MODEL_BASE_URL="<openai-compatible-base-url>"
export MODEL_NAME="<model-name>"

# 只有 TaskSpec 要求文档恢复或公开网页调研时，才会创建对应工具模型客户端。
export FILE_SURFER_API_KEY="<your-file-model-api-key>"
export FILE_SURFER_BASE_URL="<openai-compatible-base-url>"
export FILE_SURFER_MODEL="<tool-calling-model-name>"

export WEB_SURFER_API_KEY="<your-web-model-api-key>"
export WEB_SURFER_BASE_URL="<openai-compatible-base-url>"
export WEB_SURFER_MODEL="<tool-calling-model-name>"
```

`FILE_SURFER_MODEL` 和 `WEB_SURFER_MODEL` 必须真实支持 function/tool calling。若两者
使用同一服务，可以只设置 `FILE_SURFER_*`，浏览器客户端会按“WEB_SURFER →
FILE_SURFER → MODEL”顺序读取配置。不要在仓库中保存真实密钥。

首次安装或更新第一阶段功能组件时执行：

```bash
conda activate agent
python -m pip install -r requirements.txt
python -m playwright install chromium
```

框架当前接入的现成功能组件不是固定流水线步骤：

- MarkItDown：无模型的多格式文档转换器，独占执行 `document_conversion` 节点；
- AutoGen FileSurfer：通过真实 `open_path` 工具浏览 `run_dir/inputs/`，独占执行
  `file_navigation` 节点；长文档、逐页读取、定位原文等任务会强制需要该能力；
- AutoGen MultimodalWebSurfer：通过 Playwright 获取公开网页证据，仅在任务明确要求 URL、
  联网、官网或最新外部信息时提供 `web_research` 能力。
- EvidenceVerifierAgent：以 SAFE/CRITIC 风格逐条核验上游原子事实；Python 先真实读取授权
  文件、JSON Pointer 和来源引用，再校验 Agent 是否只引用了目录中的证据；
- WorkflowMemoryAgent：规划前从框架提供的候选技能中选择，最终验收后提炼可复用流程；
  技能库由 Python 过滤和写入，默认位于 `memory/workflow_skills.json`；
- AutoGen CodeExecutorAgent：代码任务中执行独立 `terminal_execution` 节点，对已经落盘的
  Python 脚本运行固定编译复核并记录退出码。

是否需要这些能力只在 TaskSpec 的 `capability_contract` 中确定。PlanningAgent 必须为
`required` 能力生成真实节点，Graph Scheduler 再从 Capability Registry 中选择执行者；
普通本地分析任务不会为了增加 Agent 数量调用它们。网页组件默认移除表单输入工具、
禁止下载，并限制步骤数和运行时间。

TaskGraph 校验还要求外部能力节点必须通过依赖边进入后续分析或执行节点。仅仅把一个
Agent 节点挂到最终校验节点、但下游完全不使用其结果，会以“疑似冗余凑数”拒绝该图。

通用任务默认采用以下动态收口契约：

```text
业务节点 ───────────────┐
代码节点 → 受控终端复核 ├→ EvidenceVerifierAgent → 产物校验
其他必要业务节点 ───────┘
```

EvidenceVerifierAgent 必须直接接收每个业务节点的结构化结果，失败时不能进入审查和报告。
WorkflowMemoryAgent 不在 TaskGraph 中伪装业务步骤：规划前只有检索到候选技能时才调用它
完成选择，最终验收后再调用它提炼候选；记忆模块异常时记录事件并降级为空记忆，不阻断当前任务。

当前终端后端名为 `bounded_local`：只允许框架生成的
`python -m py_compile artifacts/code_pipeline.py`，并通过 AutoGen 审批函数逐次核对完整代码块。
它不是 Docker 或虚拟机级隔离；在接入任意 Shell、测试套件或模型生成命令之前，应先配置
容器执行后端。

运行示例：

```bash
python utils/preflight.py --input examples/mathorcup_d
python main.py --input examples/mathorcup_d
python utils/run_audit.py

python utils/preflight.py --input examples/expense_reimbursement
python main.py --input examples/expense_reimbursement
python utils/run_audit.py

python utils/preflight.py --input 城市多模态数据集
python main.py --input 城市多模态数据集
python utils/run_audit.py
```

三类运行期动态注入恢复演示（建议使用文件较小、运行较快的差旅报销场景）：

```bash
python main.py --input examples/expense_reimbursement \
  --injection-profile recovery-demo

# 主流程结束后会自动生成报告，也可独立复核并以退出码表示结果
python utils/injection_audit.py outputs/runs/<run_id>
python utils/run_audit.py outputs/runs/<run_id>
```

`recovery-demo` 在主 `GraphScheduler` 中依次执行三个一次性注入：

- 节点失效：已选执行器第一次调用前发生受控崩溃，由恢复控制器重试、切换或重规划；
- 需求变更：至少一个节点完成后，修改尚未执行节点的契约并递增 TaskGraph 版本，已完成节点保持不变；
- 数据异常：仅修改 `run_dir/inputs/` 中的小型输入副本，以 SHA256 检出变化，保存异常隔离副本并恢复原件，随后重新执行目标节点。

完整轨迹同时保存在 `run_state.json.runtime_injections`、标准事件流以及
`injections/<injection_id>/evidence.json`。汇总证据位于
`injections/recovery_trace.json` 和 `injections/recovery_trace.md`。注入功能默认关闭，绝不修改
用户的源输入目录。也可用多个 `--inject node-failure`、`--inject requirement-change`、
`--inject data-anomaly` 只启用指定类型。

运行状态和产物位于 `outputs/runs/<run_id>/`。

启动只读运行看板（另开一个终端）：

```bash
python utils/dashboard_server.py --host 127.0.0.1 --port 8765
```

浏览器打开 `http://127.0.0.1:8765/`。看板默认展示三个正式场景、最终验收闸门、动态
任务图、产物、Token、记忆、通信、城市修复时间线、1000 步证据和端边云路由，默认
不提供任务执行或文件写入入口。

需要从演示台启动真实预设任务时，显式开启受控执行模式：

```bash
python utils/dashboard_server.py --host 127.0.0.1 --port 8765 --enable-execution
```

该模式使用 MathorCup、差旅报销和城市多模态三个固定输入目录，提供常规、千步、
预设恢复和交互式注入场景，不接受任意命令或任意文件路径。
服务会生成一次性执行口令并输出到启动终端；前端启动任务时必须输入该
口令。工作台会展示非敏感预检、当前阶段、运行节点、实时日志、运行目录和最终审计状态。
同一时间最多运行一个前端任务。正式展示稳定性优先时可保持默认只读模式。

运行结束后可用统一审计命令检查最新一轮是否真正通过全部交付闸门：

```bash
python utils/run_audit.py
```

也可以检查指定目录或输出机器可读 JSON：

```bash
python utils/run_audit.py outputs/runs/<run_id>
python utils/run_audit.py outputs/runs/<run_id> --json
```

只有 outcome、业务验证、需求验收、证据核验、报告、节点状态和阻断问题全部通过时，
该命令才返回退出码 0；任一项未闭合都会返回退出码 1。

保留三个审计 PASS 的运行目录后，可导出比赛提交用的跨场景指标：

```bash
python utils/competition_metrics.py \
  outputs/runs/<math_run_id> outputs/runs/<expense_run_id> \
  outputs/runs/<urban_run_id> \
  --output-dir evidence/metrics
```

导出器默认要求至少两个运行且每个运行都通过完整审计，随后生成
`cross_scenario_metrics.json` 和 `cross_scenario_metrics.md`。

默认核心依赖不包含本地压缩模型。系统始终提供确定性通信压缩；如需启用
LLMLingua-2，可单独安装可选依赖：

```bash
conda activate agent
python -m pip install -r requirements-optional.txt
```

首次启用可能需要下载模型并占用较多内存。框架默认禁止在任务运行时隐式下载，
只读取本地缓存；需要首次下载时显式加入 `--llmlingua-allow-download`。在 `salloc`
获得 GPU 且 Conda 环境中的 PyTorch 为 CUDA 版本后，可加入
`--llmlingua-device-map cuda`。这两个选项会在 prepare 阶段写入并冻结到本轮
`task_spec.json`，后续组件不再自行猜测设备或下载策略。

```bash
python main.py --input examples/mathorcup_d \
  --llmlingua-device-map cuda \
  --llmlingua-allow-download
```

未安装、模型加载失败或压缩结果仍超出预算时，框架会自动退回确定性压缩，
不影响基础工作流。`requirements-optional.txt` 不强制 CPU 或 CUDA 版本，避免覆盖
当前环境中已经安装好的硬件适配版 PyTorch。

每轮运行的 `run_state.json` 会记录：

- 动态路由候选及 DyLAN-lite 评分；
- 实际使用的稀疏依赖边和拓扑密度；
- 消息压缩前后 Token/字节、压缩比、重复率及引用情况；
- 完整任务 Prompt 的压缩前后估算与模型提供方实际 Token；
- 普通节点 Prompt 默认上限 5000 tokens，规划、重规划、审查和报告默认上限 8000 tokens；
- 模型调用耗时、执行者切换与重规划记录。
- 外部功能组件的真实调用次数、成功/失败次数、提供方和对应证据文件。
- 工作流记忆的检索/提炼调用次数、选中技能 ID 和新增技能 ID。
- EvidenceVerifier 的逐条 claim、证据目录、框架拒绝原因和核验产物路径。

其中 `communication.compression_ratio` 只表示 TaskGraph 依赖消息平面；完整任务 Prompt
还包含节点契约、指令和必要输入，其估算口径位于
`model_calls.complete_prompt_compression_ratio`，提供方实际计费口径位于
`model_calls.prompt_tokens`。

跨运行执行者统计默认保存在 `memory/executor_stats.json`。它只影响候选排序，
统计文件不可用时框架会降级到本轮运行目录，不影响节点真实结果和最终验收。

每次外部组件调用都会在
`artifacts/tool_results/<node_id>/` 保存结构化调用证据，同时写入
`run_state.json.external_tools`。因此可以区分“能力已注册”“规划选中了节点”和
“工具确实执行成功”这三个状态。

提交前打包（以下三个运行必须均为完整审计 PASS）：

```bash
python utils/package_submission.py \
  --run-dir outputs/runs/<math_run_id> \
  --run-dir outputs/runs/<expense_run_id> \
  --run-dir outputs/runs/<urban_run_id> \
  --output submission/competition_submission.zip
```

轻量提交包不重复嵌入各运行目录的 `inputs/`，而是在
`evidence/input_manifests/` 保存逐文件大小和 SHA256；原始城市数据按平台容量另行提交。

也可以在确认三个模型环境变量已导出后执行一键收尾：

```bash
bash scripts/finish_competition.sh
```

脚本会在任一 preflight、真实运行或完整审计失败时立即退出，不会生成伪造的指标表或提交包。

1000 步长程 checkpoint/恢复压力测试：

```bash
conda activate agent
python utils/long_horizon_stress.py \
  --steps 1000 --resume-after 500 --window-size 25 \
  --output-dir evidence/metrics
```

结果写入 `evidence/metrics/long_horizon_1000.json` 和 `.md`，完整 checkpoint
保存在命令输出的 `outputs/stress/<run_id>/`。这是确定性调度与恢复压力测试，
不等同于 1000 次真实模型调用。

真实城市数据 1000 WorkUnit 长程运行：

```bash
conda activate agent
python utils/urban_long_horizon.py --live-reasoning
python utils/run_audit.py outputs/runs/<输出的运行目录>
```

该流程处理四类真实数据，执行 1000 个可审计业务步骤和 100 个跨模态综合步骤；每 10 个
综合步骤调用一次真实模型，共 10 次。它在第 500 步从签名 checkpoint 恢复，任何模型调用、
摘要链、来源覆盖或完整审计失败都会使进程非零退出，不会生成假成功证据。
