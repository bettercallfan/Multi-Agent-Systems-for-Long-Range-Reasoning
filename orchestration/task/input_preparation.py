"""Create the task-independent input boundary consumed by graph executors."""

from __future__ import annotations

import csv
import json
from pathlib import Path
import struct
from typing import Any


def _json_safe(value: Any) -> Any:
    """Convert library scalar/null values without adding domain semantics."""
    if value is None:
        return None
    try:
        import pandas as pd
        if pd.isna(value):
            return None
    except (ImportError, TypeError, ValueError):
        pass
    if isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except (TypeError, ValueError):
            pass
    return str(value)


def _structured_source(path: Path) -> dict[str, Any]:
    """Read common structured formats losslessly enough for generic executors.

    This is format normalization only: it preserves sheet order, raw rows and
    column positions, and deliberately performs no task-specific field mapping.
    """
    source: dict[str, Any] = {
        "name": path.name,
        "relative_path": f"inputs/{path.name}",
        "suffix": path.suffix.lower(),
        "status": "metadata_only",
    }
    try:
        suffix = path.suffix.lower()
        if suffix in {".xlsx", ".xls"}:
            import pandas as pd
            sheets = pd.read_excel(path, sheet_name=None, header=None)
            source["status"] = "loaded"
            source["kind"] = "workbook"
            source["sheets"] = {
                str(name): [[_json_safe(value) for value in row] for row in frame.to_numpy().tolist()]
                for name, frame in sheets.items()
            }
        elif suffix == ".csv" and path.stat().st_size > 5_000_000:
            with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as stream:
                reader = csv.reader(stream)
                header = next(reader, [])
                samples = []
                row_count = 0
                for row in reader:
                    row_count += 1
                    if len(samples) < 5:
                        samples.append([_json_safe(value) for value in row])
            source["status"] = "loaded"
            source["kind"] = "table_summary"
            source["columns"] = header
            source["row_count"] = row_count
            source["sample_rows"] = samples
        elif suffix == ".csv":
            import pandas as pd
            frame = pd.read_csv(path, header=None)
            source["status"] = "loaded"
            source["kind"] = "table"
            source["rows"] = [
                [_json_safe(value) for value in row]
                for row in frame.to_numpy().tolist()
            ]
        elif suffix == ".json":
            source["status"] = "loaded"
            source["kind"] = "json"
            source["content"] = json.loads(path.read_text(encoding="utf-8"))
        elif suffix == ".jsonl":
            samples = []
            keys: set[str] = set()
            row_count = 0
            with path.open("r", encoding="utf-8-sig", errors="strict") as stream:
                for line in stream:
                    if not line.strip():
                        continue
                    item = json.loads(line)
                    row_count += 1
                    if isinstance(item, dict):
                        keys.update(map(str, item.keys()))
                    if len(samples) < 3:
                        samples.append(item)
            source["status"] = "loaded"
            source["kind"] = "jsonl_summary"
            source["row_count"] = row_count
            source["keys"] = sorted(keys)
            source["sample_records"] = samples
        elif suffix == ".pcap":
            with path.open("rb") as stream:
                header = stream.read(24)
            if len(header) != 24:
                raise ValueError("PCAP global header is incomplete")
            magic = header[:4]
            endian = "<" if magic in {b"\xd4\xc3\xb2\xa1", b"\x4d\x3c\xb2\xa1"} else ">"
            if magic not in {b"\xd4\xc3\xb2\xa1", b"\xa1\xb2\xc3\xd4", b"\x4d\x3c\xb2\xa1", b"\xa1\xb2\x3c\x4d"}:
                raise ValueError("unsupported PCAP magic")
            _, major, minor, _, _, snaplen, network = struct.unpack(endian + "IHHIIII", header)
            source["status"] = "loaded"
            source["kind"] = "packet_capture"
            source["file_size_bytes"] = path.stat().st_size
            source["pcap_version"] = f"{major}.{minor}"
            source["snaplen"] = snaplen
            source["link_type"] = network
        elif suffix == ".docx":
            from docx import Document

            document = Document(path)
            source["status"] = "loaded"
            source["kind"] = "document"
            source["content"] = "\n".join(
                paragraph.text
                for paragraph in document.paragraphs
                if paragraph.text.strip()
            )
            source["tables"] = [
                {
                    "table_index": index,
                    "rows": [
                        [_json_safe(cell.text) for cell in row.cells]
                        for row in table.rows
                    ],
                }
                for index, table in enumerate(document.tables)
            ]
        elif suffix == ".pdf":
            import pdfplumber

            pages: list[dict[str, Any]] = []
            with pdfplumber.open(path) as document:
                for page_index, page in enumerate(document.pages):
                    pages.append({
                        "page_number": page_index + 1,
                        "content": page.extract_text() or "",
                        "tables": [
                            [
                                [_json_safe(cell) for cell in row]
                                for row in (table or [])
                            ]
                            for table in (page.extract_tables() or [])
                        ],
                    })
            source["status"] = "loaded"
            source["kind"] = "document"
            source["pages"] = pages
        elif suffix in {".txt", ".md"}:
            source["status"] = "loaded"
            source["kind"] = "text"
            source["content"] = path.read_text(
                encoding="utf-8", errors="replace",
            )
    except Exception as exc:
        source["status"] = "load_failed"
        source["error"] = f"{type(exc).__name__}: {exc}"
    return source


def prepare_normalized_input(task_spec: dict) -> dict:
    """Return a stable JSON view without applying domain-specific transforms."""
    task_input = task_spec.get("input", {})
    files = [Path(path) for path in task_input.get("files", [])]
    return {
        "schema_version": "1.0",
        "format_contract": {
            "structured_sources": "array of source objects",
            "workbook_sheets": (
                "structured_sources[*].sheets is an object whose values are "
                "raw two-dimensional row arrays; rows are never dictionaries"
            ),
            "table_rows": (
                "structured_sources[*].rows and document tables[*].rows are "
                "raw two-dimensional arrays preserving cell order"
            ),
            "document_content": (
                "DOCX text is in content and tables; PDF text and tables are "
                "stored per page in pages"
            ),
            "preview_role": (
                "file_previews contains representative discovery data only; "
                "complete execution input is under structured_sources"
            ),
        },
        "input": {
            "type": task_input.get("type", "unknown"),
            "text": task_input.get("text", ""),
            "files": [
                Path(path).name for path in task_input.get("files", [])
            ],
        },
        "file_previews": task_spec.get("file_previews", []),
        "structured_sources": [
            _structured_source(path) for path in files if path.is_file()
        ],
    }
