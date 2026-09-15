"""Assemble a local, evidence-backed delivery directory without publishing it.

Run with the project's agent Python. Generated artifacts live in --output;
temporary rendering and the extracted system live in --work. Existing source
and historical runs are never edited by this builder.
"""
from __future__ import annotations
import argparse
import hashlib
import html
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from utils.run_audit import audit_run
from utils.injection_audit import audit_runtime_injections
from utils.submission_assets import TITLE, SUBTITLE, TEAM, MEMBERS, RUNS, SLIDES, documents

ENV = {**os.environ, "LD_LIBRARY_PATH": "/usr/lib/x86_64-linux-gnu"}
SECRET = re.compile(rb"\bsk-[A-Za-z0-9_-]{20,}\b")
PREAMBLE = r"""\documentclass[UTF8,a4paper,11pt]{ctexart}
\usepackage[margin=2.2cm]{geometry}
\usepackage{graphicx,booktabs,longtable,array,tabularx,amsmath,amssymb,xcolor,hyperref}
\usepackage{fontspec}
\setlength{\emergencystretch}{3em}
\setlength{\LTcapwidth}{\textwidth}
\hypersetup{colorlinks=true,urlcolor=blue,linkcolor=blue}
"""


def sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def run(args, cwd=ROOT, log=None, env=None):
    result = subprocess.run([str(x) for x in args], cwd=cwd, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if log:
        Path(log).write_bytes(result.stdout)
    if result.returncode:
        raise RuntimeError(f"Command failed ({result.returncode}): {args[0]}; log={log}")
    return result.stdout


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def copy_file(src, dest):
    if src.is_symlink():
        raise ValueError(f"Unexpected symlink: {src}")
    if src.suffix.lower() in {".py", ".txt", ".md", ".json", ".jsonl", ".yaml", ".yml", ".sh", ".tex"}:
        if SECRET.search(src.read_bytes()):
            raise ValueError(f"Potential secret; excluded from publication until reviewed: {src.relative_to(ROOT)}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    if sha(src) != sha(dest):
        raise ValueError("Copy checksum mismatch")


def copy_tree(src, dest, omit_inputs=False):
    for path in sorted(src.rglob("*")):
        rel = path.relative_to(src)
        if not path.is_file() or any(x in {"__pycache__", ".git", ".venv"} for x in rel.parts):
            continue
        if path.suffix in {".pyc", ".sqlite3"} or path.name == "key.md" or (path.name.startswith(".env") and path.name != ".env.example"):
            continue
        if omit_inputs and rel.parts[0] == "inputs":
            continue
        copy_file(path, dest / rel)


def compile_tex(tex, out):
    run(["latexmk", "-xelatex", "-interaction=nonstopmode", "-halt-on-error", tex.name],
        cwd=tex.parent, log=tex.with_suffix(".build.log"))
    shutil.copy2(tex.with_suffix(".pdf"), out)


def markdown_pdf(text, stem, output, work):
    md = work / (stem + ".md")
    md.write_text(text, encoding="utf-8")
    output.parent.mkdir(parents=True, exist_ok=True)
    run(["pandoc", md, "-o", output, "--pdf-engine=xelatex", "-V", "documentclass=ctexart",
         "-V", "geometry:margin=2.2cm", "-V", "fontsize=11pt", "-V", "colorlinks=true",
         "-V", "CJKmainfont=Noto Serif CJK SC", "-V", "monofont=DejaVu Sans Mono"],
        log=work / (stem + ".build.log"))
    shutil.copy2(md, output.with_suffix(".md"))


def source_and_evidence(out, work):
    source = work / "system_source"
    source.mkdir(exist_ok=True)
    for name in ["agents", "orchestration", "task_plugins", "utils", "tests", "dashboard", "scripts", "examples", "evidence", "deployment", "城市多模态数据集"]:
        copy_tree(ROOT / name, source / name)
    for name in ["main.py", "config.py", "requirements.txt", "requirements-optional.txt", "run.md", "PROJECT_REQUIREMENTS.md"]:
        copy_file(ROOT / name, source / name)
    for name in ["Dockerfile", ".dockerignore", ".env.example", "requirements-runtime.txt"]:
        copy_file(ROOT / "deployment" / name, source / name)
    copy_file(ROOT / "docs/competition_report.tex", source / "docs/competition_report.tex")
    copy_tree(ROOT / "docs/figures", source / "docs/figures")
    for name in ["演示改造与提交要求核验.md"]:
        copy_file(ROOT / "docs" / name, source / "docs" / name)
    (source / "memory").mkdir(exist_ok=True)
    (source / "outputs/dashboard_jobs").mkdir(parents=True, exist_ok=True)
    (source / "README.md").write_text(
        f"# {TITLE}\n\n{TEAM}\n\n首次使用见外层部署运行手册。\n\n"
        "无密钥回放：`bash scripts/start_demo.sh --replay`。\n\n"
        "真实执行：安装 requirements-runtime.txt，配置模型环境后使用 --live。\n\n"
        "城市原始数据保留在城市多模态数据集目录；历史城市运行不重复嵌入 inputs。\n",
        encoding="utf-8")
    audits = []
    for group, ids in RUNS.items():
        for ident in ids:
            src = ROOT / "outputs/runs" / ident
            audit = audit_run(src)
            if not audit["passed"]:
                raise ValueError(f"Historical run failed audit: {ident}")
            dest = out / "03_运行证据" / group / ident
            copy_tree(src, dest, omit_inputs=True)
            copy_tree(src, source / "outputs/runs" / ident, omit_inputs=(ident == "20260908_173502"))
            audit["run_dir"] = ident
            write_json(dest / "run_audit.json", audit)
            entry = {"run_id": ident, "group": group, "run_audit": audit}
            if group == "动态注入恢复证据":
                injection = audit_runtime_injections(src)
                if not injection["passed"]:
                    raise ValueError(f"Injection audit failed: {ident}")
                injection["run_dir"] = ident
                write_json(dest / "injection_audit.json", injection)
                entry["injection_audit"] = injection
            originals = [{"path": p.relative_to(src).as_posix(), "size": p.stat().st_size, "sha256": sha(p)}
                         for p in sorted((src / "inputs").rglob("*")) if p.is_file()]
            write_json(dest / "input_manifest.json", {"embedded": False, "files": originals,
                       "reason": "Raw source inputs are supplied once inside system_source.zip; historical copies are not duplicated here."})
            audits.append(entry)
    stress = Path(json.loads((ROOT / "evidence/metrics/long_horizon_1000.json").read_text())["run_dir"])
    copy_tree(ROOT / stress, out / "03_运行证据/千步长程证据/基础设施压力测试" / stress.name)
    copy_tree(ROOT / stress, source / stress)
    copy_tree(ROOT / "evidence/metrics", out / "03_运行证据/汇总指标")
    write_json(out / "03_运行证据/重新审计汇总.json", audits)
    copy_file(ROOT / "deployment/audit_evidence.py", out / "03_运行证据/复核运行证据.py")
    for name in ["__init__.py", "run_audit.py", "injection_audit.py"]:
        copy_file(ROOT / "utils" / name, out / "03_运行证据/utils" / name)
    system = out / "02_可运行系统"
    for name in ["Dockerfile", "docker-compose.yml", ".env.example", "bootstrap.py"]:
        copy_file(ROOT / "deployment" / name, system / name)
    (source / "requirements-environment.txt").write_bytes(
        run([sys.executable, "-m", "pip", "list", "--format=freeze"]))
    return source, audits


def technical_materials(out, work):
    target = out / "01_技术材料"
    target.mkdir(parents=True, exist_ok=True)
    copy_file(ROOT / "docs/competition_report.pdf", target / "技术方案与验收报告.pdf")
    copy_file(ROOT / "docs/competition_report.tex", target / "LaTeX源稿/competition_report.tex")
    copy_tree(ROOT / "docs/figures", target / "LaTeX源稿/figures")
    src = (ROOT / "docs/competition_report.tex").read_text()
    pre = src.split(r"\begin{document}", 1)[0]
    algs = re.findall(r"\\begin\{algorithm\}.*?\\end\{algorithm\}", src, re.S)
    complexity = src.split(r"\section{核心模块复杂度分析}", 1)[1].split(r"\section{可信自治执行的工程代价}", 1)[0]
    appendix = work / "appendix.tex"
    appendix.write_text(pre + r"\begin{document}\chapter*{核心算法与复杂度附录}" + "\n"
        + "本附录从同版技术报告抽取四项核心伪代码与复杂度分析；不增加新的实验结论。资源路由采用硬约束过滤与软评分排序，详见原报告对应机制说明。\n"
        + "\n\\clearpage\n".join(algs) + r"\clearpage\section*{核心模块复杂度分析}" + complexity + r"\end{document}", encoding="utf-8")
    compile_tex(appendix, target / "核心算法与复杂度附录.pdf")
    architecture = work / "architecture.tex"
    architecture.write_text(PREAMBLE.replace("a4paper,11pt", "a4paper,landscape,11pt") + r"\begin{document}\pagestyle{empty}\centering"
        + r"{\LARGE\bfseries 系统架构图\par}\vspace{0.3cm}"
        + "\n" + r"\includegraphics[width=\textwidth,height=0.80\textheight,keepaspectratio]{"
        + str(ROOT / "docs/figures/系统整体流程框架图.png") + r"}\par" + "\n"
        + r"{\small " + TEAM + r"}\end{document}", encoding="utf-8")
    compile_tex(architecture, target / "系统架构图.pdf")
    report_docx(src, pre, target, work)


def report_docx(src, pre, target, work):
    """Keep all algorithms/formulas exactly rendered; body/tables stay editable."""
    body = src.split(r"\frontmatter", 1)[1].rsplit(r"\end{document}", 1)[0]
    pattern = re.compile(r"\\begin\{figure\}.*?\\end\{figure\}|\\begin\{algorithm\}.*?\\end\{algorithm\}|\\\[.*?\\\]|(?<!\\)\$(?!\$)(?:\\.|[^$])*?(?<!\\)\$", re.S)
    matches = list(pattern.finditer(body))
    media = work / "report-media"
    media.mkdir(exist_ok=True)
    renders = []
    for m in matches:
        raw = m[0]
        inline = raw.startswith("$")
        if raw.startswith(r"\begin{figure}"):
            raw = re.sub(r"\\begin\{figure\}(?:\[[^]]*\])?", "", raw)
            raw = raw.replace(r"\end{figure}", "").replace(r"\caption{", r"\captionof{figure}{")
        if inline:
            renders.append(r"\begin{preview}" + raw + r"\end{preview}")
        else:
            renders.append(r"\begin{preview}\begin{minipage}{\textwidth}" + raw + r"\end{minipage}\end{preview}")
    tex = media / "assets.tex"
    tex.write_text(pre + "\n" + r"\usepackage{capt-of}\usepackage[active,tightpage]{preview}\setlength{\PreviewBorder}{1pt}"
                   + "\n" + r"\begin{document}" + "\n" + "\n".join(renders) + r"\end{document}", encoding="utf-8")
    # Resolve the report's relative figure paths without copying or editing originals.
    shutil.copytree(ROOT / "docs/figures", media / "figures", dirs_exist_ok=True)
    compile_tex(tex, media / "rendered.pdf")
    run(["/usr/bin/pdftoppm", "-r", "160", "-png", media / "rendered.pdf", media / "asset"], env=ENV,
        log=media / "raster.log")
    images = sorted(media.glob("asset-*.png"))
    if len(images) != len(matches):
        raise ValueError(f"DOCX asset count mismatch: {len(images)} != {len(matches)}")
    transformed = []
    last = 0
    for m, path in zip(matches, images):
        transformed.extend([body[last:m.start()], r"\includegraphics{" + path.as_posix() + "}"])
        last = m.end()
    transformed.append(body[last:])
    note = "转换说明：正文和表格可编辑；公式、算法伪代码和图示使用同版原稿渲染图。正式版式与编号以 PDF 为准；完整 LaTeX 源稿随包保留。"
    doc_tex = work / "editable-report.tex"
    doc_tex.write_text(pre + r"\begin{document}" + "\n" + note + "\n\n" + "".join(transformed) + r"\end{document}", encoding="utf-8")
    dest = target / "技术方案与验收报告.docx"
    run(["pandoc", doc_tex, "-f", "latex", "-t", "docx", "--toc", "--number-sections",
         "--metadata", "title=" + TITLE + "——" + SUBTITLE, "--metadata", "author=" + TEAM + "；团队成员：" + MEMBERS,
         "-o", dest], log=work / "docx-conversion.log")
    from docx import Document
    from docx.shared import Pt, Cm
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    doc = Document(dest)
    for section in doc.sections:
        section.page_width, section.page_height = Cm(21), Cm(29.7)
        section.left_margin = section.right_margin = Cm(2.2)
    for style in doc.styles:
        if style.type in (1, 2):
            style.font.name = "Noto Serif CJK SC"
            props = style.element.get_or_add_rPr()
            fonts = props.find(qn("w:rFonts"))
            if fonts is None:
                fonts = OxmlElement("w:rFonts"); props.append(fonts)
            fonts.set(qn("w:eastAsia"), "Noto Serif CJK SC")
    doc.styles["Normal"].font.size = Pt(11)
    for p in doc.paragraphs:
        if p.style.name == "Heading 1":
            p.paragraph_format.page_break_before = True
    doc.save(dest)
    write_json(target / "DOCX转换说明.json", {"source_sha256": sha(ROOT / "docs/competition_report.tex"),
        "rendered_math_algorithm_figure_count": len(matches), "editable": ["body", "tables", "headings"],
        "rendered_as_images": ["equations", "algorithms", "figures"], "layout_authority": "技术方案与验收报告.pdf"})


def defense(out, work):
    from pptx import Presentation
    from pptx.util import Inches, Pt
    from pptx.dml.color import RGBColor
    from pptx.oxml.xmlchemy import OxmlElement
    ppt = Presentation()
    ppt.slide_width, ppt.slide_height = Inches(13.333), Inches(7.5)
    # Shared semantic content; PDF is a screen-rendered presentation, PPTX editable.
    frames = []
    for i, (title, eyebrow, bullets, picture) in enumerate(SLIDES, 1):
        slide = ppt.slides.add_slide(ppt.slide_layouts[6])
        slide.background.fill.solid(); slide.background.fill.fore_color.rgb = RGBColor.from_string("F7F9FC")
        def text(x, y, w, h, value, size, color="20334E", bold=False):
            box = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
            tf = box.text_frame; tf.word_wrap = True
            for j, line in enumerate(value.split("\n")):
                p = tf.paragraphs[0] if j == 0 else tf.add_paragraph()
                p.text = line; p.font.size = Pt(size); p.font.name = "Noto Sans CJK SC"
                p.font.bold = bold; p.font.color.rgb = RGBColor.from_string(color)
                for r in p.runs:
                    ea = OxmlElement("a:ea"); ea.set("typeface", "Noto Sans CJK SC"); r._r.get_or_add_rPr().append(ea)
        text(.65, .4, 11.9, .35, eyebrow, 12, "A4262C", True)
        text(.65, 1.03, 12, .8, title, 29, bold=True)
        if picture:
            from PIL import Image
            path = ROOT / "docs/figures" / picture
            width, height = Image.open(path).size
            scale = min(11.7 / width, 4.5 / height)
            slide.shapes.add_picture(str(path), Inches((13.333-width*scale)/2), Inches(1.9),
                                     width=Inches(width*scale), height=Inches(height*scale))
            content = '<img class="diagram" src="' + path.as_uri() + '">'
        else:
            for j, bullet in enumerate(bullets):
                text(.8, 2.05 + j*.8, 11.8, .72, bullet, 20 if len(bullet)<65 else 17)
            content = '<div class="bullets">' + ''.join('<p>'+html.escape(x)+'</p>' for x in bullets) + '</div>'
        text(.7, 6.96, 11.5, .3, "西安交通大学 · XH-202631 · 证据来自归档运行；能力边界见技术报告", 10, "62728A")
        text(12.15, 6.96, .6, .3, f"{i:02}", 10)
        frames.append(f'<section><div class="eyebrow">{html.escape(eyebrow)}</div><h1>{html.escape(title)}</h1>{content}<footer>西安交通大学 · XH-202631 · 证据来自归档运行；能力边界见技术报告<span>{i:02}</span></footer></section>')
    dest = out / "05_答辩材料"
    dest.mkdir(exist_ok=True)
    ppt.save(dest / "答辩演示稿.pptx")
    page = work / "defense.html"
    page.write_text('''<!doctype html><meta charset="utf-8"><style>
    @page{size:1280px 720px;margin:0}*{box-sizing:border-box}body{margin:0;font-family:"Noto Sans CJK SC",sans-serif;color:#20334e}section{width:1280px;height:720px;padding:38px 64px;position:relative;page-break-after:always;background:#f7f9fc}.eyebrow{font-size:17px;color:#a4262c;font-weight:700}h1{font-size:39px;margin:40px 0 30px}.bullets p{font-size:26px;line-height:1.5;margin:22px 10px}.diagram{display:block;margin:auto;max-width:1120px;max-height:435px;object-fit:contain}footer{position:absolute;bottom:27px;left:64px;right:64px;font-size:14px;color:#62728a}footer span{float:right}</style>''' + ''.join(frames), encoding="utf-8")
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        pg = browser.new_page(viewport={"width":1280,"height":720})
        pg.goto(page.as_uri()); pg.evaluate("document.fonts.ready")
        pg.pdf(path=str(dest / "答辩演示稿.pdf"), print_background=True, prefer_css_page_size=True)
        pg.screenshot(path=str(work / "defense-preview.png"))
        browser.close()
    (dest / "答辩讲稿.md").write_text("# 答辩讲稿与来源\n\n" + "\n\n".join(
        f"## {i}. {title}\n\n" + "\n".join("- " + b for b in bullets)
        for i,(title,_,bullets,_) in enumerate(SLIDES,1)) + "\n\n图来自同版技术报告；结果来自03目录。PPTX与PDF同源内容，渲染方式不同。\n", encoding="utf-8")


def finalize(out, source, work):
    video = out / "04_演示视频/系统完整演示.mp4"
    if not video.is_file() or video.stat().st_size < 100_000:
        raise ValueError("A real recorded demonstration is required before finalization")
    system = out / "02_可运行系统"
    with zipfile.ZipFile(system / "system_source.zip", "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for p in sorted(source.rglob("*")):
            rel = p.relative_to(source)
            if (p.is_file() and "__pycache__" not in rel.parts and rel.parts[0] != "memory"
                    and p.suffix not in {".pyc", ".sqlite3"} and p.name != "key.md"
                    and (not p.name.startswith(".env") or p.name == ".env.example")):
                if p.is_symlink():
                    raise ValueError("Source archive must not contain symlinks")
                z.write(p, rel.as_posix())
    inventory = "# 交付物清单\n\n"
    for name, explanation in [
        ("00_请先阅读", "导读、交付物清单、五分钟评审指南"),
        ("01_技术材料", "最新PDF、DOCX、算法复杂度附录、架构图及LaTeX源稿"),
        ("02_可运行系统", "源码ZIP、完整示例输入、Docker配置、环境变量模板、部署手册"),
        ("03_运行证据", "三场景、两组动态注入、两项业务千步、基础设施压力测试及回归日志"),
        ("04_演示视频", "带字幕的历史真实运行回放MP4；不是本轮实时模型执行"),
        ("05_答辩材料", "十页可编辑PPTX、同源内容PDF、讲稿")]:
        inventory += f"## {name}\n\n{explanation}。\n\n"
    inventory += "## 状态说明\n\n清单中的文件已生成。容器启动仍待有 Docker daemon 的环境验证；视频未配音，含文字导览；DOCX公式及伪代码为原稿渲染图，正文与表格可编辑。最终分发前请团队确认数据分发范围、报名要求与视频口径。\n"
    markdown_pdf(inventory, "inventory", out / "00_请先阅读/交付物清单.pdf", work)
    write_json(out / "构建清单.json", {"builder": "build_submission_bundle.py", "generated_at": datetime.now().isoformat(),
        "project_title": TITLE, "source_report_sha256": sha(ROOT / "docs/competition_report.pdf"),
        "video_mode": "historical_real_run_replay", "docker_runtime_verified": False,
        "docx_formula_mode": "original_latex_rendered_images", "runs": RUNS})
    copy_file(ROOT / "deployment/verify_bundle.py", out / "校验提交包.py")
    files = [p for p in sorted(out.rglob("*")) if p.is_file() and p.name != "SHA256SUMS.txt"]
    (out / "SHA256SUMS.txt").write_text("".join(f"{sha(p)}  {p.relative_to(out).as_posix()}\n" for p in files), encoding="utf-8")
    archive = out.with_suffix(".zip")
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for p in sorted(out.rglob("*")):
            if p.is_file(): z.write(p, f"{out.name}/{p.relative_to(out).as_posix()}")
    archive.with_suffix(".zip.sha256").write_text(f"{sha(archive)}  {archive.name}\n", encoding="utf-8")
    print("FINAL", archive, archive.stat().st_size, flush=True)


def main():
    ap=argparse.ArgumentParser();ap.add_argument("--output",required=True);ap.add_argument("--work",required=True)
    ap.add_argument("--phase",choices=["prepare","documents","finalize"],required=True)
    args=ap.parse_args();out=Path(args.output).resolve();work=Path(args.work).resolve()
    if out == ROOT or ROOT in out.parents and out.parent != ROOT / "submission":
        raise ValueError("Use a dedicated child of submission/ for output")
    out.mkdir(parents=True,exist_ok=True);work.mkdir(parents=True,exist_ok=True)
    for name in ["00_请先阅读","01_技术材料","02_可运行系统","03_运行证据","04_演示视频","05_答辩材料"]:
        (out/name).mkdir(exist_ok=True)
    if args.phase=="prepare":
        source_and_evidence(out,work)
        print("Prepared source and audited evidence",flush=True)
    elif args.phase=="documents":
        log=(work/"python-tests.log").read_text(errors="replace")
        match=re.search(r"Ran (\d+) tests in ([\d.]+)s",log)
        if not match or not re.search(r"\nOK(?:\s|$)",log): raise ValueError("Full unittest run has not passed")
        js=(work/"js-tests.log").read_text()
        if "verified" not in js:raise ValueError("JS projection tests have not passed")
        summary=f"Python unittest：{match[1]} 项通过，耗时 {match[2]} 秒；JavaScript 回放投影测试通过。构建时间：{datetime.now().isoformat(timespec='seconds')}。"
        for relative,text in documents(summary).items():
            markdown_pdf(text,Path(relative).name,out/(relative+".pdf"),work)
        for name in ["python-tests.log","js-tests.log"]:
            copy_file(work/name,out/"03_运行证据/测试原始日志"/name)
        technical_materials(out,work);print("Technical PDFs and DOCX done",flush=True)
        defense(out,work);print("Defense deck done",flush=True)
    else:finalize(out,work/"system_source",work)


if __name__=="__main__":main()
