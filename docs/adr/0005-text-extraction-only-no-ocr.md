---
status: accepted
---

# Text-extraction only — no OCR, no JS rendering (future OCR is PDF-first)

The tool indexes only the extractable text a document already carries. It does not OCR
images and does not execute JavaScript, so image-only/scanned PDFs and image-only or
JS-rendered web pages are out of scope — they index as empty or near-empty. This is a single
**tool-wide** boundary, stated to users for every input type (PDF, HTML, Web), not just the
web feature. Image-only PDFs are the canonical case and the current code already reports it
explicitly in `read_spec_text`.

If OCR is added later, it is **prioritized for PDFs first** (scanned datasheets and specs are
the common real demand), and HTML/web image content only after.

## Consequences

- The README carries this disclaimer for **PDF reading** as well as web, in the same words.
- A document that yields no text is reported loudly as a known limitation (image-only /
  scanned / JS-rendered), never silently treated as empty — matching existing
  `read_spec_text` behavior.
- "Add OCR" is a bounded future feature with a stated order (PDF before HTML), not an
  open-ended ask.
