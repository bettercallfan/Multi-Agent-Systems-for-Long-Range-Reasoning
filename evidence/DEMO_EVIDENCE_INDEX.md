# 比赛演示证据索引

## 三场景完整闭环

| 场景 | 运行目录 | 主要结果 | 完整审计 |
|---|---|---|---|
| MathorCup 装箱优化 | `outputs/runs/20260908_124006` | 300 件货物、边界/重叠/载重/成本确定性复算 | PASS |
| 差旅报销 | `outputs/runs/20260908_125323` | PDF/DOCX/XLSX 联合核验、CNY 1,485、异常清单 | PASS |
| 城市多模态治理 | `outputs/runs/20260908_173502` | 四模态画像、隐私边界、自动修复 | PASS |

每个运行都保留 `task_spec.json`、`task_graph.json`、`run_state.json`、`agent_trace.md`、
`artifacts/`、`review/` 和 `final_report.md`。成功判定来自 `utils/run_audit.py` 的九项
独立闸门，而不是模型自述或单个 JSON 状态。

## 推荐录屏顺序

1. 打开 `evidence/metrics/cross_scenario_metrics.md`，展示 3/3 PASS 和三种任务类型。
2. 在看板依次切换三个运行，展示最终闸门、动态图、产物与 Token 指标。
3. 打开 `evidence/fault_recovery_city.md`，展示城市首次失败、定位代码节点、四节点子图重放和最终成功。
4. 打开三个 `final_report.md` 及关键领域校验文件。
5. 打开 `evidence/metrics/long_horizon_1000.md`，展示 1000/1000、第 500 步恢复、0 重复和 40 checkpoint。
6. 打开 `evidence/metrics/urban_business_1000.md`，展示真实数据 1000 WorkUnit、100 个跨模态综合步骤、10 次真实模型推理及完整审计 PASS。
7. 打开 `evidence/metrics/mathorcup_business_1000.md`，展示 1000 个装箱候选的真实生成与评估、第 500 步恢复、独立领域复算及完整审计 PASS。

## MathorCup 第二个千步场景

配置并探测真实模型路由（不会打印 API Key）：

```bash
export DEVICE_MODEL="qwen3.5-flash"
export EDGE_MODEL="qwen3.5-flash"
export CLOUD_MODEL="qwen3.5-35b-a3b"
python utils/probe_model_routes.py
```

无模型调用的确定性复现：

```bash
conda run -n agent python utils/mathorcup_long_horizon.py
```

启用每 100 个候选一次的真实模型阶段审查：

```bash
conda run -n agent python utils/mathorcup_long_horizon.py --live-reasoning
```

该口径是 1000 个真实候选方案工作单元和最多 10 次阶段模型审查，不是 1000 次模型调用。最终结果仍由现有 Python 领域验证器独立复算。

## 真实业务长程证据

- 正式运行：`outputs/runs/20260909_urban_business_1000`；
- 业务步骤：1000/1000，唯一 WorkUnit 1000，重复执行 0；
- 跨模态综合：100 步，其中 10 步调用真实阶段模型；
- 恢复：第 500 步从签名 checkpoint 恢复，共 40 个 SHA256 checkpoint；
- 数据覆盖：511 条遥感元数据、80,000 条法规、80,000 条匿名通联、500,000 个网络包；
- 交付状态：`run_state.outcome=success`，完整审计 9/9 PASS。

## 三类运行期动态注入恢复

- 正式运行：`outputs/runs/20260910_163108`；
- 节点失效：`s001_prv_file_reading_identification` 第一次执行受控失败，第 2 次执行成功；
- 需求变更：保留已完成节点，将待执行节点契约升级，TaskGraph 版本从 1 递增为 2；
- 数据异常：实际修改 `inputs/task.md` 的运行副本，SHA256 检出、隔离并恢复，第 2 次执行成功；
- 主运行完整审计：9/9 PASS；注入恢复独立审计：9/9 PASS；
- 汇总轨迹：`outputs/runs/20260910_163108/injections/recovery_trace.md`。

复现命令：

```bash
python main.py --input examples/expense_reimbursement \
  --injection-profile recovery-demo
python utils/injection_audit.py outputs/runs/<run_id>
```

## 城市数据可复算指标

- 遥感元数据：511 条；
- 政策法规：80,000 条；
- 匿名电话通联：80,000 条；
- PCAP：500,000 包、52,753,722 字节；
- TCP 5,652，UDP 252,991，ICMP 240,894。

关键文件：

- `outputs/runs/20260908_173502/artifacts/urban_multimodal_result.json`
- `outputs/runs/20260908_173502/artifacts/urban_validation_log.json`
- `outputs/runs/20260908_173502/review/business_validation.json`
- `evidence/fault_recovery_city.json`

## 口径边界

- 基础设施 1000 步是确定性任务图压力测试；真实业务 1000 WorkUnit 包含 100 个综合步骤和 10 次真实模型调用，均不表述为 1000 次模型调用；
- 端边云为可替换的模拟资源后端，不是已部署的真实多机集群；
- 推荐路由配置为 device/edge `qwen3.5-flash`、cloud `qwen3.5-35b-a3b`；路由证据记录模型名和选择理由，但不把阶段级路由表述为参数层模型切分；
- 遥感场景处理元数据和 caption，未做像素级识别；
- PCAP 只做包头统计，城市场景不进行个人级跨模态身份关联或因果推断。
