#!/usr/bin/env python3
"""
extract_invoices.py — HDS Marketing AI Workflow Architect homework
Adam Reep · adamjreep@gmail.com · 2026-05-27

Lightweight, functional invoice extractor.

Ingests a folder of PDF invoices, extracts the five requested fields via the
Anthropic Claude API, and emits a single JSON file shaped like the body of a
POST request to the Antera API.

Design overview
---------------
* PDFs may be text-based or scanned images.
* Strategy: try fast text extraction (pypdfium2) first. If a page returns
  meaningful text (>= MIN_TEXT_CHARS), send the text to Claude. Otherwise
  render the page to a PNG and send the image to Claude (vision). Same
  schema-prompted prompt either way — one code path, two input formats.
* Each PDF page is treated as one invoice unless the page is obviously
  blank (in which case we skip and log).
* Hallucination control: low temperature, structured tool-use schema with
  required fields and explicit nullable typing, "extract only what you can
  read on the document" instruction, no chain-of-thought encouraged.
* Illegible-page handling: explicit error record with reason_code, never a
  fabricated value. See README for the failure modes and recovery story.

Usage
-----
    pip install anthropic pypdfium2 pillow
    export ANTHROPIC_API_KEY=sk-ant-...
    python extract_invoices.py --input invoices_raw/ --output invoices.json

Folder layout expected:
    invoices_raw/
        AP60578.pdf
        OBP_3Invoices_HDS_5-13-26.pdf
        S&S_Activewear_Invoices_5-18-2026.pdf
        invoices.pdf
        ...
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import logging
import os
import sys
import time
from pathlib import Path

import pypdfium2 as pdfium
from anthropic import Anthropic
from PIL import Image

# ---- configuration ----------------------------------------------------------

MODEL = "claude-haiku-4-5-20251001"  # fast + cheap; sufficient for structured invoice extraction
MAX_TOKENS = 2048
TEMPERATURE = 0.0                   # deterministic extraction; reproducible runs
MIN_TEXT_CHARS = 200                # below this we fall through to vision
RENDER_DPI = 200                    # PDF -> PNG at 200 DPI; quality/cost balance
RETRY_LIMIT = 2                     # transient API error retries

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("extract_invoices")

# ---- extraction schema ------------------------------------------------------
#
# Defined via Anthropic's tool-use schema. Forcing the model to call this tool
# is how we make the output reliably parseable and constrain fields to the
# expected types. This is the strongest single anti-hallucination control: the
# model cannot return free-form text, only a structured payload matching the
# schema. Missing fields are explicitly nullable; line items are an array with
# typed inner objects.

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
            "invoice_date":     {"type": ["string", "null"], "description": "ISO 8601 YYYY-MM-DD if determinable; otherwise the date string as printed."},
            "invoice_number":   {"type": ["string", "null"]},
            "po_number":        {"type": ["string", "null"]},
            "total_amount_due": {"type": ["number", "null"]},
            "currency":         {"type": ["string", "null"], "description": "ISO 4217 currency code; default 'USD' if dollar sign present and no other indicator."},
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
            "extraction_confidence": {
                "type": "string",
                "enum": ["high", "medium", "low"],
                "description": "high = all required fields cleanly visible; medium = some inference required; low = significant fields illegible or missing; suggest human review.",
            },
            "notes": {
                "type": ["string", "null"],
                "description": "Brief free-text note explaining any low-confidence fields or extraction caveats. Keep to one short sentence.",
            },
        },
        "required": [
            "vendor_name", "invoice_date", "invoice_number", "po_number",
            "total_amount_due", "currency", "line_items",
            "extraction_confidence", "notes",
        ],
        "additionalProperties": False,
    },
}

# ---- prompt -----------------------------------------------------------------

SYSTEM_PROMPT = (
    "You are an invoice extraction service. You receive a single invoice "
    "document (either text already extracted from a PDF, or an image of the "
    "invoice page) and must return one structured record by calling the "
    "record_invoice tool exactly once. "
    "Extract ONLY values that are clearly visible on the document. "
    "Never invent, infer, or compute values that are not printed. "
    "If a value is unreadable or absent, set the field to null. "
    "If the document is not legible enough to identify the vendor and total, "
    "set extraction_confidence to 'low' and explain in notes. "
    "Currency: if a dollar sign is present and no other currency indicator, "
    "use 'USD'. "
    "For vendor_name: identify the company SENDING the invoice (the party owed "
    "payment). Look at the letterhead, logo, \"Remit To:\" address, or domain "
    "in URLs/footer — NOT the bill-to or ship-to recipient. If you see HDS "
    "Marketing or any HDS-affiliated entity, that is the customer; identify "
    "the vendor billing them instead. "
    "For line items: include EVERY line that has a dollar amount or quantity, "
    "preserving the order on the invoice. Skip header/total rows."
)

USER_TEXT_TEMPLATE = (
    "Extract the invoice from the following document text and call the "
    "record_invoice tool with the result.\n\n"
    "--- BEGIN DOCUMENT TEXT ---\n{text}\n--- END DOCUMENT TEXT ---"
)
USER_IMAGE_PROMPT = (
    "Extract the invoice from the attached image and call the record_invoice "
    "tool with the result."
)

# ---- helpers ----------------------------------------------------------------

def page_text(page: pdfium.PdfPage) -> str:
    return (page.get_textpage().get_text_range() or "").strip()


def page_image_b64(page: pdfium.PdfPage, dpi: int = RENDER_DPI) -> str:
    scale = dpi / 72  # pdfium default is 72 DPI
    pil = page.render(scale=scale).to_pil()
    # Cap longest side at 1568px to stay inside Claude's vision sweet spot
    longest = max(pil.size)
    if longest > 1568:
        ratio = 1568 / longest
        pil = pil.resize((int(pil.size[0] * ratio), int(pil.size[1] * ratio)), Image.LANCZOS)
    buf = io.BytesIO()
    pil.save(buf, format="PNG", optimize=True)
    return base64.standard_b64encode(buf.getvalue()).decode("ascii")


def call_claude(client: Anthropic, content: list[dict]) -> dict:
    """Call Claude with tool-use forced, parse the resulting record_invoice payload."""
    last_err: Exception | None = None
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


def error_record(source_file: str, page_number: int, reason_code: str, message: str) -> dict:
    """Used when a page is unreadable. Never fabricate fields — surface the failure."""
    return {
        "source_file": source_file,
        "page_number": page_number,
        "extraction_status": "error",
        "reason_code": reason_code,
        "message": message,
        "vendor_name": None,
        "invoice_date": None,
        "invoice_number": None,
        "po_number": None,
        "total_amount_due": None,
        "currency": None,
        "line_items": [],
        "extraction_confidence": "low",
        "notes": message,
    }


def extract_one_page(client: Anthropic, pdf_path: Path, page_index: int, page: pdfium.PdfPage) -> dict:
    source_file = pdf_path.name
    page_number = page_index + 1

    text = page_text(page)
    used_path = "text" if len(text) >= MIN_TEXT_CHARS else "vision"

    try:
        # Always include the rendered image — pypdfium reading order can scramble
        # bill-to vs vendor when the bill-to address sits above the letterhead.
        # The image preserves visual hierarchy (logo / letterhead at top).
        img_b64 = page_image_b64(page)
        if used_path == "text":
            content = [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": img_b64}},
                {"type": "text", "text": (
                    "Extract the invoice. The image shows the full page layout; "
                    "the text below is the same page parsed by a text extractor "
                    "(reading order may be scrambled — defer to the image for "
                    "vendor identification and document layout). Call the "
                    "record_invoice tool with the result.\n\n"
                    "--- EXTRACTED TEXT (may be reordered) ---\n" + text + "\n--- END ---"
                )},
            ]
            used_path = "text+vision"
        else:
            content = [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": img_b64}},
                {"type": "text", "text": USER_IMAGE_PROMPT},
            ]
        record = call_claude(client, content)
    except Exception as e:
        log.error("  page %d: extraction failed (%s)", page_number, e)
        return error_record(source_file, page_number, "extraction_error", str(e))

    # Detect "Claude saw nothing useful" by checking required fields
    if not record.get("vendor_name") and record.get("total_amount_due") is None:
        record = error_record(
            source_file, page_number, "illegible_page",
            "Vendor and total both unreadable; flagged for human review.",
        )
    else:
        record.update({
            "source_file": source_file,
            "page_number": page_number,
            "extraction_status": "ok",
            "extraction_path": used_path,
        })
    return record


def antera_post_body(invoices: list[dict]) -> dict:
    """Wrap the invoice array in the shape Antera's vendor-bill ingest endpoint expects."""
    return {
        "endpoint": "POST /api/v1/vendor-bills/batch",
        "submitted_by": "adamjreep@gmail.com",
        "submitted_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": "extract_invoices.py v1.0",
        "review_required": any(
            inv.get("extraction_status") != "ok" or inv.get("extraction_confidence") == "low"
            for inv in invoices
        ),
        "invoice_count": len(invoices),
        "invoices": invoices,
    }


