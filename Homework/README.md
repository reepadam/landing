# HDS AI Workflow Architect — Invoice Extraction Homework

**Adam Reep · adamjreep@gmail.com · 2026-05-27**

Files in this folder:

- `extract_invoices.py` — the script (run with `python extract_invoices.py --input ./invoices_raw --output invoices.json`)
- `invoices.json` — output across all 5 emailed invoice packages (27 invoice records, $59,194.29 total recognized)
- `README.md` — this document

A note on the output shape: I structured the JSON as two cleanly-separated blocks. The `invoices[]` array is the clean vendor-bill payload, free of implementation details. The `_audit` block at the bottom holds extraction metadata — LLM model, source file, confidence, subtotal-check results — for the AP team's review queue.

The script source, README, and a non-redacted sample record per vendor are also viewable at **[adamjreep.com/Homework](https://adamjreep.com/Homework)**. The site doesn't host the real HDS invoice data; the email zip is the only path to that.

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
5. **Line-item subtotal cross-validation.** After Claude returns a record, the script sums `quantity × unit_price` across line items and compares to `total_amount_due`. If divergence exceeds 1%, the record is demoted from `high` to `medium` confidence with an explanatory note, and `review_required` flips true on the batch. In this batch, 10 of 27 records tripped this check — most are the Angel Printing & Reproduction series, where the vendor put the order total in the "Unit Price" column on a 500-quantity row, making the math look impossible. The script flags these for human review rather than emitting them as confidently extracted. Same logic catches arithmetic hallucinations the model wouldn't notice itself.

## 3. Handling an illegible PDF

Two failure modes, both surfaced explicitly rather than hidden:

- **PDF won't open at all** → `error_record` with `reason_code: open_failed` and the exception message. No invoice records fabricated for that source.
- **Page opens but is unreadable** → if pypdfium returns < 200 chars of text, the script falls through to image-only vision. The Old Brooklyn Printing PDF in this batch (3 scanned pages, zero extractable text) was handled this way — all 3 pages extracted at high confidence. If even vision can't identify both vendor and total, the script converts the record to `reason_code: illegible_page` rather than emit fabricated values.

A real-world edge case from this batch: the Richardson Sports email contains a *link* to the invoice on the vendor portal — not the invoice itself. The script extracts what's visible from the email body (vendor, invoice number, PO, date), sets `total_amount_due: null`, marks `extraction_confidence: low`, and surfaces a note explaining the totals need to be fetched from the portal. Human follow-up rather than a guess.

The output payload includes `_audit.review_required` — true when any record needs human attention (error, low/medium confidence, subtotal-check failure, or unknown vendor). In production this routes records to a review queue before they reach any downstream system.

## 4. A note on the Antera API target

The homework spec mentioned shaping the output "as if it were going to be sent via a POST request to the Antera API." I pulled the public Antera v1 spec from api.anterasaas.com and read through it. **The public API exposes no vendor-bill, invoice, payable, or expense endpoint.** Coverage is sales-side only: `/accounts`, `/contacts`, `/orders`, `/products`, `/artwork`, `/identity`. The "expense" mentions in the spec are nested GL-account fields on order items (`qbExpenseAccount` suggests QuickBooks handles the financial side).

So the script outputs a clean, well-typed vendor-bill payload, and the `_audit.antera_target_endpoint` field is honest: *"Antera v1 public API has no AP/vendor-bill endpoint (sales-side only). Production adapter would route to whichever AP intake mechanism HDS uses (QB sync, manual UI assist, private endpoint, or Sheets review buffer)."* If HDS has a private/extended Antera endpoint not in the public spec, the script's clean schema maps to it in one place — the adapter layer — without changing the extraction logic.

## 5. Production extensions worth noting

Three enhancements that aren't in this script (Ryan asked for "lightweight, functional") but are close-by in design space:

- **Vendor canonicalization against a real master.** The script has a small embedded vendor master that maps observed variants ("HIT Promotional Products, Inc." / "Hit Promo Products") to a single canonical name and flags unknown vendors to an exceptions list. Production would replace the embedded list with a Sheets-backed or Antera vendor-master query, with fuzzy matching by name + remit-to address as the join key (not just name — vendors rename themselves).
- **Public-URL fetch.** When an email body references a public PDF URL, the script could fetch and route through the same vision pipeline — five lines of `requests` + `pypdfium`. The Richardson Sports case here is portal-gated (login wall) so a fetch returns the login HTML, not the invoice; that case needs a vendor-portal adapter with stored credentials per vendor — a separate subsystem.
- **An "outliers" forwardable mailbox.** Stand up `outliers@hdsbrands.com` with an allowlist of internal senders. An agent watches the mailbox and runs the same pipeline on forwarded edge cases. The forwarder can prepend a context note that flows through to the prompt. Keeps the human in the loop without making the human do the transcription work — the human's contribution is *interpretation*, not data entry. This is how the script becomes part of the AP team's daily workflow instead of a one-off tool.
