"""Резюме в PDF (WeasyPrint)."""

from __future__ import annotations

import re
from pathlib import Path

import markdown as md

from ... import paths

CSS = """
@page { size: A4; margin: 16mm 16mm; }
body { font-family: "DejaVu Sans", "Noto Sans", sans-serif; font-size: 10.5pt; line-height: 1.38; color: #1a1a1a; }
h1 { font-size: 20pt; margin: 0 0 2mm; }
h2 { font-size: 12pt; margin: 6mm 0 2mm; padding-bottom: 1mm; border-bottom: 0.4pt solid #999; text-transform: uppercase; letter-spacing: .04em; }
h3 { font-size: 11pt; margin: 4mm 0 1mm; }
p { margin: 1mm 0; }
ul { margin: 1mm 0 2mm; padding-left: 5mm; }
li { margin: .6mm 0; }
a { color: #1a4fd6; text-decoration: none; }
"""


def render_pdf(markdown_text: str, name: str) -> Path:
    try:
        from weasyprint import HTML
    except ImportError as e:
        raise RuntimeError("Для PDF-резюме нужны зависимости: pip install sotto[jobs] "
                           "(weasyprint на Windows требует GTK/Pango — см. README)") from e
    out_dir = paths.JOBS_DIR / "pdf"
    out_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^\w\-]+", "_", name).strip("_")[:60] or "resume"
    out = out_dir / f"{safe}.pdf"
    html = f"<html><head><meta charset='utf-8'><style>{CSS}</style></head><body>{md.markdown(markdown_text, extensions=['sane_lists'])}</body></html>"
    HTML(string=html).write_pdf(str(out))
    return out
