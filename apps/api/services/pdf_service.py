"""Server-side PDF rendering for workflow reports and action nodes.

``render_pdf_bytes`` turns the markdown an agent produces into a styled,
report-quality PDF (title banner, headings, bold/italic, bullet & numbered
lists, and GitHub-style pipe tables). If anything in the markdown path fails
it falls back to a plain-text PDF so a download is always produced.
"""

from __future__ import annotations

import base64
import re
from typing import Any

_BRAND = "#4F46E5"
_INK = "#1E293B"
_MUTE = "#64748B"
_LINE = "#E2E8F0"
_ZEBRA = "#F8FAFC"


def _safe_filename(title: str) -> str:
    stem = re.sub(r"[^\w\s-]", "", (title or "report").strip())[:80].strip()
    stem = re.sub(r"\s+", "-", stem) or "report"
    return stem if stem.lower().endswith(".pdf") else f"{stem}.pdf"


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _inline(token: Any) -> str:
    """Flatten a markdown-it ``inline`` token's children into ReportLab markup."""
    out: list[str] = []
    for c in token.children or []:
        t = c.type
        if t == "text":
            out.append(_esc(c.content))
        elif t == "code_inline":
            out.append(f'<font face="Courier">{_esc(c.content)}</font>')
        elif t in ("strong_open",):
            out.append("<b>")
        elif t in ("strong_close",):
            out.append("</b>")
        elif t in ("em_open",):
            out.append("<i>")
        elif t in ("em_close",):
            out.append("</i>")
        elif t in ("softbreak", "hardbreak"):
            out.append("<br/>")
        elif t == "link_open":
            href = dict(c.attrs).get("href", "")
            out.append(f'<font color="{_BRAND}">')
            c._href = href  # noqa: SLF001
        elif t == "link_close":
            out.append("</font>")
        else:
            out.append(_esc(getattr(c, "content", "") or ""))
    return "".join(out).strip()


def _render_markdown_pdf(title: str, content: str) -> bytes:
    import io

    from markdown_it import MarkdownIt
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import (
        HRFlowable,
        ListFlowable,
        ListItem,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    ss = getSampleStyleSheet()
    ink, mute, line, brand = (
        colors.HexColor(_INK),
        colors.HexColor(_MUTE),
        colors.HexColor(_LINE),
        colors.HexColor(_BRAND),
    )
    body = ParagraphStyle("body", parent=ss["Normal"], fontSize=10.5, textColor=ink, leading=15, spaceAfter=6)
    hstyles = {
        1: ParagraphStyle("h1", parent=ss["Heading1"], fontSize=16, textColor=brand, spaceBefore=12, spaceAfter=6),
        2: ParagraphStyle("h2", parent=ss["Heading2"], fontSize=13.5, textColor=brand, spaceBefore=12, spaceAfter=5),
        3: ParagraphStyle("h3", parent=ss["Heading3"], fontSize=11.5, textColor=ink, spaceBefore=10, spaceAfter=4),
    }
    cell = ParagraphStyle("cell", parent=ss["Normal"], fontSize=9, textColor=ink, leading=12)
    cellh = ParagraphStyle("cellh", parent=ss["Normal"], fontSize=9, textColor=colors.white, leading=12, alignment=TA_CENTER)

    md = MarkdownIt("commonmark", {"html": False}).enable("table")
    tokens = md.parse(content)

    flow: list[Any] = []
    title = (title or "").strip()
    if title:
        flow.append(Paragraph(_esc(title), ParagraphStyle("title", parent=ss["Title"], fontSize=19, textColor=ink, alignment=0, spaceAfter=4)))
        flow.append(HRFlowable(width="100%", thickness=1, color=line, spaceAfter=8))

    i, n = 0, len(tokens)
    while i < n:
        tk = tokens[i]
        if tk.type == "heading_open":
            level = int(tk.tag[1])
            txt = _inline(tokens[i + 1]) if i + 1 < n else ""
            flow.append(Paragraph(txt, hstyles.get(min(level, 3), hstyles[3])))
            i += 3
            continue
        if tk.type == "paragraph_open":
            txt = _inline(tokens[i + 1]) if i + 1 < n else ""
            if txt:
                flow.append(Paragraph(txt, body))
            i += 3
            continue
        if tk.type == "hr":
            flow.append(HRFlowable(width="100%", thickness=0.5, color=line, spaceBefore=6, spaceAfter=6))
            i += 1
            continue
        if tk.type in ("bullet_list_open", "ordered_list_open"):
            ordered = tk.type == "ordered_list_open"
            items: list[Any] = []
            j = i + 1
            depth = 1
            while j < n and depth > 0:
                if tokens[j].type in ("bullet_list_open", "ordered_list_open"):
                    depth += 1
                elif tokens[j].type in ("bullet_list_close", "ordered_list_close"):
                    depth -= 1
                elif tokens[j].type == "inline":
                    items.append(ListItem(Paragraph(_inline(tokens[j]), body), leftIndent=12))
                j += 1
            flow.append(ListFlowable(items, bulletType="1" if ordered else "bullet", start="1" if ordered else None, leftIndent=14))
            i = j
            continue
        if tk.type == "table_open":
            rows: list[list[Any]] = []
            header_rows = 0
            j = i + 1
            cur: list[Any] = []
            in_head = False
            while j < n and tokens[j].type != "table_close":
                tj = tokens[j].type
                if tj == "thead_open":
                    in_head = True
                elif tj == "thead_close":
                    in_head = False
                elif tj == "tr_open":
                    cur = []
                elif tj == "tr_close":
                    rows.append(cur)
                    if in_head:
                        header_rows += 1
                elif tj in ("th_open", "td_open"):
                    inline_tok = tokens[j + 1] if j + 1 < n and tokens[j + 1].type == "inline" else None
                    style = cellh if tj == "th_open" else cell
                    cur.append(Paragraph(_inline(inline_tok) if inline_tok else "", style))
                j += 1
            if rows:
                ncols = max(len(r) for r in rows)
                rows = [r + [Paragraph("", cell)] * (ncols - len(r)) for r in rows]
                avail = 210 * mm - 32 * mm
                tbl = Table(rows, colWidths=[avail / ncols] * ncols, repeatRows=header_rows or 1)
                tstyle = [
                    ("GRID", (0, 0), (-1, -1), 0.5, line),
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("TOPPADDING", (0, 0), (-1, -1), 4),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                    ("LEFTPADDING", (0, 0), (-1, -1), 6),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                ]
                if header_rows:
                    tstyle.append(("BACKGROUND", (0, 0), (-1, header_rows - 1), brand))
                    if len(rows) > header_rows:
                        tstyle.append(("ROWBACKGROUNDS", (0, header_rows), (-1, -1), [colors.white, colors.HexColor(_ZEBRA)]))
                tbl.setStyle(TableStyle(tstyle))
                flow.append(Spacer(1, 4))
                flow.append(tbl)
                flow.append(Spacer(1, 6))
            i = j + 1
            continue
        i += 1

    if not flow or (title and len(flow) <= 2):
        flow.append(Paragraph(_esc(content.strip()), body))

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4, topMargin=18 * mm, bottomMargin=16 * mm,
        leftMargin=16 * mm, rightMargin=16 * mm, title=title or "Report",
    )
    doc.build(flow)
    return buf.getvalue()


