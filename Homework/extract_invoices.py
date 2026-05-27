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
  supported here — the R######### S##### edge case (link to vendor portal)
  is handled via a pre-processed text input demonstrating the failure-mode
  path; production would extend the script to walk .eml attachments and
  fetch public URLs.
* Hallucination control: forced tool-use with typed schema, temperature 0,
  "extract only what you see" instruction, and a post-call subtotal
  cross-validation that catches arithmetic divergence between line items
  and the stated total.
* Vendor canonicalization: a small embedded vendor master maps observed
  variants ("H## P########## P#######, Inc." / "H## P#### P#######, Inc.")
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
import urllib.request
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
    {
        "canonical": "A#### P####### & R########### Inc.",
        "aliases": ["a#### p#######"],
        "parsing_notes": (
            "A#### P####### INVOICES use a non-standard line format: the 'Unit Price' "
            "column on multi-quantity rows shows the LINE TOTAL for the entire quantity, "
            "not the per-unit price. The 'Amount' column matches the 'Unit Price' column. "
            "When you see A#### P####### as the vendor and a row where Unit Price * Quantity "
            ">> printed Total Due, compute unit_price = (Amount column value) / Quantity. "
            "Example: '500  Business Cards  $45.00 (UnitPrice)  $45.00 (Amount)' with "
            "Total Due $45.00 should yield quantity=500, unit_price=0.09."
        ),
    },
    {"canonical": "H## P########## P#######, Inc.", "aliases": ["h## p##########", "h## p####"]},
    {"canonical": "S&# A#########",                 "aliases": ["s ## a#########", "## a#########"]},
    {"canonical": "O## B####### P#######",          "aliases": ["o## b#######"]},
    {"canonical": "R######### S#####",              "aliases": ["r######### s#####", "r#########"]},
]


def get_vendor_parsing_notes(canonical_name: str | None) -> str | None:
    if not canonical_name: return None
    for v in VENDOR_MASTER:
        if v["canonical"] == canonical_name and v.get("parsing_notes"):
            return v["parsing_notes"]
    return None

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
    "preserving the order on the invoice. This includes adjustment lines like "
    "Freight, Shipping, Handling, Tax, Surcharge, and Discount — capture each as a "
    "separate line_item with item_name set to the adjustment label (e.g. \"Freight\") "
    "and the dollar amount as unit_price; use quantity 1 if not otherwise stated, "
    "and a negative unit_price for Discount lines. Skip pure header rows and any "
    "row that is solely a running subtotal or \"Total\" / \"Amount Due\" line."
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
        # _source_file and _page_number let us join audit -> invoice without ambiguity.
        # These get stripped before posting to Antera (downstream adapter removes underscore-prefixed keys).
        "_source_file":     source_file,
        "_page_number":     page_number,
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

    # If a known-quirky vendor is identified and the subtotal fails, re-extract once
    # with vendor-specific parsing notes prepended to the system prompt.
    sub_status, _sub_note = validate_subtotal(record)
    if sub_status == "failed":
        canonical, _, _ = canonicalize_vendor(record.get("vendor_name"))
        parsing_notes = get_vendor_parsing_notes(canonical)
        if parsing_notes:
            log.info("    re-extracting with vendor-specific parsing notes for %s", canonical)
            try:
                img_b64_2 = page_image_b64(page)
                content2 = [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": img_b64_2}},
                    {"type": "text",  "text": (
                        "VENDOR-SPECIFIC INSTRUCTION:\n" + parsing_notes +
                        "\n\nExtract the invoice with this guidance in mind."
                        + (USER_TEXT_TEMPLATE.format(text=text) if text else "")
                    )},
                ]
                record2 = call_claude(client, content2)
                # Use the re-extracted record only if its math is cleaner
                sub2_status, _ = validate_subtotal(record2)
                if sub2_status == "passed":
                    record = record2
                    record["notes"] = (record.get("notes") or "") + " | re-extracted with vendor parsing notes."
                    used_path = used_path + "+vendor_notes"
                    sub_status = "passed"
            except Exception as e:
                log.warning("    vendor-notes re-extract failed: %s", e)

    # If still failed: ask Claude to identify missing adjustments
    if sub_status == "failed":
        try:
            line_sum = sum((li.get("quantity") or 0) * (li.get("unit_price") or 0)
                           for li in (record.get("line_items") or []))
            delta = (record.get("total_amount_due") or 0) - line_sum
            recon = reconcile_subtotal(client, page, text, record.get("line_items") or [], record.get("total_amount_due") or 0, delta)
            # Only accept adjustments that have BOTH quantity and unit_price populated.
            # An adjustment with a null field is the model self-signalling "I am guessing."
            valid_adj = [a for a in (recon.get("adjustments") or [])
                         if a.get("quantity") is not None and a.get("unit_price") is not None]
            if len(valid_adj) != len(recon.get("adjustments") or []):
                log.info("    rejected %d fabricated adjustment(s) (null fields)",
                         len(recon.get("adjustments") or []) - len(valid_adj))
            if valid_adj:
                record["line_items"] = (record.get("line_items") or []) + valid_adj
                if recon.get("notes"):
                    record["notes"] = (record.get("notes") or "") + " | reconciled: " + recon["notes"]
                log.info("    reconciled %d adjustment(s) accepted", len(valid_adj))
        except Exception as e:
            log.warning("    reconcile pass failed: %s", e)

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



