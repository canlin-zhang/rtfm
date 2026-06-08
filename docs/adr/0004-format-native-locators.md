---
status: accepted
---

# Hits are cited by a format-native locator

How a search hit reports its location is chosen per file format, not forced into one scheme:

- **PDF** → page number.
- **Markdown / RST / plain text / code** → source line (the source line is itself readable).
- **HTML** → nearest native heading/anchor (an element `id`, else nearest heading text),
  falling back to source line when the page has no structure. Stripped-HTML line numbers
  point at tag soup, so the page's own anchors — which are also URL fragments — are the
  human-meaningful, deterministic locator.
- **Web** _(planned)_ → URL + native anchor, reusing HTML's locator path unchanged.

The hit-reporting layer therefore carries a typed locator (e.g. `{kind: page|line|anchor,
value}`) rather than today's bare `p.N` / `L.N` strings.

## Considered Options

- **One uniform scheme** (e.g. line numbers everywhere) — rejected: stripped-HTML line
  numbers are meaningless to a reader, and Web sources have no local file or line at all.
- **LLM-generated semantic anchors as the locator** — rejected: non-deterministic, so
  citations would rot on every re-index, and server-side LLM calls would wreck the
  dependency-light install (every installer would need an API key and pay tokens to index a
  folder). HTML's native labels are deterministic and free.

## Consequences

- Markdown/RST keep line citation (their source line is readable); only HTML switches to
  native anchors, because only HTML's source line is unreadable tag soup.
- Capturing native anchors needs a light DOM walk (track headings/ids during extraction) —
  a hair more than bare `get_text()`, but not main-content extraction: the full text,
  including boilerplate, is still indexed.
- Web (planned) inherits the HTML locator path unchanged.
