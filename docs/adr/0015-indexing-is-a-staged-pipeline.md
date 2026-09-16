---
status: accepted
---

# Indexing asks four questions in order, and each owns its own failure

rtfm indexes potentially stale bytes sitting at presumed-stable positions. Getting
those bytes into the index used to be one tangle of extension checks: a single
constant, `TEXT_EXTS`, decided both which files were selected and which got
markdown heading parsing; body extraction and doc-signal extraction dispatched on
extension independently in separate `if` chains; and `read_text(errors="replace")`
meant "can rtfm read this?" never got asked at all.

Everything that went wrong had the same shape — one step answering a question it
did not own, and giving the wrong remedy:

- A `.bzl` file was selected for indexing and then extracted to zero rows, because
  only one of the two extension dispatches knew about it.
- A binary file indexed cleanly with `errors: 0`, its bytes mangled into
  searchable text by U+FFFD substitution.
- A source whose extension list matched nothing indexed zero files and gave no
  reason.
- A corrupt PDF was reported as "not valid UTF-8" — a parse failure wearing a
  decoding error's message. PDF extraction never decodes UTF-8 at all.
- One `chmod 000` file made `reindex()` raise, made `search()` drop the entire
  source, and left `health_check()` reporting `ok: True`. Three call sites, three
  different behaviours, for one cause.

## Decision

A user provides a manifest. rtfm asks four questions, in order, and each one owns
its own failure.

### Step 1 — "Can I even get at these bytes?"

Access means one of two things, and each has its own handler:

- the user already downloaded them and pointed at a folder → OS/filesystem
  permission scan
- the user points at something remote → Git/webpage access smoke test

```
Happy path: everything is reachable.
Mid path:   partially. We do NOT stop here, but we warn.
Sad path:   nothing is. Stop, error message.
```

**Report this unfiltered.** We cannot filter what we could not read — an
inaccessible directory might hold exactly the files the user wants, and its
contents are unknowable from outside. Letting Step 2's intent suppress Step 1's
facts would leave a user writing a manifest against a corpus rtfm silently
truncated.

This step scans; it does not read. A `stat` per enumerated file costs
milliseconds over a 13,000-file tree and answers the common case — someone ran
`chmod` — without opening anything.

### Step 2 — "Out of that list, what do you want?"

`ext_allowlist`, or `ext_blocklist` plus rtfm's default blocklist (ADR 0014).

```
Happy path: we still have files left. Continue.
Sad path:   nothing left to process. Stop, error message.
```

**No mid path, why?** We do NOT speculate on how much filtering the user wanted.
Steps 1, 3A and 3B ask about the world, where "partial" is an observed fact worth
reporting. Step 2 asks about intent, where rtfm has no standing to call 90%
filtered a partial success. Either something survived or nothing did.

### Step 3A — "Are those bytes even readable?"

Step 1 scanned. This one actually reads: stale NFS/CIFS handles, `EIO`, ACLs
beyond the mode bits, the file moved underneath us between the scan and the read.

```
Happy path: every file gave up its bytes.
Mid path:   some didn't. Warn, and name them.
Sad path:   none did. Error message.
```

Failing here is infrastructure. The user fixes storage or permissions — not files.

### Step 3B — "Can we handle those bytes?"

One handler per format, and each reports in its own terms.

```
Happy path: everything processed.
Mid path:   some aren't supported by us. Warn, and name which and why.
Sad path:   nothing could be processed. Error message.
```

Failing here is the file. The user converts it, or excludes its type.

A corrupt PDF failed to *parse*; it is not "invalid UTF-8". Decoding is a concern
*inside* the text handlers, not a stage every file traverses — the PDF handler
reads bytes through a parser and never decodes UTF-8 at all. An earlier draft of
this ADR asserted a universal DECODE stage, which is exactly how a malformed PDF
came to be told to fix its encoding.

## The rules that fall out

**A cheap check may assert the negative, never the positive.** Step 1's mode bits
denying you is conclusive. Mode bits allowing you is a hint — `os.access` tests
the real uid against POSIX bits only, and 3A owns the truth. The same asymmetry
governs freshness: a git commit that moved is proof the bytes are stale, while a
file's mtime matching proves only that a position's metadata is unchanged. It is
never permission to skip a step.

**Every step has a handler table, not a branch.** Adding a source kind or a format
is adding a handler:

| Step | Handlers |
| --- | --- |
| 1 Access | local filesystem · git clone/fetch · web fetch |
| 2 Select | (policy, not a handler — the manifest's extension lists) |
| 3A Read | filesystem · http |
| 3B Handle | pdf · markup · plain text |

**A step never stands in for another.** Each failure names the step that owns it
and gives that step's remedy. Merging them produces exactly the defects listed
above: telling someone to convert a file they lack permission to open, or to check
an extension list when the directory was unreadable.

**Freshness gates the pipeline; it is not a step in it.** It decides whether to
ask the four questions at all. Its handlers differ in what they can prove without
looking: git compares the indexed commit to `origin/<ref>` and can skip the
traversal outright, while bookkeeping needs a `stat` per position and therefore
folds into Step 1 rather than preceding it.

## Consequences

- `TEXT_EXTS` is gone. `MARKUP_EXTS` is the markup handler's extension set and
  nothing else; `DEFAULT_EXT_BLOCKLIST` belongs to Step 2 alone.
- The four steps are code boundaries, not message text. An earlier attempt added
  the steps' *vocabulary* — warnings, counters, stage names — to one monolithic
  function, which widened the gap between what the code claimed and what it was.
  Every blocking defect found in review lived in that gap.
- Each step returns its own outcome. Reporting reads those outcomes; it does not
  reconstruct them by comparing counters across steps, which is how a content
  count came to be printed as a file count.
- `errors="replace"` stays gone from every decode site. If real non-UTF-8
  documents turn up, the fix is a second named encoding inside the text handler,
  never replacement.
- Step 1's report reaches `search`, `reindex` and `health_check`. A stated
  boundary is worthless if a user cannot hear that they hit it.