def _render_plain_pdf(title: str, content: str) -> bytes:
    import fitz  # pymupdf

    doc = fitz.open()
    page = doc.new_page()
    rect = page.rect
    margin = 50
    text_rect = fitz.Rect(margin, margin, rect.width - margin, rect.height - margin)
    header = f"{title.strip()}\n\n" if (title or "").strip() else ""
    page.insert_textbox(text_rect, f"{header}{content}", fontsize=11, fontname="helv", align=fitz.TEXT_ALIGN_LEFT)
    pdf_bytes = doc.tobytes()
    doc.close()
    return pdf_bytes


def render_pdf_bytes(*, title: str, content: str) -> bytes:
    """Render markdown (or plain) report text into a styled PDF document."""
    text = (content or "").strip()
    if not text:
        raise ValueError("empty_content")
    try:
        return _render_markdown_pdf(title, text)
    except Exception:  # noqa: BLE001 — never fail a download; degrade to plain
        return _render_plain_pdf(title, text)


def render_pdf_result(*, title: str, content: str) -> dict[str, Any]:
    """Return the action-runner shaped envelope for ``pdf_generator`` nodes."""
    try:
        pdf_bytes = render_pdf_bytes(title=title, content=content)
    except ValueError:
        return {
            "__provider__": "pdf_generator",
            "__action__": "create_pdf",
            "__dry_run__": True,
            "__reason__": "empty_content",
            "data": {
                "ok": False,
                "note": "PDF not generated — no content was provided upstream.",
            },
        }

    filename = _safe_filename(title or "Report")
    return {
        "__provider__": "pdf_generator",
        "__action__": "create_pdf",
        "__dry_run__": False,
        "data": {
            "ok": True,
            "filename": filename,
            "content_type": "application/pdf",
            "pdf_base64": base64.b64encode(pdf_bytes).decode("ascii"),
            "size_bytes": len(pdf_bytes),
        },
    }


__all__ = ["render_pdf_bytes", "render_pdf_result", "_safe_filename"]
