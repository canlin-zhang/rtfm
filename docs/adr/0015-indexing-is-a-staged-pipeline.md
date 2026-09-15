---
status: accepted
---

# Indexing is a staged pipeline: select, access, decode, then hand to a routine

Getting a file into the index used to be one tangle of extension checks. A single
constant, `TEXT_EXTS`, decided both which files were selected and which got
markdown heading parsing. Body extraction and doc-signal extraction each
dispatched on extension independently, in separate `if` chains. And
`read_text(errors="replace")` meant "can rtfm read this?" never got asked at all.

Four failures came out of that. The first three share a shape — a file is
accepted, something quietly does nothing, and the result looks like success:

- A `.bzl` file added to the selection set was selected for indexing and then
  extracted to zero rows, because only one of the two dispatches knew about it.
- A binary file indexed cleanly with `errors: 0`, its bytes mangled into
  searchable text by U+FFFD substitution.
- A source whose extension list matched nothing indexed zero files and reported
  no reason.

The fourth is louder and worse. Nothing owned the question "can rtfm open this
file at all", so an unreadable one landed wherever it happened to fall — measured
on a source holding one `chmod 000` file beside one readable file:

```
reindex()      RAISES PermissionError          — the whole call dies
search()       "AUTO-REINDEX FAILED 'a' ..."   — survives, drops the source for one file
health_check() ok: True, issues: []            — reports nothing at all
```

A subtree the process cannot enter is worse still: `rglob` swallows the
`PermissionError` inside `scandir`, so those files never existed as far as rtfm
knows, and no tool can report what no code path can see.

## Decision

Indexing is four stages. Each asks exactly one question, and each reports when
it is the stage that emptied the source.

```
a tree of files
  │
  ├─ Stage 1  SELECT      which files does this source index?
  │           extension lists only — ext_allowlist, or ext_blocklist plus
  │           rtfm's defaults (ADR 0014). Never inspects content.
  │           → if nothing survives, say so and name the list
  │
  ├─ Stage 2  ACCESS      can rtfm open the bytes of a file it chose?
  │           an unreadable file is skipped and named, never fatal.
  │           → if nothing survives, say so and name what could not be opened
  │
  ├─ Stage 3  DECODE      can rtfm correctly process the bytes, given that only
  │           UTF-8 is supported? Strict decoding; a file that does not decode
  │           is a recorded extraction error, never mangled into the index.
  │           → if nothing survives, say so and name the encoding
  │
  └─ Stage 4  HANDLE      how does this file become rows?
              one routine per format, chosen by extension.
```

Each stage has its own remedy, which is the test for whether a stage is real:

| Stage | Failure means | The user fixes |
| --- | --- | --- |
| SELECT | nothing matched, or the tree was not fully enumerable | the manifest, or permissions |
| ACCESS | a chosen file could not be opened | permissions, or the mount |
| DECODE | the bytes are not UTF-8 | convert the file, or exclude its type |
| HANDLE | — | no failure mode of its own |

### The stages do not substitute for each other

A stage answers its own question only. Using a later stage to do an earlier
stage's job reports the wrong cause: a `.png` that slipped past SELECT would be
reported by DECODE as "not valid UTF-8" when the true answer is a blocklist
missing `.png`; a file behind a permission wall reported by DECODE would tell the
user to convert it when the fix is `chmod`. SELECT is a policy question about file
kinds, ACCESS is an operating-system question about this process's rights, DECODE
is a capability statement about what rtfm can read. None is a heuristic for the
others, and none inspects what another owns.

This is also why DECODE needs no content sniffing — no NUL-byte scan, no
`libmagic`, no new dependency. A strict decode answers the question definitionally:
a file that decodes is a text serialization and one that does not, is not.

A permission failure is two different failures, and they fall on opposite sides of
SELECT. A file rtfm cannot open is necessarily *after* SELECT — rtfm only opens
files it chose, so this is ACCESS. A directory rtfm cannot enter is necessarily
*before* it: SELECT needs the listing, and an extension list cannot be applied to
names that cannot be read. That is not ACCESS failing, it is SELECT's *input* being
incomplete, so SELECT reports it (below) and ACCESS never sees it.