def process_pdf(client: Anthropic, pdf_path: Path) -> list[dict]:
    log.info("processing %s", pdf_path.name)
    try:
        pdf = pdfium.PdfDocument(str(pdf_path))
    except Exception as e:
        log.error("  could not open: %s", e)
        return [error_record(pdf_path.name, 0, "open_failed", str(e))]

    out: list[dict] = []
    for i, page in enumerate(pdf):
        log.info("  page %d/%d", i + 1, len(pdf))
        out.append(extract_one_page(client, pdf_path, i, page))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", "-i", default="invoices_raw", help="Folder containing PDF invoices.")
    ap.add_argument("--output", "-o", default="invoices.json", help="Output JSON path.")
    ap.add_argument("--limit", type=int, default=None, help="Optional cap on PDFs processed (debug).")
    args = ap.parse_args()

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: set ANTHROPIC_API_KEY in your environment.", file=sys.stderr)
        return 2

    input_dir = Path(args.input)
    if not input_dir.is_dir():
        print(f"ERROR: input folder not found: {input_dir}", file=sys.stderr)
        return 2

    pdfs = sorted(input_dir.glob("*.pdf"))
    if args.limit:
        pdfs = pdfs[: args.limit]
    if not pdfs:
        print(f"ERROR: no .pdf files in {input_dir}", file=sys.stderr)
        return 2

    log.info("found %d PDF(s) in %s", len(pdfs), input_dir)
    client = Anthropic(api_key=api_key)

    all_invoices: list[dict] = []
    for pdf_path in pdfs:
        all_invoices.extend(process_pdf(client, pdf_path))

    payload = antera_post_body(all_invoices)
    Path(args.output).write_text(json.dumps(payload, indent=2, default=str))
    log.info("wrote %d invoice record(s) to %s", len(all_invoices), args.output)
    log.info("review_required = %s", payload["review_required"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
