"""Single-source reviewer text and defense-slide content for the local bundle."""
TITLE = "面向复杂任务求解的多智能体动态协作与自治交付系统"
SUBTITLE = "以数据建模与城市多模态数据分析为验证场景"
TEAM = "西安交通大学｜指导老师：刘恒嘉、曾菊香｜申报人：陈若凡"
MEMBERS = "高卓辉、郭威、黄潇、黄宇哲、李宛桐、李智骋、谢东东、张煜皓、陈必纯"
RUNS = {
    "三场景证据": ["20260908_124006", "20260908_125323", "20260908_173502"],
    "动态注入恢复证据": ["20260910_163108", "20260914_161813"],
    "千步长程证据": ["20260909_urban_business_1000", "20260911_mathorcup_business_1000"],
}
SLIDES = [
    ("从高层意图到可信交付", "XH-202631 / 系统概览", [
        TITLE, "——" + SUBTITLE, TEAM,
        "规划 → 执行 → 验证 → 局部修复 → 自治交付"], None),
    ("复杂任务需要的不只是一个答案", "01 / 问题与目标", [
        "长程任务：目标、硬约束和关键事实必须跨步骤保留。",
        "动态环境：需求变化、节点失效、数据异常不能导致假成功。",
        "可信交付：代码必须真实执行，结果必须满足确定性业务规则。",
        "系统目标：统一任务状态、协作依赖与证据来源。"], None),
    ("模型提议，控制面维护状态与验收", "02 / 整体架构", [], "系统整体流程框架图.png"),
    ("任务图、记忆与资源的统一协作", "03 / 核心机制", [
        "动态拓扑：契约化 TaskNode、按需增量扩展、版本化 DAG。",
        "低冗余通信：直接依赖通信、字段投影、artifact 引用。",
        "长程记忆：Protected State、ContextCapsule、语义 checkpoint。",
        "资源路由：安全与能力硬过滤，再按成本、时延与历史质量排序。",
        "当前端边云为逻辑资源与阶段级模型路由，不是物理多机或参数层切分。"], None),
    ("三类动态注入形成完整恢复轨迹", "04 / 自主恢复", [
        "节点失效：主调度器检测受控执行失败，恢复后实际重执行。",
        "需求变更：修改待执行契约、递增图版本、保留已完成节点。",
        "数据异常：修改运行输入副本，SHA256 检测、隔离、恢复并复验。",
        "最新 HTTP 交互运行：20260914_161813；三类请求均 accepted/recovered。",
        "运行审计 9/9；注入审计 10/10。证据：03 / 动态注入恢复证据。"], None),
    ("三个业务场景，使用同一验收框架", "05 / 跨场景证据", [
        "MathorCup：300 件货物完整覆盖；5 辆车；成本 3500。",
        "差旅报销：PDF、DOCX、XLSX 联合核验；核验总额 CNY 1,485。",
        "城市多模态：遥感元数据、法规、匿名通联、PCAP 包头统计。",
        "三个正式运行均通过独立于主执行链的运行审计 9/9。",
        "证据路径：03_运行证据 / 三场景证据 / 各 run_id。"], None),
    ("千步验证的是状态连续性与业务执行", "06 / 长程证据", [
        "城市业务：1,000 WorkUnit；100 个综合步骤；10 次阶段模型调用。",
        "装箱优化：1,000 个确定性候选；最终结果由业务验证器复算。",
        "两个业务长程运行：第 500 步恢复；40 个 checkpoint；重复执行 0。",
        "另外保留基础设施 1,000 步调度压力测试，三种口径分别标注。",
        "WorkUnit 不等于大模型调用次数；不宣称已经验证无限长任务。"], None),
    ("复杂度与已验证边界", "07 / 严谨性", [
        "DAG 校验 O(V+E)；候选资源排序 O(C log C)；哈希 O(B)。",
        "受影响子图遍历 O(V′+E′)；不包含模型与外部工具时间。",
        "当前扫描式调度器完整调度最坏 O(V²+E)，不宣称理论最优。",
        "缺少同条件 Static DAG / Full Broadcast / ReAct 系统性对照。",
        "Docker 配置已提供；此构建机无可用 Docker daemon，容器启动待复核。"], None),
    ("如何在五分钟内复核", "08 / 评审路径", [
        "先看 00_请先阅读，再打开 01 中最新技术报告。",
        "解压 02 的源码，启动 /demo；无需密钥可回放真实历史记录。",
        "查看三类恢复证据和最终产物，运行审计脚本复核。",
        "配置自己的模型服务后，可启动真实任务并在运行中注入。",
        "演示视频为历史真实运行回放，不冒充本轮实时模型执行。"], None),
    ("交付物可追溯，能力边界可复核", "09 / 总结", [
        "技术材料：完整报告、可编辑正文、算法附录与架构图。",
        "可运行系统：源码、示例输入、依赖、原生启动与容器部署配置。",
        "运行证据：三场景、两种动态注入、两项业务长程及压力测试。",
        "机器验证：自动化回归日志、原始审计 JSON、SHA256SUMS。",
        "团队成员：" + MEMBERS], None),
]


