# HDS AI Workflow Architect — Invoice Extraction Homework

**Adam Reep · adamjreep@gmail.com · 2026-05-27**

Files in this folder:

- `extract_invoices.py` — the script (run with `python extract_invoices.py --input ./invoices_raw --output invoices.json`)
- `invoices.json` — output across all 5 emailed invoice packages (27 invoice records, $59,194.29 total)
- `README.md` — this document

The script is also runnable live with pre-loaded vendor batches and visible source at **[adamjreep.com/Homework](https://adamjreep.com/Homework)**.

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

In production I'd layer a fifth control: cross-validate that line-item subtotals reconcile to `total_amount_due`. A two-line math check that catches arithmetic hallucinations the model doesn't notice itself. Skipped here for the lightweight spec.

## 3. Handling an illegible PDF

Two failure modes, both surfaced explicitly rather than hidden:

- **PDF won't open at all** → `error_record` with `reason_code: open_failed` and the exception message. No invoice records are fabricated for that source.
- **Page opens but is unreadable** → if pypdfium returns < 200 chars of text, the script falls through to rendering the page to PNG and sending it via Claude vision. The Old Brooklyn Printing PDF in this batch (3 scanned pages, zero extractable text) was handled this way — all 3 pages extracted at high confidence. If even vision can't identify both vendor and total, the script converts the record to `reason_code: illegible_page` rather than emit fabricated values.

A real-world edge case from this batch worth calling out: the Richardson Sports email contains a *link* to the invoice on the vendor's portal — not the invoice itself. The script extracts what's available from the email body (vendor, invoice number, PO, date), sets `total_amount_due: null`, marks `extraction_confidence: low`, and surfaces a note explaining the totals need to be fetched from the portal. Human follow-up rather than a guess.

The output JSON includes a `review_required` boolean — true if any record needs human attention. In production this routes records to a review queue before they reach the Antera POST. The demo at adamjreep.com/OCR_LLM_Demo shows that queue implemented in the Bills & Invoices module.

## 4. Production extensions worth noting

Two enhancements that aren't in this script (Ryan asked for "lightweight, functional") but are close-by in design space:

- **Public-URL fetch.** When an email body references a public PDF URL, the script could fetch the PDF and route it through the same vision pipeline — five lines of `requests` + `pypdfium`. The Richardson Sports case in this batch is portal-gated (login wall), so a fetch would return the login HTML rather than the invoice; that case needs a vendor-portal adapter with stored credentials, which is a separate subsystem.
- **An "outliers" forwardable mailbox.** Stand up `outliers@hdsbrands.com` (or similar) with an allowlist of internal senders. An agent watches the mailbox and runs the same extraction pipeline on forwarded edge cases. The forwarder can prepend a short context note ("this vendor changes their invoice layout monthly", "ship date is the in-hands date for this one") that flows through to the prompt. Keeps the human in the loop without making the human do the boring extraction — the human's contribution is *interpretation*, not transcription. This is how the script becomes part of the AP team's daily workflow instead of a one-off tool.

---

**One downstream concern, not solved here:** even high-confidence extractions can produce minor vendor-name variations across documents ("Angel Printing&Reproduction" vs "Angel Printing & Reproduction"). Vendor canonicalization against Antera's vendor master — with fuzzy matching plus an exception queue for new vendors — belongs in the Antera-side adapter, not in this extractor.