# ---- reconciliation: when subtotal fails, ask Claude to explain the delta -----

RECONCILE_TOOL = {
    "name": "reconcile_delta",
    "description": "List adjustment line items (freight, tax, shipping, discount, surcharge, handling) that reconcile the unaccounted delta between line-item subtotal and total. Return [] if the document shows no such adjustments.",
    "input_schema": {
        "type": "object",
        "properties": {
            "adjustments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "item_name": {"type": "string"},
                        "quantity":  {"type": ["number", "null"]},
                        "unit_price": {"type": ["number", "null"]},
                    },
                    "required": ["item_name", "quantity", "unit_price"],
                    "additionalProperties": False,
                },
            },
            "explained": {"type": "boolean", "description": "True if the adjustments fully explain the delta within $0.05 / 1%."},
            "notes": {"type": ["string", "null"]},
        },
        "required": ["adjustments", "explained", "notes"],
        "additionalProperties": False,
    },
}

def reconcile_subtotal(client: Anthropic, page, text: str, line_items: list, total: float, delta: float) -> dict:
    """Second Claude pass: hand it the doc and ask for the adjustments that explain the missing delta."""
    img_b64 = page_image_b64(page)
    extracted = ", ".join(f"{li.get('item_name','?')}: {li.get('quantity')} x {li.get('unit_price')}" for li in line_items)
    prompt = (
        f"You previously extracted these line items: [{extracted}]. They sum to "
        f"${sum((li.get('quantity') or 0) * (li.get('unit_price') or 0) for li in line_items):.2f}. "
        f"But the printed Total Amount Due on this invoice is ${total:.2f}, leaving a delta of "
        f"${delta:+.2f} unaccounted for. Look at the invoice (image + text below) for "
        f"adjustment lines that explain this delta — typically labelled Freight, Shipping, "
        f"Handling, Tax, Surcharge, Discount, or similar. Return those adjustments via the "
        f"reconcile_delta tool. Return [] if no such adjustments are visible."
    )
    content = [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": img_b64}},
        {"type": "text", "text": prompt + ("\n\n--- EXTRACTED TEXT ---\n" + text + "\n--- END ---" if text else "")},
    ]
    resp = client.messages.create(
        model=MODEL, max_tokens=1024, temperature=TEMPERATURE,
        system="You are an invoice reconciliation service. Look ONLY at the printed document to identify adjustments that explain the delta. Never fabricate.",
        tools=[RECONCILE_TOOL],
        tool_choice={"type": "tool", "name": "reconcile_delta"},
        messages=[{"role": "user", "content": content}],
    )
    for block in resp.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "reconcile_delta":
            return dict(block.input)
    return {"adjustments": [], "explained": False, "notes": "no tool call returned"}

# ---- email + url fetch helpers ---------------------------------------------

URL_RE = re.compile(r"https?://[^\s<>\"\']+")

