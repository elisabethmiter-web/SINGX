"""PDF engine: normalize uploads, render page previews, flatten signed fields
onto the pages, and append a Certificate of Completion.

Field coordinates are fractions of the page (0..1) measured from the top-left,
so they mean the same thing in the browser preview and on the PDF.
"""
import hashlib
import io
import os
import threading
from datetime import datetime

import pypdfium2 as pdfium
from pypdf import PdfReader, PdfWriter
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from xml.sax.saxutils import escape

INK = colors.HexColor("#1d2433")
MUTED = colors.HexColor("#5b6475")
RULE = colors.HexColor("#d5d9e2")
ACCENT = colors.HexColor("#24408e")
SIG_INK = colors.HexColor("#1b2a5c")

PREVIEW_SCALE = 1.6  # ~115 dpi; sharp enough for reading on screen
_PDFIUM_LOCK = threading.Lock()  # pdfium is not thread-safe


class PdfError(Exception):
    pass


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


# ------------------------------------------------------------------ upload
def normalize_upload(src_path, out_path):
    """Validate a PDF and rewrite it with page rotation baked into the content,
    so that preview images and stamped fields share one upright coordinate space.
    Returns a list of page sizes [{w, h}] in points."""
    try:
        reader = PdfReader(src_path)
    except Exception as exc:  # noqa: BLE001
        raise PdfError("That file couldn't be read as a PDF.") from exc
    if reader.is_encrypted:
        try:
            if not reader.decrypt(""):
                raise PdfError("That PDF is password-protected. Remove the password and upload it again.")
        except Exception as exc:  # noqa: BLE001
            raise PdfError("That PDF is password-protected. Remove the password and upload it again.") from exc
    if len(reader.pages) == 0:
        raise PdfError("That PDF has no pages.")
    if len(reader.pages) > 200:
        raise PdfError("PDFs are limited to 200 pages.")
    writer = PdfWriter()
    sizes = []
    for page in reader.pages:
        if page.rotation:
            page.transfer_rotation_to_content()
        # Use the crop box as the visible page and make media box match it.
        cb = page.cropbox
        page.mediabox.lower_left = cb.lower_left
        page.mediabox.upper_right = cb.upper_right
        writer.add_page(page)
        sizes.append({"w": round(float(cb.width), 2), "h": round(float(cb.height), 2)})
    # Drop interactive form fields: they'd float above our stamped values.
    if "/AcroForm" in writer._root_object:
        del writer._root_object["/AcroForm"]
    for page in writer.pages:
        if "/Annots" in page:
            annots = [a for a in page["/Annots"] if a.get_object().get("/Subtype") != "/Widget"]
            if annots:
                page[pdf_name("/Annots")] = _array(annots)
            else:
                del page["/Annots"]
    with open(out_path, "wb") as fh:
        writer.write(fh)
    return sizes


def pdf_name(n):
    from pypdf.generic import NameObject
    return NameObject(n)


def _array(items):
    from pypdf.generic import ArrayObject
    return ArrayObject(items)


def render_pages(pdf_path, out_pattern, only=None):
    """Render pages to PNG files named out_pattern.format(n). Skips files that exist."""
    with _PDFIUM_LOCK:
        doc = pdfium.PdfDocument(pdf_path)
        try:
            for n in range(len(doc)):
                if only is not None and n != only:
                    continue
                out = out_pattern.format(n)
                if os.path.exists(out):
                    continue
                page = doc[n]
                img = page.render(scale=PREVIEW_SCALE, may_draw_forms=False).to_pil().convert("RGB")
                page.close()
                tmp = out + ".tmp"
                img.save(tmp, "PNG", optimize=True)
                os.replace(tmp, out)
        finally:
            doc.close()


# ------------------------------------------------------------------ flatten
def _fit_font(text, font, max_w, max_h, start=11):
    size = min(start, max_h * 0.72)
    while size > 5 and rl_canvas.Canvas(io.BytesIO()).stringWidth(text, font, size) > max_w:
        size -= 0.5
    return max(size, 5)


def _draw_image_in_box(c, img_path, x, y, w, h):
    img = ImageReader(img_path)
    iw, ih = img.getSize()
    scale = min(w / iw, h / ih)
    dw, dh = iw * scale, ih * scale
    c.drawImage(img, x, y + (h - dh) / 2, dw, dh, mask="auto")


