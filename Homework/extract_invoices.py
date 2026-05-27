#!/usr/bin/env python3
"""
extract_invoices.py — HDS Marketing AI Workflow Architect homework
Adam Reep · adamjreep@gmail.com · 2026-05-27

Lightweight, functional invoice extractor.

Ingests a folder of PDF invoices, extracts the five requested fields via the
Anthropic Claude API, and emits a single JSON file shaped like a clean
vendor-bill schema with a sidecar audit block.

A note on Antera shape: the public Antera v1 spec (api.anterasaas.com) does
not expose a vendor-bill / AP endpoint — its documented endpoints are
/accounts, /contacts, /orders, /artwork, /products. So this script emits a
generic, well-typed vendor-bill payload + a separate audit block. A
production deployment would add a thin adapter that maps `invoices[]` to
whichever Antera endpoint HDS actually uses for AP intake (possibly an
internal/extended endpoint, possibly a Sheets review buffer, possibly an
orders payload with a vendor-bill type).

Strategy
--------
* PDFs may be text-based or scanned images.
* Every page is sent to Claude as BOTH extracted text AND a rendered image.
  Text extraction can scramble reading order (bill-to ends up above the
  vendor letterhead); the image preserves visual hierarchy.
* Each PDF page is treated as one invoice. .eml files are not natively
  supported here — the Richardson Sports edge case (link to vendor portal)
  is handled via a pre-processed text input demonstrating the failure-mode
  path; production would extend the script to walk .eml attachments and
  fetch public URLs.
* Hallucination control: forced tool-use with typed schema, temperature 0,
  "extract only what you see" instruction, and a post-call subtotal
  cross-validation that catches arithmetic divergence between line items
  and the stated total.
* Vendor canonicalization: a small embedded vendor master maps observed
  variants ("HIT Promotional Products, Inc." / "Hit Promo Products, Inc.")
  to a single canonical name. Unknown vendors are emitted to an exceptions
  list inside the audit block rather than being silently passed through.

Usage
-----
    pip install anthropic pypdfium2 pillow
    export ANTHROPIC_API_KEY=sk-ant-...
    python extract_invoices.py --input invoices_raw/ --output invoices.json
"""
from __future__ import annotations

import argparse, base64, io, json, logging, os, re, sys, time, uuid
from pathlib import Path

import pypdfium2 as pdfium
from anthropic import Anthropic
from PIL import Image

# ---- configuration ----------------------------------------------------------

MODEL          = "claude-haiku-4-5-20251001"
MAX_TOKENS     = 2048
TEMPERATURE    = 0.0
MIN_TEXT_CHARS = 200
RENDER_DPI     = 200
RETRY_LIMIT    = 2
SUBTOTAL_TOLERANCE = 0.01   # 1% — accommodates rounding & line-item taxes

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("extract_invoices")

# ---- vendor master (canonicalization) --------------------------------------
# Production extension: this lives in a Sheets-backed master or vendor-master
# table in Antera, with fuzzy matching + an exceptions queue. For the
# homework, we hard-code the five vendors observed in the test batch.

VENDOR_MASTER = [
    {"canonical": "Angel Printing & Reproduction Inc.", "aliases": ["angel printing"]},
    {"canonical": "Hit Promotional Products, Inc.",     "aliases": ["hit promotional", "hit promo"]},
    {"canonical": "S&S Activewear",                     "aliases": ["s s activewear", "ss activewear"]},
    {"canonical": "Old Brooklyn Printing",              "aliases": ["old brooklyn"]},
    {"canonical": "Richardson Sports",                  "aliases": ["richardson sports", "richardson"]},
]

def canonicalize_vendor(name: str | None) -> tuple[str | None, bool, bool]:
    """Returns (canonical_name, was_renamed, matched_in_master).
    matched_in_master is True whenever the raw name matched any alias,
    regardless of whether canonical form differs from the raw input."""
    if not name: return None, False, False
    n = re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()
    for v in VENDOR_MASTER:
        if any(a in n for a in v["aliases"]):
            return v["canonical"], (v["canonical"] != name), True
    return name, False, False

