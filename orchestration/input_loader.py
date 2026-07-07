from pathlib import Path


def load_input(input_path: str):
    path = Path(input_path)

    if not path.exists():
        raise FileNotFoundError(f"输入路径不存在: {input_path}")

    if path.is_file():
        return {
            "input_type": "file",
            "files": [str(path)],
            "text": "",
        }

    if path.is_dir():
        files = [str(p) for p in path.rglob("*") if p.is_file()]
        return {
            "input_type": "directory",
            "files": files,
            "text": "",
        }

    raise ValueError(f"不支持的输入路径: {input_path}")
