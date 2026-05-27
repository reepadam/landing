# HDS AI Workflow Architect — Invoice Extraction Homework

**Adam Reep · adamjreep@gmail.com · 2026-05-27**

## Files in this zip

- `extract_invoices.py` — the script (`python extract_invoices.py --input ./invoices_raw --output invoices.json`)
- `extract_invoices_minimal.py` — a 122-line strict-spec version (just schema + extraction + JSON, no reconciliation/parsing-notes/follow-up-drafts) for comparison
- `invoices.json` — output across all 5 emailed packages (26 invoice records (one multi-page invoice merged))
- `invoices_dashboard.xlsx` — stare-and-compare review view (Summary / Invoices / Line Items / Follow-up Drafts tabs)
- `README.md` — this 1-pager
- `DESIGN_NOTES.md` — the architectural deep-read (LLM choice, anti-hallucination, illegible-PDF handling, layered reconciliation, Antera spec finding, security considerations, production extensions)

## What the script does

Ingests a folder of PDF invoices (and `.eml` files), routes each page through Claude Haiku 4.5 with a forced tool-use schema, and emits a clean `invoices[]` array plus a sidecar `_audit` block — the first is the vendor-bill payload, the second is the AP team's review queue with confidence, math-check results, and pre-written vendor follow-up email drafts for anything that needs human review.

## Headline results on this batch

- **27 records extracted from 5 emailed packages**
- **26 of 26 reconcile cleanly** (line items + adjustments sum to the printed total within 1%)
- **1 flagged for human review** — AP60633 has a 3.4% unexplained delta where the document doesn't show any visible adjustment line; the script generates a pre-written email asking the vendor for clarification, which the AP team can copy/paste/send
- **Cost:** ~$0.20 in API spend, ~70 seconds wall-clock

## Why the layered controls matter

The script doesn't just flag mismatches — it tries to resolve them through three escalations: vendor-specific parsing notes (for known-quirky formats), an adjustment-finder pass (asks Claude to identify Freight/Tax/Discount lines that explain the math gap), and a fabrication guard (rejects any reconciled adjustment with a null field — the model's self-flag for "I'm guessing"). The full reasoning + worked examples are in `DESIGN_NOTES.md`.

## A note on Antera

Antera's public v1 spec has no vendor-bill / AP endpoint — coverage is sales-side only. The script emits a clean schema; the production adapter maps `invoices[]` to whichever AP intake mechanism HDS actually uses. `DESIGN_NOTES.md` section 4 has the longer version.

## Live at

**[adamjreep.com/Homework](https://adamjreep.com/Homework)** — script source, rendered design notes, per-vendor schema demonstration (values redacted). The email zip is the only path to real data.