# ---- extraction schema (tool-use) ------------------------------------------

INVOICE_TOOL = {
    "name": "record_invoice",
    "description": (
        "Record one extracted invoice. Use null for any field that is not "
        "clearly visible on the document. Do not infer or fabricate values. "
        "Use the line_items array exactly as printed; if a field for a line "
        "item is not present, set it to null."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "vendor_name":      {"type": ["string", "null"]},
            "invoice_date":     {"type": ["string", "null"]},
            "invoice_number":   {"type": ["string", "null"]},
            "po_number":        {"type": ["string", "null"]},
            "total_amount_due": {"type": ["number", "null"]},
            "currency":         {"type": ["string", "null"]},
            "line_items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "item_name":  {"type": ["string", "null"]},
                        "quantity":   {"type": ["number", "null"]},
                        "unit_price": {"type": ["number", "null"]},
                    },
                    "required": ["item_name", "quantity", "unit_price"],
                    "additionalProperties": False,
                },
            },
            "extraction_confidence": {"type": "string", "enum": ["high", "medium", "low"]},
            "notes": {"type": ["string", "null"]},
        },
        "required": ["vendor_name", "invoice_date", "invoice_number", "po_number",
                     "total_amount_due", "currency", "line_items",
                     "extraction_confidence", "notes"],
        "additionalProperties": False,
    },
}

SYSTEM_PROMPT = (
    "You are an invoice extraction service. You receive a single invoice "
    "document (rendered image plus optionally the same page's extracted "
    "text) and must return one structured record by calling the "
    "record_invoice tool exactly once. "
    "Extract ONLY values clearly visible on the document. "
    "Never invent, infer, or compute values that are not printed. "
    "If a value is unreadable or absent, set the field to null. "
    "If the document is not legible enough to identify both the vendor "
    "and total, set extraction_confidence to 'low' and explain in notes. "
    "For vendor_name: identify the company SENDING the invoice (the party "
    "owed payment). Look at letterhead, logo, 'Remit To:' address, or "
    "domain in URLs/footer — NOT the bill-to or ship-to recipient. "
    "If you see HDS Marketing or any HDS-affiliated entity in the addresses, "
    "that is the customer; identify the vendor billing them instead. "
    "Currency: if a dollar sign is present and no other indicator, use 'USD'. "
    "For line items: include EVERY line with a dollar amount or quantity, "
    "preserving the order on the invoice. Skip header/total rows."
)

USER_TEXT_TEMPLATE = (
    "Extract the invoice. The image shows the full page layout; the text "
    "below is the same page parsed by a text extractor (reading order may "
    "be scrambled — defer to the image for vendor identification and "
    "document layout). Call the record_invoice tool with the result.\n\n"
    "--- EXTRACTED TEXT (may be reordered) ---\n{text}\n--- END ---"
)
USER_IMAGE_PROMPT = (
    "Extract the invoice from the attached image and call the record_invoice "
    "tool with the result."
)

# ---- helpers ----------------------------------------------------------------

def page_text(page: pdfium.PdfPage) -> str:
    return (page.get_textpage().get_text_range() or "").strip()

def page_image_b64(page: pdfium.PdfPage, dpi: int = RENDER_DPI) -> str:
    pil = page.render(scale=dpi / 72).to_pil()
    if max(pil.size) > 1568:
        ratio = 1568 / max(pil.size)
        pil = pil.resize((int(pil.size[0] * ratio), int(pil.size[1] * ratio)), Image.LANCZOS)
    buf = io.BytesIO()
    pil.save(buf, format="PNG", optimize=True)
    return base64.standard_b64encode(buf.getvalue()).decode("ascii")