def documents(test_summary):
    return {
        "00_请先阅读/README_FIRST": f"""# 请先阅读

## 项目与团队

{TITLE}——{SUBTITLE}。项目编号 XH-202631。

{TEAM}。团队成员：{MEMBERS}。

## 这是什么

这是按评审使用顺序组织的本地提交包。技术报告是当前 97 页定稿版本；源码、示例输入、演示前端和精选真实运行可以脱离开发机目录使用。根目录 SHA256SUMS.txt 用于校验文件完整性。

## 推荐打开顺序

1. 先读本目录的《5分钟评审指南》和《交付物清单》。
2. 阅读 01_技术材料中的技术方案与验收报告.pdf。
3. 按 02_可运行系统中的部署运行手册启动演示台。
4. 对照 03_运行证据中的原始结果、事件和审计 JSON。
5. 观看 04_演示视频，再使用 05_答辩材料。

## 两种运行方式

证据回放：不需要模型密钥，展示已经保存的真实历史执行。真实执行：需要自备可用模型服务与 tool-calling 模型，产生正常接口用量。运行中注入由演示者发起，后续恢复由主工作流处理。

## 必须知道的边界

- 当前端边云是逻辑资源后端和阶段级模型路由，不是物理多机集群或 Transformer 参数切分。
- 千步是 WorkUnit / 确定性候选执行口径，不等于千次模型调用。
- 视频明确展示历史真实运行回放，并非本次打包期间重新调用模型。
- Docker 配置随包交付；构建机没有可用 Docker daemon，不能认定容器构建、启动已经实测。
- DOCX 的正文和表格可编辑；复杂公式、伪代码及图示使用原稿渲染图，完整 LaTeX 源稿随包保留。正式版式以 PDF 为准。
- 运行审计复核保存的交付门禁，不等价于第三方认证，也不重新调用模型。

## 文件校验

在提交包根目录执行 `sha256sum -c SHA256SUMS.txt`。也可使用 `python 校验提交包.py` 跨平台核对。请先验证原始交付文件，再在工作副本运行系统。
""",
        "00_请先阅读/5分钟评审指南": """# 5分钟评审指南

## 00:00—00:40：看清目标和边界

打开技术报告封面、摘要和系统架构图。重点看“模型驱动智能面 + 确定性控制面”，以及最终成功由业务验证、需求验收和证据闭合共同决定。

## 00:40—01:30：看任务如何变成协作图

启动 `bash scripts/start_demo.sh --replay`，访问 `http://127.0.0.1:8765/demo`。选择“交互式注入 · 20260914_161813”，查看任务契约、真实依赖图和节点路由。

## 01:30—03:10：看三类异常如何恢复

选择节点失效、需求变更、数据异常章节。沿真实事件进度查看触发、检测、恢复动作与重新验证；打开每项恢复证据。图是保存的任务拓扑，历史记录不等于逐时刻图快照。

## 03:10—04:10：看最终交付与原始证据

跳转可信交付，检查九项运行审计；打开 final_report.md、业务验证结果和注入审计。最新交互运行额外检查 interactive_requests_applied，共 10 项注入检查。

## 04:10—05:00：看跨场景和千步证据

切换装箱、报销、城市运行。千步证据在专家看板的“长程与恢复”查看，或直接打开归档的 long_horizon_validation.json / long_horizon_search_validation.json。城市千步包含 10 次阶段模型调用；装箱千步为确定性候选搜索。查看第 500 步恢复、40 个 checkpoint 和 0 重复执行。专用千步工具没有通用 run_finished 事件，其演示页“可信交付”章节可能不可跳转；以实际验证文件为依据。

## 需要复核真实执行时

按部署手册配置模型服务，以 --live 启动演示台，使用终端生成的口令发起任务。请在任务运行中发起注入；不要把口令或密钥录入演示视频。模型服务耗时不包含在上述五分钟导览中。
""",
        "02_可运行系统/部署运行手册": """# 部署运行手册

## 1. 运行边界与依赖

建议 Linux + Python 3.11；回放服务仅依赖 Python 标准库，真实执行还需安装 requirements-runtime.txt。本次实测解释器为 Python 3.10；3.11 容器配置待镜像实测。回放投影的独立 JavaScript 测试另需 Node.js，正常运行前端不需要服务端 Node.js。浏览器访问前端；真实模型必须通过环境变量配置，不内置 API Key。逻辑端边云与模型名称应配置为服务商实际可用值。

本包提供完整城市输入数据、装箱及报销输入。原始城市输入仅在源码中保留一份；城市历史运行中的重复 inputs 副本不再重复嵌入，其大小与 SHA256 在 input_manifest.json 中记录。历史证据原文件未修改，绝对路径可能指向原开发机；新的执行使用当前解压目录。

## 2. 解压源码并验证

在本目录执行：

```bash
python bootstrap.py
cd system_source
python --version
bash scripts/start_demo.sh --replay
```

浏览器访问 http://127.0.0.1:8765/demo。专家看板为根路径 /。bootstrap.py 不覆盖已有 system_source；重复部署请选择新目录，保护已有结果。

## 3. 原生真实执行

```bash
cd system_source
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-runtime.txt
python -m playwright install --with-deps chromium
```

根据 .env.example 设置 MODEL_API_KEY、MODEL_BASE_URL、MODEL_NAME、FILE_SURFER_MODEL 等。主进程不自动加载 .env；请在自己的安全终端 export 所需变量。不要使用不可信文件进行 shell source。FileSurfer / WebSurfer 必须使用支持工具调用的模型。

```bash
python utils/preflight.py --input examples/expense_reimbursement
bash scripts/start_demo.sh --live
```

在真实执行页输入服务终端生成的临时执行口令，启动差旅审查。任务运行期间可提交三类注入，请求在下一业务节点边界应用。无需人工批准恢复动作；任务结束后不再接收新注入。

也可直接运行主工作流：

```bash
python main.py --input examples/mathorcup_d
python main.py --input examples/expense_reimbursement --injection-profile recovery-demo
python main.py --input 城市多模态数据集
```

## 4. Docker Compose（配置已提供，容器启动待实测）

构建机没有可用 Docker daemon。本节为交付的可执行配置，不是已经完成的镜像验证记录。首次构建需要联网下载 Python 依赖及 Chromium。

回到包含 docker-compose.yml 的目录。先执行 bootstrap.py，复制 .env.example 为本地 .env。在 Linux 上将 LOCAL_UID、LOCAL_GID 改为 `id -u`、`id -g` 输出，避免持久化目录权限错误。

```bash
docker compose up --build demo
# 结束回放容器后，再启动真实执行容器：
docker compose stop demo
docker compose --profile live up --build live
```

服务仅映射到宿主机 127.0.0.1。outputs 和 memory 持久化到解压源码中。不要同时启动占用同一端口的回放和真实执行服务。Dockerfile 已在源码内外各保留一份，Compose 构建上下文为 system_source。

## 5. 独立审计与长程复核

```bash
python utils/run_audit.py outputs/runs/20260908_124006 --json
python utils/run_audit.py outputs/runs/20260914_161813 --json
python utils/injection_audit.py outputs/runs/20260914_161813 --json
python -m unittest discover -s tests -v
node tests/test_demo_projection.js
```

注入审计命令会在指定运行的 injections 目录重新写入汇总报告。请对工作副本操作，不要修改供校验的原始提交证据。

```bash
# 以下默认不调用真实模型；重新生成业务或压力测试运行
python utils/mathorcup_long_horizon.py
python utils/urban_long_horizon.py
python utils/long_horizon_stress.py
```

以上工具的 --live-reasoning 会调用配置的模型并产生正常接口用量。基础设施压力测试与业务千步的含义不同，不能混为一个指标。

## 6. 故障排查与安全

- 端口占用：`DEMO_PORT=8766 bash scripts/start_demo.sh --replay`。
- 远程服务器：通过 SSH 本地端口转发访问，不直接暴露执行接口。
- 401：检查临时执行口令；只读模式禁止启动与注入。
- 模型超时：检查服务可达性、配额、模型能力和 MODEL_CALL_TIMEOUT_SECONDS。
- 缺少 Chromium：执行 playwright 安装；回放本身无需 Chromium 服务端组件。
- 部分历史路径是原始运行记录，不要手工改写它们；新运行自动生成新路径。
- 当前 bounded_local 执行器不是虚拟机级沙箱；容器部署也不等同于端边云集群。
- 停止容器使用 docker compose stop；不要用 down -v 删除需要保留的运行数据。
""",
        "03_运行证据/自动化测试报告": f"""# 自动化测试与提交验证报告

## 本次实际执行

{test_summary}

原始终端输出随包保存在 测试原始日志/。回归测试使用本地 agent Python 环境，包含模拟执行器和隔离测试目录；不代表全部测试均访问了真实模型。

## 历史真实业务运行重新审计

七组精选运行均由当前 utils.run_audit.audit_run 重新检查，通过 9/9 交付门禁。预设注入运行 20260910_163108 通过 9/9 注入检查；HTTP 交互运行 20260914_161813 通过 10/10 注入检查。

审计结果位于各证据运行目录旁的 run_audit.json 和 injection_audit.json，完整 checks 字段可机器复核。该审计读取已有状态、证据文件和交付门禁，不重新执行历史模型，也不构成第三方机构认证。

## 文档、演示与包装校验

- PDF 来源为最新封面版报告；DOCX 重新从同一 LaTeX 源稿生成。
- 回放时间线测试检查事件顺序、注入生命周期以及不提前呈现最终 PASS。
- 包装过程使用允许清单，排除 key.md、真实 .env、Git、缓存和跨运行私有记忆。
- 精选证据复制前后按 SHA256 比较；整包附 SHA256SUMS.txt。
- 容器配置通过可用的静态检查；没有可用 daemon，未执行 docker build / up。
- 真实 HTTP 注入联调为历史记录；本轮打包没有重新产生模型接口用量。

## 验证范围说明

业务算法正确性需要结合领域验证产物；运行审计仅验证保存的验收闭合。城市原始数据和匿名化边界应由提交团队在最终公开或分发前确认。未完成的物理多机、参数切分及系统性对照实验不在通过声明中。
""",
    }
