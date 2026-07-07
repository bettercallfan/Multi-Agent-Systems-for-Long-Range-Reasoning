import json
from pathlib import Path


class RunContext:
    """
    轻量级运行时上下文，在任务启动时汇总所有关键信息，
    并负责动态注入到各 Agent 的 system prompt 中。

    RunContext 是静态快照 —— 创建后不变。
    运行过程中变化的状态请用 RunState。
    """

    def __init__(self, task_spec: dict, file_previews: list, run_dir: str):
        self.task_spec = task_spec
        self.file_previews = file_previews
        self.run_dir = Path(run_dir)

        self.task_type = task_spec.get("task_type", "general_complex_task")
        self.task_name = task_spec.get("task_name", "未命名任务")
        self.input_files = task_spec.get("input", {}).get("files", [])
        self.requirements = task_spec.get("requirements", {})
        self.success_criteria = task_spec.get("success_criteria", {})
        self.expected_artifacts = self.success_criteria.get("required_artifacts", [])
        self.required_sections = self.success_criteria.get("required_report_sections", [])

        # 读取 code_policy
        self.code_policy = task_spec.get("code_policy", {})
        self.allows_complex = self.code_policy.get("allows_complex", False)
        self.allows_lightweight = self.code_policy.get("allows_lightweight", False)
        self.requires_code = self.allows_complex or self.allows_lightweight

    def to_prompt_context(self) -> str:
        """
        生成注入到 Agent system prompt 中的上下文字符串。
        注意：这里只提供信息，不规定流程。
        """

        lines = [
            "",
            "---",
            "## 当前任务上下文（RunContext）",
            "",
            f"- 任务名称：{self.task_name}",
            f"- 任务类型：{self.task_type}",
            f"- 运行目录：{self.run_dir}",
            "",
        ]

        # 输入文件摘要
        if self.input_files:
            lines.append("### 输入文件")
            for f in self.input_files:
                fname = Path(f).name if "/" in f or "\\" in f else f
                lines.append(f"- {fname}")
            lines.append("")

        # 文件预读取摘要
        if self.file_previews:
            lines.append("### 文件内容摘要（file_previews）")
            for preview in self.file_previews:
                status = preview.get("status", "unknown")
                fpath = preview.get("path", "")
                fname = Path(fpath).name

                if status == "success":
                    ptype = preview.get("type", "")
                    if ptype == "pdf":
                        pages = preview.get("page_count", "?")
                        text = preview.get("text_preview", "")
                        lines.append(f"- **{fname}** (PDF, {pages}页):")
                        # 只取前 500 字作为 Agent system prompt 的上下文提示
                        if text:
                            lines.append(f"  {text[:500]}")
                    elif ptype == "docx":
                        paras = preview.get("paragraph_count", "?")
                        tables_n = preview.get("tables_count", "?")
                        text = preview.get("text_preview", "")
                        lines.append(f"- **{fname}** (Word, {paras}段, {tables_n}表):")
                        if text:
                            lines.append(f"  {text[:500]}")
                    elif ptype in ("xlsx", "xls"):
                        sheets = preview.get("sheets", [])
                        summaries = preview.get("sheet_summaries", {})
                        lines.append(f"- **{fname}** (Excel, {len(sheets)}个sheet):")
                        for s in sheets:
                            info = summaries.get(s, {})
                            rows = info.get("rows", "?")
                            cols = info.get("columns", [])
                            lines.append(f"  - Sheet [{s}]: {rows}行, 列: {cols}")
                    else:
                        text = preview.get("text_preview", "")
                        lines.append(f"- **{fname}** ({ptype}):")
                        if text:
                            lines.append(f"  {text[:300]}")
                else:
                    error = preview.get("error", "未知错误")
                    lines.append(f"- **{fname}**: 解析失败 ({error})，请用 FileSurfer 查看")
            lines.append("")

        # 预期产物
        if self.expected_artifacts:
            lines.append("### 预期产物")
            for a in self.expected_artifacts:
                lines.append(f"- {a}")
            lines.append("")

        # 报告要求章节
        if self.required_sections:
            lines.append("### 报告必须包含的章节")
            for s in self.required_sections:
                lines.append(f"- {s}")
            lines.append("")

        # code_policy 说明
        lines.append("### 代码策略（code_policy）")
        if self.allows_complex:
            lines.append("允许复杂代码。分步生成，小步验证，超限降级。")
        elif self.allows_lightweight:
            lines.append("仅允许轻量工具脚本（JSON写入、字段校验、金额汇总）。")
            lines.append("禁止复杂算法、模型训练、优化求解。单次脚本≤80行，失败后不重试。")
            lines.append("如果 file_previews 中的数据已足够，可以不调用 CodeModelingAgent。")
        else:
            lines.append("本任务不要求代码。Orchestrator 可根据需要灵活决定。")
            lines.append("如果不需要代码，请直接进行分析和报告生成。")

        lines.append("")
        lines.append("**重要：以上是当前任务的信息摘要。你应该根据这些信息理解任务，")
        lines.append("而不是假定一个固定的执行流程。具体执行顺序由 Orchesrator 动态决定。**")
        lines.append("")

        return "\n".join(lines)

    def get_input_file_paths(self, relative_to_run: bool = False):
        """返回输入文件在本 run 目录下的路径列表"""
        if relative_to_run:
            return [str(self.run_dir / "inputs" / Path(f).name) for f in self.input_files]
        return list(self.input_files)
