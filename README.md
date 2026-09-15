# 面向复杂任务求解的多智能体动态协作与自治交付系统

以数据建模与城市多模态数据分析为验证场景。西安交通大学，XH-202631。

指导老师：刘恒嘉、曾菊香；申报人：陈若凡。团队成员：高卓辉、郭威、黄潇、黄宇哲、李宛桐、李智骋、谢东东、张煜皓、陈必纯。

系统采用模型驱动的分析与规划、框架负责的确定性控制：将高层任务转化为版本化任务图，组织按需协作，处理运行期扰动，并通过独立验证决定是否允许最终交付。

## 系统组成

- 动态协作：需求前沿、阶段规划、任务图编译、能力匹配与执行器路由。
- 长程状态：直接依赖消息、ContextCapsule、运行期记忆与窗口级 checkpoint。
- 自主恢复：节点失效、需求变更、数据异常注入，以及验证驱动的责任定位和局部重放。
- 可验证交付：领域验证、证据核验、需求验收与最终成功门禁。
- 演示台：历史事件回放、真实执行入口、注入操作与恢复证据查看。

```text
main.py / config.py       主工作流与模型配置
agents/                  专业角色
orchestration/           规划、任务图、调度、通信、记忆、执行与验收
task_plugins/            装箱优化、差旅报销、城市多模态业务插件
dashboard/               演示台与专家看板
tests/                   回归测试
deployment/              交付包部署模板及辅助工具
docs/                    技术报告源稿、算法附录与架构图
evidence/                历史运行指标摘要（不是完整原始运行档案）
```

## 安装与真实执行

本地验证使用 Python 3.10；建议使用独立虚拟环境。真实执行需要可用的模型服务，浏览器工具另需 Chromium。首次安装依赖需要联网。

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-runtime.txt
python -m playwright install --with-deps chromium
```

根据 [环境变量模板](deployment/.env.example) 配置实际服务。主程序不自动读取 `.env`，请在本地终端设置变量；不要将真实密钥提交到 Git。

```bash
export MODEL_API_KEY="<your-api-key>"
export MODEL_BASE_URL="<your-provider-base-url>"
export MODEL_NAME="<your-model-name>"
export FILE_SURFER_MODEL="<tool-calling-model-name>"
export WEB_SURFER_MODEL="<tool-calling-model-name>"

python utils/preflight.py --input examples/expense_reimbursement
python main.py --input examples/expense_reimbursement --injection-profile recovery-demo
```

文件与浏览器工具模型必须支持工具调用；默认可复用主模型服务的密钥和地址，也可配置独立的 `FILE_SURFER_*`、`WEB_SURFER_*`。真实运行会产生模型接口用量。装箱场景输入为 `examples/mathorcup_d`。

## 演示台

```bash
# 首次克隆后创建本地运行目录；Git 不保存空目录
mkdir -p outputs/runs memory

# 只读回放，不调用模型
bash scripts/start_demo.sh --replay

# 真实执行与运行期注入，需要先配置模型服务
bash scripts/start_demo.sh --live
```

访问 `http://127.0.0.1:8765/demo`；专家看板位于 `/`。两个模式不要同时占用同一端口。真实执行使用启动终端显示的临时执行口令；远程服务器建议通过 SSH 端口转发访问，不直接暴露执行接口。

Git 仓库不包含 `outputs/runs/` 下的完整历史运行。首次克隆后，回放页面可能没有可回放记录；先生成本地运行，或将经授权的比赛运行档案放入该目录。`evidence/` 中的归档指标不代表本机刚刚完成了运行。

## 数据与测试

装箱和报销示例随源码提供。城市多模态原始数据、完整运行日志、私有记忆和比赛交付大压缩包不在本仓库中；城市任务需要将经授权的数据另行放入 `城市多模态数据集/`。

以下检查不需要真实模型或城市原始数据：

```bash
python -m unittest discover -s tests -p 'test_submission_bundle.py' -v
python -m unittest discover -s tests -p 'test_package_submission.py' -v
python -m unittest discover -s tests -p 'test_task_graph.py' -v
python -m unittest discover -s tests -p 'test_runtime_memory.py' -v
```

JavaScript 投影测试 `node tests/test_demo_projection.js` 需要 Node.js 和归档运行 `outputs/runs/20260910_163108/`；请补齐该归档后再运行，正常前端运行不需要服务端 Node.js。完整测试入口为 `python -m unittest discover -s tests -v`；部分测试依赖城市数据与指定历史运行，未补齐这些本地资料时不能把完整测试结果当作系统回归结论。

对生成的运行可独立审计：

```bash
python utils/run_audit.py outputs/runs/<run_id> --json
python utils/injection_audit.py outputs/runs/<run_id> --json
```

注入审计会写入汇总文件；请在工作副本上运行，不要改写已经封存的证据。

## 技术材料与实现边界

- [技术报告 LaTeX 源稿](docs/competition_report.tex)
- [核心算法与复杂度附录](docs/core_algorithms_appendix.pdf)及其[源稿](docs/core_algorithms_appendix.tex)
- [详细运行说明](run.md)
- [代码结构说明](CODE_STRUCTURE_GUIDE.md)

安装 TeX Live/XeLaTeX 后，可在 `docs/` 下运行 `latexmk -xelatex competition_report.tex` 生成报告。算法附录按当前代码给出复杂度边界；主报告部分概念性复杂度表述仍需与附录统一，不将理想算法上界视为现有实现的性能保证。

`deployment/docker-compose.yml` 与 `bootstrap.py` 面向比赛交付包目录，不是直接从 Git 克隆目录启动的 Compose 配置。容器配置已提供，但镜像构建与启动尚未实测；本仓库优先使用上述原生启动方式。

端边云目前是逻辑资源后端，不代表物理多机集群或模型参数切分；千步实验区分工作单元与确定性候选评估，不等同于千次大模型调用。`bounded_local` 执行器也不等同于虚拟机级沙箱。可选 LLMLingua 依赖见 `requirements-optional.txt`，未启用时使用确定性压缩路径。