def _overlay(width, height, page_fields, envelope_ref):
    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=(width, height))
    c.setFont("Helvetica", 6.5)
    c.setFillColor(MUTED)
    c.drawString(14, height - 12, f"Envelope ID: {envelope_ref}")
    for f in page_fields:
        x = f["x"] * width
        w = f["w"] * width
        h = f["h"] * height
        y = height - (f["y"] * height) - h
        t, val = f["type"], f.get("value")
        if t in ("signature", "initials"):
            if val and os.path.exists(val):
                _draw_image_in_box(c, val, x, y, w, h)
        elif t == "checkbox":
            if val in (True, "true", "on", "1"):
                c.setStrokeColor(SIG_INK)
                c.setLineWidth(max(1.2, h * 0.09))
                c.line(x + w * 0.2, y + h * 0.5, x + w * 0.42, y + h * 0.22)
                c.line(x + w * 0.42, y + h * 0.22, x + w * 0.82, y + h * 0.8)
        else:
            text = str(val or "")
            if not text:
                continue
            font = "Helvetica"
            size = _fit_font(text, font, w - 2, h)
            c.setFont(font, size)
            c.setFillColor(SIG_INK)
            c.drawString(x + 1, y + (h - size) / 2 + size * 0.18, text)
    c.save()
    buf.seek(0)
    return PdfReader(buf).pages[0]


# ------------------------------------------------------------------ certificate
H1 = ParagraphStyle("h1", fontName="Helvetica-Bold", fontSize=16, leading=20, textColor=INK)
H2 = ParagraphStyle("h2", fontName="Helvetica-Bold", fontSize=10.5, leading=14, textColor=INK, spaceBefore=12, spaceAfter=4)
SMALL = ParagraphStyle("s", fontName="Helvetica", fontSize=8, leading=10.5, textColor=MUTED)
LABEL = ParagraphStyle("l", fontName="Helvetica", fontSize=7.5, leading=10, textColor=MUTED)
VALUE = ParagraphStyle("v", fontName="Helvetica", fontSize=8.8, leading=11.5, textColor=INK)
MONO = ParagraphStyle("m", fontName="Courier", fontSize=7, leading=9, textColor=INK)


def _p(t, s=VALUE):
    return Paragraph(escape(str(t if t is not None else "")).replace("\n", "<br/>"), s)


def _when(iso):
    if not iso:
        return "—"
    try:
        return datetime.fromisoformat(iso).strftime("%b %d, %Y %H:%M:%S UTC")
    except ValueError:
        return iso


def _tbl(rows, widths, header=True):
    t = Table(rows, colWidths=widths, repeatRows=1 if header else 0)
    style = [("VALIGN", (0, 0), (-1, -1), "TOP"), ("LINEBELOW", (0, 0), (-1, -1), 0.5, RULE),
             ("LEFTPADDING", (0, 0), (-1, -1), 0), ("TOPPADDING", (0, 0), (-1, -1), 4),
             ("BOTTOMPADDING", (0, 0), (-1, -1), 4)]
    t.setStyle(TableStyle(style))
    return t


