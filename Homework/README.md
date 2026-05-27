# HDS AI Workflow Architect — Invoice Extraction Homework

**Adam Reep · adamjreep@gmail.com · 2026-05-27**

Files in this folder:

- `extract_invoices.py` — the script (run with `python extract_invoices.py --input ./invoices_raw --output invoices.json`)
- `invoices.json` — output across all 5 emailed invoice packages (27 invoice records)
- `README.md` — this document

A note on the output shape: I structured the JSON as two cleanly-separated blocks. The `invoices[]` array is the clean vendor-bill payload, free of implementation details. The `_audit` block holds extraction metadata — LLM model, source file, confidence, subtotal-check results — for the AP team's review queue.

The script source, README, and a non-redacted schema demo per vendor are also viewable at **[adamjreep.com/Homework](https://adamjreep.com/Homework)**. The site doesn't host real HDS invoice data; the email zip is the only path to that.

---

## 1. Choice of LLM: Claude Haiku 4.5

Anthropic Claude Haiku 4.5 via the official Python SDK. Three reasons:

- **Tool-use schema enforcement.** Claude's structured `tools` parameter forces the model to call a single function (`record_invoice`) with a typed schema. Output of the wrong type, or with fabricated fields, is structurally prohibited — the model has no other return path. This is the strongest single anti-hallucination control available.
- **Vision in the same model.** No separate OCR pipeline. Scanned/image invoices and text-extractable invoices flow through the same code path. One model, one prompt, one place to reason about behavior.
- **Cost + latency.** The full 27-invoice run cost ~$0.20 in ~70 seconds. Production scale (thousands/month) is comfortably under $20/mo.

GPT-4.1-mini or Gemini Flash would work too. Claude's tool-use is cleanest for forcing schema compliance.

## 2. Prompt engineering against hallucination

1. **Forced tool-use with typed schema.** `tool_choice` pins the model to one function call with required, nullable, typed fields. Wrong types and missing fields are structurally impossible.
2. **Temperature 0.0.** Deterministic. Same input → same output. No creative register.
3. **"Extract only what you can read" instruction.** System prompt forbids inferring, computing, or fabricating values not printed on the document. Missing → `null`. The model self-reports uncertainty via `extraction_confidence: high | medium | low`.
4. **Hybrid text + image input.** I caught a vendor-misidentification on the Hit Promotional Products invoices: text extraction put the bill-to ("HDS MARKETING/FACILIS GROUP") above the vendor letterhead, and the model named HDS as the vendor. Fix: every page is sent as **both** extracted text **and** rendered image, with the prompt instructing the model to defer to the image for layout-dependent fields. After the change all 7 Hit Promo pages identify "HIT Promotional Products, Inc." at high confidence.
5. **Line-item subtotal cross-validation.** After Claude returns a record, the script sums `quantity × unit_price` across line items and compares to `total_amount_due`. If divergence exceeds 1%, the record is flagged in `_audit.records[].subtotal_check` and `review_required` flips true on the batch. In this batch, 10 of 27 records tripped this check — most are the Angel Printing & Reproduction series, where the vendor put the order total in the "Unit Price" column on a 500-quantity row, making the line-item math diverge from the printed total. The Richardson Sports record also tripped this (4% divergence: a shipping line on the PDF wasn't captured by the vision pass). Same logic catches arithmetic hallucinations the model wouldn't notice itself.

## 3. Handling an illegible PDF — and other "ingestion edge cases"

The script surfaces failures explicitly rather than hiding them:

- **PDF won't open at all** → `error_record` with `reason_code: open_failed`. No invoice records fabricated for that source.
- **Page opens but is unreadable** → if pypdfium returns < 200 chars of text, the script falls through to image-only vision. The Old Brooklyn Printing PDF (3 scanned pages, zero extractable text) was handled this way; all 3 pages extracted at high confidence. If even vision can't identify both vendor and total, the page becomes an `illegible_page` audit record rather than fabricated values.

**The Richardson Sports case — vendor sends a link, not the invoice.** Their email body contains "View Invoice" / "Download Invoice" hyperlinks pointing to URLs like `https://reports.richardsoncap.com/...&apikey=...&p_InvoiceID=NDYxMDk5Ng==`. The URLs include an embedded apikey, so they're publicly fetchable. The script:

1. Extracts what's visible from the email body itself (vendor, invoice number, PO, date)
2. Scans the HTML body for URLs (the plain-text body usually strips the URLs from the anchor text — the HTML version preserves them)
3. Filters noise (Outlook XML namespaces, sendgrid tracking pixels, branding links, unsubscribe)
4. Prioritises URLs containing invoice/report/bill keywords and fetches them
5. Routes the fetched HTML or PDF through the same Claude vision pipeline
6. Merges the financial detail from the fetch with the identification fields from the body

For this batch, the fetch produced a real total ($455.92) and the main line item; the subtotal check noticed a ~4% gap (likely a shipping line the vision pass missed), so the record is flagged for human review with the exact math in the audit note. Honest > confident.

The output payload includes `_audit.review_required` — true when any record needs human attention (error, low/medium confidence, subtotal-check failure, or unknown vendor). In production this routes records to a review queue before they reach any downstream system.

## 4. A note on the Antera API target

I pulled the public Antera v1 spec from api.anterasaas.com and read through all 79 pages. **The public API exposes no vendor-bill, invoice, payable, or expense endpoint.** Coverage is sales-side only: `/accounts`, `/contacts`, `/orders`, `/products`, `/artwork`, `/identity`. The "expense" mentions in the spec are nested GL-account fields on order items, and the `qbExpenseAccount` field suggests QuickBooks handles the AP side.

So the script outputs a clean, well-typed vendor-bill payload, and the `_audit.antera_target_endpoint` field is honest: *"Antera v1 public API has no AP/vendor-bill endpoint (sales-side only). Production adapter would route to whichever AP intake mechanism HDS uses."* If HDS has a private/extended Antera endpoint not in the public spec, the script's clean schema maps to it in one place — the adapter layer — without changing the extraction logic.

## 5. Closing the loop on edge cases with HDS-side infrastructure

The Richardson Sports case (vendor sends a link, not the invoice) is solvable with a small AP-team workflow change that doesn't require any vendor cooperation:

- **An `outliers@hdsbrands.com` mailbox** with an allowlist of internal senders. An agent monitors the mailbox and runs this same pipeline on whatever is forwarded. The forwarder can prepend a context note ("this vendor only ever sends links, no attachments") that flows through to the prompt. The AP team's contribution becomes *interpretation*, not transcription.
- **An Outlook auto-forward rule** so Ryan keeps receiving / reading / replying to the original email transparently, while a copy lands in the bot mailbox for extraction. No behavior change required from him.
- **Inbound link safety scanning before the agent (or anyone) clicks**. Vendor invoice URLs are exactly the channel phishing campaigns mimic. A scan layer (Defender ATP, VirusTotal, in-house URL reputation) sits between the mailbox and the extractor, and any link that doesn't clear scan goes to a separate "manual review" tray instead of being auto-fetched. This is a real concern — a senior buyer at HDS shouldn't be the one clicking unverified links from vendor inboxes; the bot pipeline can take that exposure on the team's behalf.

That trio — outliers mailbox + auto-forward rule + URL scan — closes the link-only-email gap with a few hours of M365 config and no code change to the extractor.

## 6. Production extensions worth noting

- **Vendor canonicalization against a real master.** The script has a small embedded vendor master that maps observed variants ("HIT Promotional Products, Inc." / "Hit Promo Products") to a canonical name and flags unknown vendors to an exceptions list. Production would replace the embedded list with a Sheets-backed or Antera-side vendor master, with fuzzy matching by name **plus** remit-to address as the join key — vendors rename themselves, addresses are more stable.
- **Vendor-portal adapter with credentials.** Some vendors (not Richardson — theirs is public) gate their invoice URLs behind a login. For those, a per-vendor credential vault + light Selenium/Playwright adapter is the right shape, isolated to one subsystem.
- **Math validation extended to tax + shipping awareness.** The current subtotal check is naive: `sum(qty × unit_price) ≈ total`. Production would parse named additional lines (tax, freight, surcharge, discount) and validate `line_sum + tax + freight - discount ≈ total`. That removes false positives like the Richardson shipping discrepancy.
