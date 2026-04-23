#!/usr/bin/env python3
"""Render a Markdown file to PDF (html intermediate) for reports. Requires: pip install markdown xhtml2pdf."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

try:
    import markdown
    from xhtml2pdf import pisa
except ImportError:
    print("Run: pip install markdown xhtml2pdf", file=sys.stderr)
    raise SystemExit(1)

CSS = """
@page { size: A4; margin: 18mm; }
html { font-family: DejaVu Sans, Helvetica, Arial, sans-serif; font-size: 9.5pt; line-height: 1.35; }
h1 { font-size: 16pt; border-bottom: 1px solid #333; padding-bottom: 4px; }
h2 { font-size: 12.5pt; margin-top: 1.2em; color: #1a1a1a; }
h3 { font-size: 11pt; }
code, pre { font-size: 8.5pt; }
pre { background: #f4f4f4; padding: 8px; border-left: 3px solid #ccc; white-space: pre-wrap; }
table { border-collapse: collapse; width: 100%; font-size: 8.5pt; }
th, td { border: 1px solid #999; padding: 4px 6px; vertical-align: top; }
th { background: #eee; }
blockquote { border-left: 3px solid #666; margin-left: 0; padding-left: 10px; color: #444; }
hr { border: 0; border-top: 1px solid #ccc; margin: 1.5em 0; }
ul, ol { margin-left: 1.2em; }
.mermaid { font-size: 8pt; color: #333; }
"""


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("input", type=Path, help="Path to .md file")
    p.add_argument(
        "-o", "--output", type=Path, default=None, help="Output .pdf (default: same name as .md)"
    )
    args = p.parse_args()
    md_path: Path = args.input
    if not md_path.is_file():
        print(f"Not found: {md_path}", file=sys.stderr)
        raise SystemExit(2)
    out_path = args.output
    if out_path is None:
        out_path = md_path.with_suffix(".pdf")

    text = md_path.read_text(encoding="utf-8")
    body = markdown.markdown(
        text,
        extensions=[
            "markdown.extensions.extra",
            "markdown.extensions.tables",
            "markdown.extensions.toc",
            "markdown.extensions.fenced_code",
        ],
    )
    # Mark mermaid blocks (```mermaid) for styling (still plain text in PDF)
    full_html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8"/>
<style>{CSS}</style>
</head>
<body>
{body}
</body>
</html>"""

    with out_path.open("wb") as pdf_file:
        status = pisa.CreatePDF(
            full_html,
            dest=pdf_file,
            encoding="utf-8",
            show_error_as_pdf=True,
        )
    if status.err:
        print(f"xhtml2pdf reported errors; output may be incomplete: {out_path}", file=sys.stderr)
    print(f"Wrote {out_path.resolve()}")


if __name__ == "__main__":
    main()