Either way the walk has to notice. It cannot be a bare `rglob`: `pathlib` catches
`OSError` inside `scandir` and yields nothing, which is indistinguishable from an
empty directory.

### Every stage that can empty a source says so

A stage that silently drops everything produces a source that is configured,
reports success, and answers nothing — the confident-but-empty failure this
codebase keeps meeting. So each stage reports its own zero, naming the setting
responsible, and the report reaches `search`, `reindex` and `health_check` rather
than sitting in a nested summary nobody reads.

SELECT reports at three granularities, because each is a different cause: the
source as a whole selected nothing, an individual `paths` prefix contributed
nothing, and a directory could not be enumerated. A source with prefixes gets both
of the first two — the headline and the specific entry to fix.

The third is SELECT's honesty about its own input: what the user declared and what
rtfm could actually look at do not match, so anything under that directory was
never considered. The user should not be handing rtfm an unreadable subtree, which
is the argument for saying so rather than against it — silently indexing part of a
corpus is the failure this whole design refuses, and a mistake being avoidable does
not make it self-announcing.

Weighting this honestly: the per-file ACCESS failure justifies itself, because an
unreadable file used to take down `reindex()` entirely. The directory case is
rarer — root-owned build output inside a doc tree, a group-restricted subtree on a
shared mount — and is cheap only because the walk had to stop swallowing the error
regardless.

### Handling routines are a registry, not an `if` chain

```python
ROUTINES = (
    _Routine("pdf",    frozenset({".pdf"}), _pdf_rows,  _pdf_doc_signal),
    _Routine("markup", MARKUP_EXTS,         _text_rows, _text_doc_signal),
)
DEFAULT_ROUTINE = _Routine("text", frozenset(), _text_rows, _no_doc_signal)
```

Body extraction and doc-level signal live in the same record, so the two
dispatches cannot disagree about which routine a file belongs to. Adding a
format — HTML with anchor locators (ADR 0004), or a structured reader for another
markup — is adding a routine, not editing two parallel `if` chains.

The fallback sits **outside** `ROUTINES` rather than inside it behind a sentinel.
Inside, it would match unconditionally, so placing it anywhere but last would
silently shadow every routine after it — `.pdf` quietly getting plain-text rows
and no signal, with nothing raised. Outside there is no ordering left to get
wrong: the invalid arrangement is unrepresentable rather than forbidden by a
comment.

The fallback deliberately carries no doc-level signal. Guessing structure from a
file rtfm has no reader for would put a source file's licence header into
doc-level ranking (ADR 0012): `#` is a comment in Starlark, Python and shell, and
the markup reader would turn every comment line into a heading.

## Consequences

- `TEXT_EXTS` is gone. `MARKUP_EXTS` is the markup routine's extension set and
  nothing else; `DEFAULT_EXT_BLOCKLIST` belongs to Stage 1 alone.
- `errors="replace"` is removed from every decode site. A non-UTF-8 file that a
  user genuinely wants indexed now fails loudly instead of being silently
  mangled; the remedy is to convert it or to exclude its type. If real non-UTF-8
  documents turn up, the fix is attempting a second named encoding at DECODE,
  never returning to replacement.
- `contents.error` and `summary["errors"]` stop being write-only — the tools
  surface them, because a stated format boundary is worthless if a user cannot
  hear that a file hit it.
- ACCESS and DECODE failures are counted and reported separately, because their
  remedies differ. Merging them would tell a user to convert a file they simply
  lack permission to open.
- An unenumerable directory is reported by SELECT, not ACCESS — different stage,
  different message, because it is a subtree that never reached selection.
- The source walk is `os.walk` with an `onerror` callback, not `rglob`. It returns
  both the files it selected and the directories it could not enter; a caller that
  only wants the files is unaffected.
- A new format is one `_Routine` entry plus its two functions.