def fetch_url(url: str, timeout: int = 12) -> tuple[bytes | None, str | None]:
    """Fetch a URL and return (bytes, content_type) or (None, None) on failure.
    Returns at most 5MB to keep prompt costs bounded."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "extract_invoices.py/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            ctype = resp.headers.get("Content-Type", "").split(";")[0].strip().lower()
            data = resp.read(5 * 1024 * 1024)
            return data, ctype
    except Exception as e:
        log.warning("URL fetch failed %s: %s", url, e)
        return None, None

def claude_extract_from_html(client: "Anthropic", html_text: str, source_label: str) -> dict:
    """Send fetched HTML text to Claude through the same extraction path."""
    content = [{"type": "text", "text": USER_TEXT_TEMPLATE.format(text=
        f"[Fetched from URL: {source_label}]\n\n{html_text[:60000]}"
    )}]
    return call_claude(client, content)

def process_email(client: "Anthropic", eml_path: Path):
    """Extract invoice data from a .eml file. Walk attachments first (PDFs treated like
    normal PDF input). If the body contains a public invoice URL, fetch and extract."""
    import email
    from email import policy
    msg = email.message_from_bytes(eml_path.read_bytes(), policy=policy.default)
    subject = msg.get("subject", "")
    sender  = msg.get("from", "")
    body_plain = msg.get_body(preferencelist=("plain",))
    body_html  = msg.get_body(preferencelist=("html",))
    body_text = body_plain.get_content() if body_plain else ""
    body_html_text = body_html.get_content() if body_html else ""
    # URLs live in the HTML version when the plain version strips them out
    url_search_text = body_html_text or body_text

    invoices, audits = [], []

    # First: extract what we can from the email body itself
    body_input_text = (f"Subject: {subject}\nFrom: {sender}\n\n{body_text}")
    try:
        record = call_claude(client, [{"type": "text", "text": USER_TEXT_TEMPLATE.format(text=body_input_text)}])
    except Exception as e:
        audits.append(error_audit(eml_path.name, 1, "extraction_error", str(e)))
        return invoices, audits

    # If we have invoice number + PO but no total, try fetching URLs in the body
    fetched_record = None
    fetched_from = None
    if record.get("total_amount_due") is None and not record.get("line_items"):
        import html as _html_lib
        urls = [_html_lib.unescape(u).rstrip(".,;)") for u in URL_RE.findall(url_search_text)]
        # Filter noise (XML namespaces, tracking pixels, branding links, unsubscribe)
        url_candidates = [u for u in urls if not any(
            n in u.lower() for n in
            ("schemas.microsoft.com", "w3.org", "sendgrid", "unsubscribe",
             "tracking", "/wf/open", "mcauto-images", "hdsbrands.com")
        )]
        # Prioritise URLs that look invoice-related
        def _score(u):
            score = 0
            for k in ("invoice", "report", "bill", "pdf", "viewer"):
                if k in u.lower(): score += 1
            return -score  # negative because sort is ascending
        url_candidates.sort(key=_score)
        # Drop near-duplicates (HTML/PDF variants of the same invoice resource)
        seen = set(); deduped = []
        for u in url_candidates:
            key = u.split("?")[0][:80]
            if key in seen: continue
            seen.add(key); deduped.append(u)
        for url in deduped[:5]:  # cap attempts
            log.info("  trying URL: %s", url[:80])
            data, ctype = fetch_url(url)
            if data is None:
                continue
            if "html" in ctype or "text" in ctype:
                try:
                    html_text = data.decode("utf-8", errors="replace")
                except Exception:
                    continue
                if any(k in html_text.lower() for k in ("invoice", "total", "amount due", "po #", "p.o.")):
                    fetched_record = claude_extract_from_html(client, html_text, url)
                    fetched_from = url
                    log.info("  URL fetch produced extraction: total=%s line_items=%d", fetched_record.get("total_amount_due"), len(fetched_record.get("line_items") or []))
                    break
            elif "pdf" in ctype:
                # Save and re-process as PDF
                tmp = Path("/tmp/_fetched.pdf")
                tmp.write_bytes(data)
                try:
                    pdf = pdfium.PdfDocument(str(tmp))
                    if len(pdf) > 0:
                        inv, aud = extract_one_page(client, tmp, 0, pdf[0])
                        if inv: fetched_record = {**record, "total_amount_due": inv.get("total_amount_due"), "line_items": inv.get("line_items"), "currency": inv.get("currency")}
                        fetched_from = url
                        break
                except Exception:
                    continue

    # Decide which record to use. When URL fetch yielded a real total/lines, prefer
    # that record entirely (it's the source of financial truth); fall back to the
    # email-body record otherwise.
    if fetched_record and fetched_record.get("total_amount_due") is not None:
        use_record = dict(fetched_record)
        # Backfill identification fields from the email body if the fetched view
        # didn't include them (often the invoice PDF omits the email-context fields).
        for k in ("vendor_name", "invoice_number", "po_number", "invoice_date"):
            if not use_record.get(k):
                use_record[k] = record.get(k)
        path_label = f"email_body+url_fetch ({fetched_from})"
    else:
        use_record = dict(record)
        path_label = "email_body"
        if use_record.get("total_amount_due") is None and use_record.get("extraction_confidence") != "low":
            use_record["extraction_confidence"] = "low"
            use_record["notes"] = (use_record.get("notes") or "") + " | Total not extractable from email body; URL fetch attempted but did not yield a total."

    inv, aud = split_invoice_and_audit(use_record, eml_path.name, 1, path_label, "ok")
    if inv: invoices.append(inv)
    audits.append(aud)
    return invoices, audits


# ---- vendor follow-up draft generator --------------------------------------

def vendor_followup_draft(invoice: dict, audit: dict) -> dict | None:
    """For records flagged for review, generate a copy-pasteable email draft
    the AP team can send to the vendor asking for the specific clarification
    needed to close the loop. Returns None when the record doesn't need follow-up.

    The goal: the human reviewer doesn't have to figure out *what to ask the
    vendor* — they just open the draft, read it for 10 seconds, edit if needed,
    and send. The system tees up the work; the human chooses to do it."""
    vendor    = (invoice.get("vendor") or {}).get("name") or audit.get("vendor_raw_name") or "Vendor"
    inv_num   = invoice.get("invoice_number") or "(unknown)"
    inv_date  = invoice.get("invoice_date") or "(unknown)"
    po_num    = invoice.get("po_number") or "(unknown)"
    total     = invoice.get("total_amount_due")
    sub_check = audit.get("subtotal_check")
    sub_note  = audit.get("subtotal_note") or ""
    status    = audit.get("extraction_status")
    confidence = audit.get("extraction_confidence")

    # Compute line sum + delta for the body
    line_items = invoice.get("line_items") or []
    line_sum = sum((li.get("quantity") or 0) * (li.get("unit_price") or 0) for li in line_items)
    delta = (total or 0) - line_sum if total is not None else None

    # Decide the issue and the ask
    if sub_check == "failed" and delta is not None and abs(delta) > 0.01:
        issue = (
            f"the line items we extracted from your invoice total ${line_sum:,.2f}, "
            f"but the printed Total Amount Due is ${total:,.2f} — a difference of "
            f"${abs(delta):,.2f} we can't reconcile from the document."
        )
        ask = (
            "Could you confirm whether there are any additional line items (freight, "
            "shipping, handling, tax, surcharge, or discount adjustments) that aren't "
            "shown on the invoice we received? If so, could you send a corrected copy "
            "with all charges itemized so we can post the invoice cleanly to our AP "
            "system?"
        )
    elif total is None or not line_items:
        issue = (
            "the invoice you sent us did not include line-item detail or a payable "
            "total in the document body — only a link or summary in the email."
        )
        ask = (
            "Could you send a complete copy of the invoice with line items and total "
            "as an attachment (PDF preferred), so we can post the invoice cleanly to "
            "our AP system?"
        )
    elif confidence == "low":
        issue = "we couldn't reliably read several fields from the document we received."
        ask = (
            "Could you re-send the invoice as a fresh PDF or higher-resolution scan? "
            "Some fields didn't read clearly enough for us to confirm the values."
        )
    else:
        return None  # no follow-up needed

    subject = f"Question on invoice {inv_num} — clarification needed for AP posting"
    body = (
        f"Hi [vendor contact],\n\n"
        f"We received your invoice {inv_num} dated {inv_date} (PO {po_num}) and started "
        f"processing it through our intake system, but {issue}\n\n"
        f"{ask}\n\n"
        f"For reference, here is what we extracted from the document:\n"
        f"  Vendor:        {vendor}\n"
        f"  Invoice #:     {inv_num}\n"
        f"  Invoice Date:  {inv_date}\n"
        f"  PO #:          {po_num}\n"
        f"  Total Due:     ${total:,.2f}" if total is not None else f"  Total Due:     [not extracted]"
    )
    body += f"\n  Line items extracted ({len(line_items)}):\n"
    for li in line_items:
        body += f"    - {li.get('item_name','(no name)')}: qty {li.get('quantity')} x ${li.get('unit_price')}\n"
    if delta is not None and abs(delta) > 0.01:
        body += f"\n  Line-item subtotal: ${line_sum:,.2f}\n"
        body += f"  Unexplained delta:  ${delta:+,.2f}\n"
    body += (
        "\nThanks — we'll post the invoice as soon as you can confirm. We're trying "
        "to clear our AP backlog and your reply will help us pay you sooner.\n\n"
        "— HDS Marketing AP team"
    )

    return {
        "subject": subject,
        "body": body,
        "reason_code": sub_check if sub_check == "failed" else ("low_confidence" if confidence == "low" else "incomplete_data"),
        "perceived_issue": issue,
        "ready_to_send": False,  # explicit: human must review
    }


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
    emls = sorted(input_dir.glob("*.eml"))
    if not pdfs and not emls:
        print(f"ERROR: no .pdf or .eml files in {input_dir}", file=sys.stderr); return 2

    log.info("found %d PDF(s) in %s", len(pdfs), input_dir)
    client = Anthropic(api_key=api_key)
    all_invoices, all_audits = [], []
    for pdf_path in pdfs:
        inv, aud = process_pdf(client, pdf_path)
        all_invoices.extend(inv); all_audits.extend(aud)
    for eml_path in emls:
        log.info("processing %s", eml_path.name)
        inv, aud = process_email(client, eml_path)
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

    # For any flagged record, generate a copy-pasteable vendor follow-up email draft.
    # Builds the index inv_idx -> invoice for matching.
    inv_by_invnum = {inv.get("invoice_number"): inv for inv in all_invoices if inv.get("invoice_number")}
    for a in all_audits:
        inv = None
        # Try to find the matching invoice for this audit record.
        # Match priority: source_file+page first, then vendor+invoice_number.
        for candidate in all_invoices:
            if (candidate.get("_source_file") == a.get("source_file")
                and candidate.get("_page_number") == a.get("page_number")):
                inv = candidate; break
        if not inv:
            for candidate in all_invoices:
                vmatch = candidate.get("vendor", {}).get("name") == a.get("vendor_canonical_name")
                # Audit may not store invoice_number directly; we use the file name + the canonical vendor
                # to do a heuristic match: if there's exactly one invoice for that vendor + source_file, take it
                if vmatch:
                    # Strong match if source_file appears in the invoice metadata we'll inject below
                    inv = candidate
                    # don't break — let later iterations refine if a better match exists
                    if a.get("source_file","").startswith(candidate.get("invoice_number","")):
                        break
        if not inv:
            # Fall back to a minimal dict for the email-draft generator
            inv = {"vendor": {"name": a.get("vendor_canonical_name")},
                   "invoice_number": None, "invoice_date": None, "po_number": None,
                   "total_amount_due": None, "line_items": []}
        # Only generate drafts when the record actually needs follow-up
        if (a.get("subtotal_check") == "failed"
            or a.get("extraction_confidence") in ("low", "medium")
            or a.get("extraction_status") != "ok"
            or not a.get("vendor_known")):
            draft = vendor_followup_draft(inv, a)
            if draft:
                a["vendor_followup_draft"] = draft

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
