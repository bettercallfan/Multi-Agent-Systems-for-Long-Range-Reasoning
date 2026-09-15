"""Check the local environment before starting a competition run."""

from __future__ import annotations

import argparse
import importlib
import os
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def check(input_path: str, output_root: str = "outputs/runs") -> tuple[bool, list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    required_env = ("MODEL_API_KEY", "MODEL_BASE_URL", "MODEL_NAME")
    missing = [name for name in required_env if not os.getenv(name)]
    if missing:
        errors.append("缺少模型环境变量：" + ", ".join(missing))
    if os.getenv("MODEL_API_KEY", "").strip().lower() in {"你的key", "your-key", "changeme"}:
        errors.append("MODEL_API_KEY 仍是占位值")
    path = Path(input_path).expanduser().resolve()
    if not path.exists():
        errors.append(f"输入路径不存在：{path}")
    elif path.is_dir() and not any(item.is_file() for item in path.rglob("*")):
        errors.append(f"输入目录没有文件：{path}")
    output = Path(output_root).expanduser().resolve()
    try:
        output.mkdir(parents=True, exist_ok=True)
        probe = output / ".preflight-write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        errors.append(f"输出目录不可写：{output} ({exc})")
    for module in (
        "autogen_agentchat", "autogen_core", "autogen_ext", "pydantic",
        "docx", "pdfplumber", "pandas",
    ):
        try:
            importlib.import_module(module)
        except Exception as exc:
            errors.append(f"Python 依赖不可用 {module}: {type(exc).__name__}: {exc}")
    try:
        importlib.import_module("task_plugins.mathorcup_d.solver")
        importlib.import_module("task_plugins.expense_reimbursement.solver")
    except Exception as exc:
        errors.append(f"任务插件加载失败：{type(exc).__name__}: {exc}")
    if os.getenv("MODEL_BASE_URL", "").startswith("http://"):
        warnings.append("MODEL_BASE_URL 使用明文 HTTP，请确认这是受控内网")
    if os.getenv("DEVICE_MODEL") or os.getenv("EDGE_MODEL") or os.getenv("CLOUD_MODEL"):
        configured_routes = {
            "device": os.getenv("DEVICE_MODEL", "qwen3.5-flash"),
            "edge": os.getenv("EDGE_MODEL", os.getenv("DEVICE_MODEL", "qwen3.5-flash")),
            "cloud": os.getenv("CLOUD_MODEL", os.getenv("MODEL_NAME", "qwen3.5-35b-a3b")),
        }
        if len(set(configured_routes.values())) < 2:
            warnings.append("端边云模型路由当前只有一个不同模型；可配置 DEVICE_MODEL/EDGE_MODEL/CLOUD_MODEL")
    if sys.version_info < (3, 10):
        errors.append(f"Python 版本过低：{sys.version.split()[0]}，需要 3.10+")
    return not errors, [*("警告：" + item for item in warnings), *errors]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True)
    parser.add_argument("--output-root", default="outputs/runs")
    args = parser.parse_args()
    passed, messages = check(args.input, args.output_root)
    print("Preflight: " + ("PASS" if passed else "FAIL"))
    for message in messages:
        print("- " + message)
    if passed:
        print("模型密钥已设置（值不会被打印）。")
        print("可以开始运行任务，并在结束后执行 python utils/run_audit.py。")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
