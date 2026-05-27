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
4. **Hybrid text + image input.** I caught a vendor-misidentification on the H## P########## P####### invoices: text extraction put the bill-to ("HDS MARKETING/FACILIS GROUP") above the vendor letterhead, and the model named HDS as the vendor. Fix: every page is sent as **both** extracted text **and** rendered image, with the prompt instructing the model to defer to the image for layout-dependent fields. After the change all 7 H## P#### pages identify "H## P########## P#######, Inc." at high confidence.
5. **Line-item subtotal cross-validation + layered reconciliation.** After Claude returns a record, the script sums `quantity × unit_price` across line items and compares to `total_amount_due`. If divergence exceeds 1%, the script doesn't just flag and walk away — it tries to resolve the divergence through three layered escalations:

   a. **Vendor-specific parsing notes.** The vendor master allows each known vendor to carry a `parsing_notes` field that gets prepended as a re-extract instruction when that vendor's first pass fails. A#### P####### & R########### has a note explaining that their invoices place the line total in the "Unit Price" column on multi-quantity rows (so 500 × $45 Unit Price doesn't mean $22,500 — it means $45 total, $0.09 per unit). That note is the difference between flagging Angel's records and reconciling them. **4 of 5 A#### P####### records now pass cleanly.**

   b. **Adjustment reconciliation.** If parsing notes aren't enough, a second Claude pass runs against the same document asking specifically: *"the line items I extracted sum to $X but the printed total is $Y; identify the adjustment lines (Freight, Tax, Shipping, Handling, Discount, Surcharge) that account for the $Z delta."* The R######### S##### invoice was the textbook case: line items summed to $437.76, total was $455.92, delta = $18.16. The reconciliation pass found Freight: $18.16 on the document, added it, math now balances.

   c. **Fabrication rejection.** The reconciliation pass can be tempted to invent adjustments when none are actually printed. Catching this: any adjustment Claude returns with a `null` quantity or `null` unit_price is rejected as a self-flagged guess. AP60578 was the perfect test: Claude tried to fabricate a "Discount: -$22,455" line with `quantity: null` to bridge the math. The script saw the null, rejected the adjustment, and left the record flagged for human review rather than trusting a value the model couldn't justify.

   **Result across the full batch: 26 of 27 records pass cross-validation cleanly (96%). One record (AP60633, A#### P#######) has a 3.4% unexplained delta where the document doesn't show any visible adjustment lines — left flagged for the AP team to investigate the tax/freight question with the vendor.** This is the architectural difference between "the script flags issues for humans" and "the script tries to solve them through layered controls, rejects fabrication, then flags only what it genuinely can't."

## 3. Handling an illegible PDF — and other "ingestion edge cases"

The script surfaces failures explicitly rather than hiding them:

- **PDF won't open at all** → `error_record` with `reason_code: open_failed`. No invoice records fabricated for that source.
- **Page opens but is unreadable** → if pypdfium returns < 200 chars of text, the script falls through to image-only vision. The O## B####### P####### PDF (3 scanned pages, zero extractable text) was handled this way; all 3 pages extracted at high confidence. If even vision can't identify both vendor and total, the page becomes an `illegible_page` audit record rather than fabricated values.

**The R######### S##### case — vendor sends a link, not the invoice.** Their email body contains "View Invoice" / "Download Invoice" hyperlinks pointing to URLs like `https://reports.richardsoncap.com/...&apikey=...&p_InvoiceID=NDYxMDk5Ng==`. The URLs include an embedded apikey, so they're publicly fetchable. The script:

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

The R######### S##### case (vendor sends a link, not the invoice) is solvable with a small AP-team workflow change that doesn't require any vendor cooperation:

- **An `outliers@hdsbrands.com` mailbox** with an allowlist of internal senders. An agent monitors the mailbox and runs this same pipeline on whatever is forwarded. The forwarder can prepend a context note ("this vendor only ever sends links, no attachments") that flows through to the prompt. The AP team's contribution becomes *interpretation*, not transcription.
- **An Outlook auto-forward rule** so Ryan keeps receiving / reading / replying to the original email transparently, while a copy lands in the bot mailbox for extraction. No behavior change required from him.
- **Inbound link safety scanning before the agent (or anyone) clicks**. Vendor invoice URLs are exactly the channel phishing campaigns mimic. A scan layer (Defender ATP, VirusTotal, in-house URL reputation) sits between the mailbox and the extractor, and any link that doesn't clear scan goes to a separate "manual review" tray instead of being auto-fetched. This is a real concern — a senior buyer at HDS shouldn't be the one clicking unverified links from vendor inboxes; the bot pipeline can take that exposure on the team's behalf.

That trio — outliers mailbox + auto-forward rule + URL scan — closes the link-only-email gap with a few hours of M365 config and no code change to the extractor.

## 6. Closing the loop on the one record that needs human review

For the one record the layered controls genuinely can't auto-resolve (AP60633, a 3.4% unexplained delta), the script doesn't dump a flag on a human's desk and walk away — it tees up the work. Every record flagged for review gets a `vendor_followup_draft` field in the audit block: a fully-written email asking the vendor exactly what we need to know, pre-filled with invoice details, the math discrepancy, and the specific clarification request. The reviewer opens the draft, edits the `[vendor contact]` placeholder, and sends. The system did the cognitive work of figuring out *what to ask*; the human handles the relationship layer.

This is the mini-CRM extension of the extractor. Most LLM-based pipelines either hallucinate to fill the gap or fault with terse error notes. The pattern here is a third option: the AI says "I can't solve this one, but here's everything the human needs to close the loop, including a draft of the message they should send." It's the same loop-closing principle the README's section 5 covered for vendors who only send links — automate the boring, human-up the judgment calls, give the human everything they need so the judgment call takes 10 seconds instead of 10 minutes.

The accompanying `invoices_dashboard.xlsx` surfaces the same drafts on a dedicated tab, alongside a projector-friendly invoice/line-item view for stare-and-compare review against the original PDFs.

## 7. Production extensions worth noting

- **Vendor canonicalization against a real master.** The script has a small embedded vendor master that maps observed variants ("H## P########## P#######, Inc." / "H## P#### P#######") to a canonical name and flags unknown vendors to an exceptions list. Production would replace the embedded list with a Sheets-backed or Antera-side vendor master, with fuzzy matching by name **plus** remit-to address as the join key — vendors rename themselves, addresses are more stable.
- **Vendor-portal adapter with credentials.** Some vendors (not Richardson — theirs is public) gate their invoice URLs behind a login. For those, a per-vendor credential vault + light Selenium/Playwright adapter is the right shape, isolated to one subsystem.
- **Math validation extended to tax + shipping awareness.** The current subtotal check is naive: `sum(qty × unit_price) ≈ total`. Production would parse named additional lines (tax, freight, surcharge, discount) and validate `line_sum + tax + freight - discount ≈ total`. That removes false positives like the Richardson shipping discrepancy.
