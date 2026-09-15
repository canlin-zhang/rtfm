---
status: accepted
---

# Indexing is a staged pipeline: select, decode, then hand to a routine

Getting a file into the index used to be one tangle of extension checks. A single
constant, `TEXT_EXTS`, decided both which files were selected and which got
markdown heading parsing. Body extraction and doc-signal extraction each
dispatched on extension independently, in separate `if` chains. And
`read_text(errors="replace")` meant "can rtfm read this?" never got asked at all.

Three failures came out of that, all of the same shape — a file is accepted,
something quietly does nothing, and the result looks like success:

- A `.bzl` file added to the selection set was selected for indexing and then
  extracted to zero rows, because only one of the two dispatches knew about it.
- A binary file indexed cleanly with `errors: 0`, its bytes mangled into
  searchable text by U+FFFD substitution.
- A source whose extension list matched nothing indexed zero files and reported
  no reason.

## Decision

Indexing is three stages. Each asks exactly one question, and each reports when
it is the stage that emptied the source.

```
a tree of files
  │
  ├─ Stage 1  SELECT      which files does this source index?
  │           extension lists only — ext_allowlist, or ext_blocklist plus
  │           rtfm's defaults (ADR 0014). Never inspects content.
  │           → if nothing survives, say so and name the list
  │
  ├─ Stage 2  DECODE      can rtfm read what it selected?
  │           UTF-8, strictly. A file that does not decode is a recorded
  │           extraction error, never mangled into the index.
  │           → if nothing survives, say so and name the encoding
  │
  └─ Stage 3  HANDLE      how does this file become rows?
              one routine per format, chosen by extension, catch-all last.
```

### The stages do not substitute for each other

A stage answers its own question only. Using a later stage to do an earlier
stage's job reports the wrong cause: if a `.png` slipped past Stage 1, catching it
at Stage 2 would tell the user "not valid UTF-8" when the true answer is that
their blocklist is missing `.png`. Stage 1 is a policy question about file kinds;
Stage 2 is a capability statement about what rtfm can read. Neither is a heuristic
for the other, and neither inspects the thing the other owns.

This is also why Stage 2 needs no content sniffing — no NUL-byte scan, no
`libmagic`, no new dependency. A strict decode answers the question definitionally:
a file that decodes is a text serialization and one that does not, is not.

### Every stage that can empty a source says so

A stage that silently drops everything produces a source that is configured,
reports success, and answers nothing — the confident-but-empty failure this
codebase keeps meeting. So each stage reports its own zero, naming the setting
responsible, and the report reaches `search`, `reindex` and `health_check` rather
than sitting in a nested summary nobody reads.

Stage 1 reports at two granularities, because both are causes: the source as a
whole selected nothing, and an individual `paths` prefix contributed nothing. A
source with prefixes gets both — the headline and the specific entry to fix.

### Handling routines are a registry, not an `if` chain

```python
ROUTINES = (
    _Routine("pdf",    frozenset({".pdf"}), _pdf_rows,  _pdf_doc_signal),
    _Routine("markup", MARKUP_EXTS,         _text_rows, _text_doc_signal),
    _Routine("text",   None,                _text_rows, _no_doc_signal),
)
```

Body extraction and doc-level signal live in the same record, so the two
dispatches cannot disagree about which routine a file belongs to. First match
wins; `exts=None` marks the catch-all, which stays last. Adding a format — HTML
with anchor locators (ADR 0004), or a structured reader for another markup — is
adding a routine, not editing two parallel `if` chains.

The catch-all deliberately carries no doc-level signal. Guessing structure from a
file rtfm has no reader for would put a source file's licence header into
doc-level ranking (ADR 0012): `#` is a comment in Starlark, Python and shell, and
the markup reader would turn every comment line into a heading.

## Consequences

- `TEXT_EXTS` is gone. `MARKUP_EXTS` is the markup routine's extension set and
  nothing else; `DEFAULT_EXT_BLOCKLIST` belongs to Stage 1 alone.
- `errors="replace"` is removed from every decode site. A non-UTF-8 file that a
  user genuinely wants indexed now fails loudly instead of being silently
  mangled; the remedy is to convert it or to exclude its type. If real non-UTF-8
  documents turn up, the fix is attempting a second named encoding at Stage 2,
  never returning to replacement.
- `contents.error` and `summary["errors"]` stop being write-only — the tools
  surface them, because a stated format boundary is worthless if a user cannot
  hear that a file hit it.
- A new format is one `_Routine` entry plus its two functions.