def certificate(env, docs, recipients, events, business):
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter, leftMargin=0.75 * inch, rightMargin=0.75 * inch,
                            topMargin=0.75 * inch, bottomMargin=0.75 * inch,
                            title="Certificate of Completion", author=business)

    def frame(c, d):
        c.saveState()
        c.setFont("Helvetica", 7)
        c.setFillColor(MUTED)
        c.drawString(0.75 * inch, 0.45 * inch, f"{business} · Envelope ID {env['ref']}")
        c.drawRightString(letter[0] - 0.75 * inch, 0.45 * inch, f"Certificate page {d.page}")
        c.restoreState()

    story = [_p("Certificate of Completion", H1), Spacer(1, 4),
             _p(f"{business} · Envelope ID {env['ref']}", SMALL), Spacer(1, 6)]
    summary = [
        [_p("Subject", LABEL), _p(env["title"])],
        [_p("Status", LABEL), _p("Completed")],
        [_p("Sent", LABEL), _p(_when(env["sent_at"]))],
        [_p("Completed", LABEL), _p(_when(env["completed_at"]))],
        [_p("Documents", LABEL), _p(", ".join(f"{d['name']} ({len(d['pages'])} p.)" for d in docs))],
        [_p("Signers", LABEL), _p(str(sum(1 for r in recipients if r["kind"] == "signer")))],
    ]
    story.append(_tbl(summary, [1.4 * inch, 5.6 * inch], header=False))

    story.append(_p("Signer events", H2))
    rows = [[_p("Signer", LABEL), _p("Signature", LABEL), _p("Timestamps", LABEL)]]
    for r in recipients:
        if r["kind"] != "signer":
            continue
        sig = _p("—", VALUE)
        if r.get("signature_file") and os.path.exists(r["signature_file"]):
            img = ImageReader(r["signature_file"])
            iw, ih = img.getSize()
            h = 0.45 * inch
            w = min(1.8 * inch, h * iw / ih)
            h = w * ih / iw
            sig = Image(r["signature_file"], width=w, height=h)
        who = (f"{r['name']}\n{r['email']}\nRouting order {r['routing_order']}\n"
               f"IP: {r.get('ip') or '—'}\n"
               f"Adopted: {'drawn' if r.get('signature_kind') == 'draw' else 'typed'} signature")
        times = (f"Sent: {_when(r.get('sent_at'))}\nViewed: {_when(r.get('viewed_at'))}\n"
                 f"Consented: {_when(r.get('consented_at'))}\nSigned: {_when(r.get('signed_at'))}")
        rows.append([_p(who, SMALL), sig, _p(times, SMALL)])
    story.append(_tbl(rows, [2.5 * inch, 2.0 * inch, 2.5 * inch]))

    ccs = [r for r in recipients if r["kind"] == "cc"]
    if ccs:
        story.append(_p("Carbon copy recipients", H2))
        rows = [[_p("Recipient", LABEL), _p("Copy sent", LABEL)]]
        rows += [[_p(f"{r['name']} <{r['email']}>", SMALL), _p(_when(env["completed_at"]), SMALL)] for r in ccs]
        story.append(_tbl(rows, [4.5 * inch, 2.5 * inch]))

    story.append(_p("Document fingerprints (SHA-256 of each original upload)", H2))
    rows = [[_p("Document", LABEL), _p("SHA-256", LABEL)]]
    rows += [[_p(d["name"], SMALL), _p(d["sha256"], MONO)] for d in docs]
    story.append(_tbl(rows, [2.2 * inch, 4.8 * inch]))

    story.append(_p("Envelope history", H2))
    rows = [[_p("When", LABEL), _p("Event", LABEL), _p("IP", LABEL)]]
    for e in events:
        rows.append([_p(_when(e["at"]), SMALL), _p(e["text"], SMALL), _p(e.get("ip") or "", SMALL)])
    story.append(_tbl(rows, [1.8 * inch, 4.1 * inch, 1.1 * inch]))

    story += [Spacer(1, 10), _p(
        "Each signer agreed to use electronic records and signatures before signing. Signers were identified by "
        "access to a unique link sent to the email address listed above. The fingerprint of this completed file is "
        "recorded by the sender's system; anyone can check that a copy is unaltered on the sender's verification page.",
        SMALL)]
    doc.build(story, onFirstPage=frame, onLaterPages=frame)
    buf.seek(0)
    return buf


# ------------------------------------------------------------------ complete
def build_completed(out_path, env, docs, fields_by_doc, recipients, events, business, upload_dir):
    """docs: [{id, name, file, sha256, pages}], fields_by_doc: {doc_id: {page: [field+value]}}"""
    writer = PdfWriter()
    for d in docs:
        reader = PdfReader(os.path.join(upload_dir, d["file"]))
        for idx, page in enumerate(reader.pages):
            w, h = float(page.mediabox.width), float(page.mediabox.height)
            ox, oy = float(page.mediabox.left), float(page.mediabox.bottom)
            ov = _overlay(w, h, fields_by_doc.get(d["id"], {}).get(idx, []), env["ref"])
            if ox or oy:
                from pypdf import Transformation
                ov.add_transformation(Transformation().translate(ox, oy))
            page.merge_page(ov)
            writer.add_page(page)
    cert = PdfReader(certificate(env, docs, recipients, events, business))
    for page in cert.pages:
        writer.add_page(page)
    writer.add_metadata({"/Title": f"{env['title']} (completed)", "/Author": business,
                         "/Subject": f"Envelope ID {env['ref']}", "/Producer": "FormSign"})
    with open(out_path, "wb") as fh:
        writer.write(fh)
    return sha256_file(out_path)
