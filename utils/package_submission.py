"""Build a strict, secret-free competition submission archive."""

from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import sys
import zipfile

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    from utils.run_audit import audit_run
except ModuleNotFoundError:  # Direct execution: python utils/package_submission.py
    from run_audit import audit_run


REQUIRED_FILES = (
    "main.py", "config.py", "requirements.txt", "run.md",
    "PROJECT_REQUIREMENTS.md", "docs/competition_report.tex",
    "docs/演示改造与提交要求核验.md", "城市多模态数据集/task.md",
    "utils/run_audit.py", "utils/competition_metrics.py",
    "utils/dashboard_server.py", "utils/preflight.py",
    "scripts/finish_competition.sh",
    "examples/mathorcup_d", "examples/expense_reimbursement",
)
SOURCE_ROOTS = (
    "agents", "orchestration", "task_plugins", "utils", "examples", "tests",
    "dashboard", "docs", "scripts", "evidence",
)
SECRET_PATTERNS = (
    re.compile(r"(?:MODEL_API_KEY|DASHSCOPE_API_KEY|OPENAI_API_KEY)\s*=\s*[\"']?sk-", re.I),
    re.compile(r"Authorization\s*:\s*Bearer\s+sk-", re.I),
)


def _missing(root: Path) -> list[str]:
    return [item for item in REQUIRED_FILES if not (root / item).exists()]


def _candidate_files(root: Path, evidence_dirs: list[Path]) -> list[Path]:
    paths: list[Path] = []
    for relative in SOURCE_ROOTS:
        base = root / relative
        if base.is_file():
            paths.append(base)
        elif base.is_dir():
            paths.extend(
                path for path in base.rglob("*")
                if path.is_file() and "__pycache__" not in path.parts
                and path.suffix not in {".pyc", ".sqlite3"}
            )
    for relative in REQUIRED_FILES:
        path = root / relative
        if path.is_file() and path not in paths:
            paths.append(path)
    for evidence in evidence_dirs:
        if evidence.is_dir():
            paths.extend(
                path for path in evidence.rglob("*")
                if path.is_file()
                and path.relative_to(evidence).parts[0] != "inputs"
                and "__pycache__" not in path.parts
                and path.suffix not in {".pyc", ".sqlite3"}
            )
    return sorted(set(paths))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _input_manifest(run: Path) -> dict:
    input_root = run / "inputs"
    files = sorted(path for path in input_root.rglob("*") if path.is_file()) if input_root.is_dir() else []
    entries = [
        {
            "path": path.relative_to(run).as_posix(),
            "size": path.stat().st_size,
            "sha256": _sha256(path),
        }
        for path in files
    ]
    return {
        "schema_version": "1.0",
        "run_id": run.name,
        "raw_inputs_embedded": False,
        "reason": "Run-local raw input copies are excluded from the lightweight evidence package.",
        "file_count": len(entries),
        "total_bytes": sum(item["size"] for item in entries),
        "files": entries,
    }


def _secret_hits(paths: list[Path]) -> list[str]:
    hits: list[str] = []
    text_suffixes = {".py", ".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".sh"}
    for path in paths:
        if path.suffix.lower() not in text_suffixes:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if any(pattern.search(text) for pattern in SECRET_PATTERNS):
            hits.append(path.as_posix())
    return hits


def validate_submission(root: str | Path, run_dirs: list[str | Path]) -> dict:
    project = Path(root).resolve()
    missing = _missing(project)
    audited: list[dict] = []
    task_types: set[str] = set()
    evidence_paths: list[Path] = []
    for raw in run_dirs:
        run = Path(raw).resolve()
        result = audit_run(run)
        if not result["passed"]:
            raise ValueError(f"运行未通过完整审计，不能提交：{run.name}")
        spec_path = run / "task_spec.json"
        try:
            spec = json.loads(spec_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError(f"运行缺少有效 task_spec.json：{run}") from exc
        task_type = str(spec.get("task_type") or "unknown")
        task_types.add(task_type)
        audited.append({"run_dir": run.as_posix(), "task_type": task_type, "run_id": run.name})
        evidence_paths.append(run)
    if missing:
        raise ValueError("提交材料缺失：" + ", ".join(missing))
    if len(audited) < 2 or len(task_types) < 2:
        raise ValueError("提交包至少需要两个不同 task_type 的完整审计 PASS 运行")
    files = _candidate_files(project, evidence_paths)
    hits = _secret_hits(files)
    if hits:
        raise ValueError("候选提交文件疑似包含 API Key：" + ", ".join(hits))
    return {
        "schema_version": "1.0", "generated_at": datetime.now().isoformat(timespec="seconds"),
        "project_root": project.as_posix(), "run_count": len(audited),
        "task_types": sorted(task_types), "runs": audited,
        "file_count": len(files), "files": [path.relative_to(project).as_posix() for path in files],
        "raw_input_copies_embedded": False,
        "input_manifest_entries": [f"evidence/input_manifests/{run.name}.json" for run in evidence_paths],
    }


def build_archive(root: str | Path, run_dirs: list[str | Path], output: str | Path) -> dict:
    project = Path(root).resolve()
    manifest = validate_submission(project, run_dirs)
    output_path = Path(output).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path = project / ".submission_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    files = [project / relative for relative in manifest["files"]]
    evidence_dirs = [Path(path).resolve() for path in run_dirs]
    try:
        with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in files:
                archive.write(path, path.relative_to(project).as_posix())
            for run in evidence_dirs:
                input_manifest = _input_manifest(run)
                archive.writestr(
                    f"evidence/input_manifests/{run.name}.json",
                    json.dumps(input_manifest, ensure_ascii=False, indent=2),
                )
            archive.write(manifest_path, "submission_manifest.json")
    finally:
        manifest_path.unlink(missing_ok=True)
    return {"output": output_path.as_posix(), "manifest": manifest}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", action="append", required=True,
                        help="repeat for each complete audit-PASS run")
    parser.add_argument("--output", default="submission/competition_submission.zip")
    parser.add_argument("--project-root", default=".")
    args = parser.parse_args()
    try:
        result = build_archive(args.project_root, args.run_dir, args.output)
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        parser.error(str(exc))
    print(f"Submission archive: {result['output']}")
    print(f"Runs: {result['manifest']['run_count']} / task types: {', '.join(result['manifest']['task_types'])}")
    print(f"Files: {result['manifest']['file_count']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