def call_claude(client: Anthropic, content: list[dict]) -> dict:
    last_err = None
    for attempt in range(1, RETRY_LIMIT + 2):
        try:
            resp = client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                temperature=TEMPERATURE,
                system=SYSTEM_PROMPT,
                tools=[INVOICE_TOOL],
                tool_choice={"type": "tool", "name": "record_invoice"},
                messages=[{"role": "user", "content": content}],
            )
            for block in resp.content:
                if getattr(block, "type", None) == "tool_use" and block.name == "record_invoice":
                    return dict(block.input)
            raise RuntimeError("Claude did not return a record_invoice tool call")
        except Exception as e:
            last_err = e
            wait = 2 ** attempt
            log.warning("Claude call failed (attempt %d): %s — retrying in %ds", attempt, e, wait)
            time.sleep(wait)
    raise RuntimeError(f"Claude call failed after {RETRY_LIMIT + 1} attempts: {last_err}")

def validate_subtotal(record: dict) -> tuple[str, str | None]:
    """Cross-validate sum(qty * unit_price) against total_amount_due.
    Catches arithmetic hallucinations the model doesn't notice itself.
    Returns (status, note_or_none). Status is one of: passed, failed, skipped."""
    total = record.get("total_amount_due")
    line_items = record.get("line_items") or []
    if total is None or not line_items:
        return "skipped", None
    line_sum = 0.0
    for li in line_items:
        q, p = li.get("quantity"), li.get("unit_price")
        if q is None or p is None:
            return "skipped", "one or more line items missing quantity or unit_price"
        line_sum += q * p
    if total == 0:
        return "skipped", "total is zero"
    diff_pct = abs(line_sum - total) / abs(total)
    if diff_pct > SUBTOTAL_TOLERANCE:
        return "failed", f"line-sum {line_sum:.2f} vs total {total:.2f} ({diff_pct*100:.1f}% divergence)"
    return "passed", None

def split_invoice_and_audit(record: dict, source_file: str, page_number: int,
                            extraction_path: str, status: str) -> tuple[dict | None, dict]:
    """Splits the LLM record into (clean_invoice_payload, audit_record).
    Returns (None, audit) for error cases so the audit alone surfaces them."""
    # Canonicalize the vendor
    raw_vendor = record.get("vendor_name")
    canonical, was_renamed, vendor_matched = canonicalize_vendor(raw_vendor)

    # Cross-validate subtotals
    subtotal_status, subtotal_note = validate_subtotal(record)
    confidence = record.get("extraction_confidence", "low")
    if subtotal_status == "failed":
        confidence = "medium" if confidence == "high" else confidence
        record["notes"] = (record.get("notes") or "") + " | subtotal_check: " + (subtotal_note or "")

    audit = {
        "source_file": source_file,
        "page_number": page_number,
        "extraction_status": status,
        "extraction_path": extraction_path,
        "extraction_confidence": confidence,
        "vendor_canonicalized": was_renamed,
        "vendor_canonical_name": canonical,
        "vendor_raw_name": raw_vendor,
        "vendor_known": vendor_matched,
        "subtotal_check": subtotal_status,
        "subtotal_note": subtotal_note,
        "notes": record.get("notes"),
    }

    if status != "ok":
        return None, audit

    # Clean vendor-bill payload (no implementation details)
    invoice = {
        "vendor": {"name": canonical or raw_vendor},
        "invoice_date":     record.get("invoice_date"),
        "invoice_number":   record.get("invoice_number"),
        "po_number":        record.get("po_number"),
        "total_amount_due": record.get("total_amount_due"),
        "currency":         record.get("currency") or "USD",
        "line_items": [
            {
                "item_name":  li.get("item_name"),
                "quantity":   li.get("quantity"),
                "unit_price": li.get("unit_price"),
            }
            for li in (record.get("line_items") or [])
        ],
    }
    return invoice, audit

def error_audit(source_file: str, page_number: int, reason_code: str, message: str) -> dict:
    return {
        "source_file": source_file,
        "page_number": page_number,
        "extraction_status": "error",
        "reason_code": reason_code,
        "extraction_confidence": "low",
        "notes": message,
    }

