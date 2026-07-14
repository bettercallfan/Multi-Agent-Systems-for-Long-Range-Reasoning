import json
from pathlib import Path


def read_docx(path: str):
    result = {
        "path": path,
        "type": "docx",
        "status": "unknown",
        "text_preview": "",
        "paragraph_count": 0,
        "tables_count": 0,
        "error": "",
    }

    try:
        from docx import Document

        doc = Document(path)
        paragraphs = [p.text.strip() for p in doc.paragraphs if p.text.strip()]

        result["status"] = "success"
        result["paragraph_count"] = len(paragraphs)
        result["tables_count"] = len(doc.tables)
        result["text_preview"] = "\n".join(paragraphs[:80])[:6000]

    except Exception as e:
        result["status"] = "error"
        result["error"] = str(e)

    return result


def read_xlsx(path: str):
    result = {
        "path": path,
        "type": "xlsx",
        "status": "unknown",
        "sheets": [],
        "sheet_summaries": {},
        "error": "",
    }

    try:
        import pandas as pd

        with pd.ExcelFile(path, engine="openpyxl") as excel:
            result["sheets"] = excel.sheet_names

            for sheet in excel.sheet_names:
                df = pd.read_excel(excel, sheet_name=sheet)

                sample = df.head(5).fillna("")
                # Pandas 3.x 兼容：优先 map，fallback applymap
                def _safe_convert(x):
                    if hasattr(x, "isoformat"):
                        return x.isoformat()
                    return x

                try:
                    sample = sample.map(_safe_convert)
                except AttributeError:
                    sample = sample.applymap(_safe_convert)

                result["sheet_summaries"][sheet] = {
                    "rows": int(len(df)),
                    "columns": [str(c) for c in df.columns],
                    "dtypes": {str(k): str(v) for k, v in df.dtypes.items()},
                    "sample_rows": sample.to_dict(orient="records"),
                }

        result["status"] = "success"

    except Exception as e:
        result["status"] = "error"
        result["error"] = str(e)

    return result


def read_pdf(path: str):
    result = {
        "path": path,
        "type": "pdf",
        "status": "unknown",
        "page_count": 0,
        "text_preview": "",
        "error": "",
    }

    try:
        text_parts = []

        try:
            import pdfplumber

            with pdfplumber.open(path) as pdf:
                result["page_count"] = len(pdf.pages)

                for page in pdf.pages[:10]:
                    text = page.extract_text()
                    if text:
                        text_parts.append(text)

        except Exception:
            import PyPDF2

            with open(path, "rb") as f:
                reader = PyPDF2.PdfReader(f)
                result["page_count"] = len(reader.pages)

                for page in reader.pages[:10]:
                    text = page.extract_text()
                    if text:
                        text_parts.append(text)

        result["status"] = "success"
        result["text_preview"] = "\n".join(text_parts)[:10000]

    except Exception as e:
        result["status"] = "error"
        result["error"] = str(e)

    return result


def read_file_preview(path: str):
    suffix = Path(path).suffix.lower()

    if suffix == ".docx":
        return read_docx(path)

    if suffix in [".xlsx", ".xls"]:
        return read_xlsx(path)

    if suffix == ".pdf":
        return read_pdf(path)

    result = {
        "path": path,
        "type": suffix.replace(".", "") or "unknown",
        "status": "unknown",
        "text_preview": "",
        "error": "",
    }

    try:
        text = Path(path).read_text(encoding="utf-8", errors="ignore")
        result["status"] = "success"
        result["text_preview"] = text[:5000]
    except Exception as e:
        result["status"] = "error"
        result["error"] = str(e)

    return result


def build_file_previews(files):
    previews = []

    for file_path in files:
        previews.append(read_file_preview(file_path))

    return previews


def save_file_previews(previews, output_path):
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    output_path.write_text(
        json.dumps(previews, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