def extract_one_page(client: Anthropic, pdf_path: Path, page_index: int, page: pdfium.PdfPage):
    source_file = pdf_path.name
    page_number = page_index + 1
    text = page_text(page)
    used_path = "text+vision" if len(text) >= MIN_TEXT_CHARS else "vision"
    try:
        img_b64 = page_image_b64(page)
        if used_path == "text+vision":
            content = [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": img_b64}},
                {"type": "text",  "text": USER_TEXT_TEMPLATE.format(text=text)},
            ]
        else:
            content = [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": img_b64}},
                {"type": "text",  "text": USER_IMAGE_PROMPT},
            ]
        record = call_claude(client, content)
    except Exception as e:
        log.error("  page %d: %s", page_number, e)
        return None, error_audit(source_file, page_number, "extraction_error", str(e))

    if not record.get("vendor_name") and record.get("total_amount_due") is None:
        return None, error_audit(source_file, page_number, "illegible_page",
                                 "Vendor and total both unreadable; flagged for human review.")
    return split_invoice_and_audit(record, source_file, page_number, used_path, "ok")

def process_pdf(client: Anthropic, pdf_path: Path):
    log.info("processing %s", pdf_path.name)
    invoices, audits = [], []
    try:
        pdf = pdfium.PdfDocument(str(pdf_path))
    except Exception as e:
        log.error("  could not open: %s", e)
        audits.append(error_audit(pdf_path.name, 0, "open_failed", str(e)))
        return invoices, audits
    for i, page in enumerate(pdf):
        log.info("  page %d/%d", i + 1, len(pdf))
        inv, aud = extract_one_page(client, pdf_path, i, page)
        if inv: invoices.append(inv)
        audits.append(aud)
    return invoices, audits

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", "-i", default="invoices_raw")
    ap.add_argument("--output", "-o", default="invoices.json")
    args = ap.parse_args()

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: set ANTHROPIC_API_KEY in your environment.", file=sys.stderr)
        return 2
    input_dir = Path(args.input)
    if not input_dir.is_dir():
        print(f"ERROR: input folder not found: {input_dir}", file=sys.stderr); return 2
    pdfs = sorted(input_dir.glob("*.pdf"))
    if not pdfs:
        print(f"ERROR: no .pdf files in {input_dir}", file=sys.stderr); return 2

    log.info("found %d PDF(s) in %s", len(pdfs), input_dir)
    client = Anthropic(api_key=api_key)
    all_invoices, all_audits = [], []
    for pdf_path in pdfs:
        inv, aud = process_pdf(client, pdf_path)
        all_invoices.extend(inv); all_audits.extend(aud)

    # Build the two-block payload
    review_required = any(
        a.get("extraction_status") != "ok"
        or a.get("extraction_confidence") in ("low", "medium")
        or a.get("subtotal_check") == "failed"
        or not a.get("vendor_known")
        for a in all_audits
    )
    unknown_vendors = sorted({
        a.get("vendor_raw_name") for a in all_audits
        if a.get("extraction_status") == "ok" and not a.get("vendor_known")
    } - {None})

    payload = {
        "batch_id": time.strftime("%Y%m%d-%H%M%S-utc", time.gmtime()),
        "submitted_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "invoices": all_invoices,
        "_audit": {
            "extractor": "extract_invoices.py v1.0",
            "llm_model": MODEL,
            "antera_target_endpoint": "TBD per HDS deployment (no public AP endpoint in Antera v1 spec)",
            "review_required": review_required,
            "invoice_count": len(all_invoices),
            "unknown_vendors": unknown_vendors,
            "records": all_audits,
        },
    }
    Path(args.output).write_text(json.dumps(payload, indent=2, default=str))
    log.info("wrote %d invoice records to %s", len(all_invoices), args.output)
    log.info("review_required = %s | unknown_vendors = %s", review_required, unknown_vendors)
    return 0

if __name__ == "__main__":
    sys.exit(main())
